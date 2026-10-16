#!/usr/bin/env python3
"""
Acurast Reward Daemon v2 — Blackshirt Crypto
Final definitive version.

Setup:
  source ~/acurast-env/bin/activate
  python3 ~/acurast-dashboard/acurast_daemon_v2.py 2>/dev/null

PM2:
  pm2 start ~/acurast-dashboard/acurast_daemon_v2.py \
    --name acurast-dashboard \
    --interpreter ~/acurast-env/bin/python3
  pm2 save
"""

import sqlite3, os, time, logging, threading, json, urllib.request
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, SimpleHTTPRequestHandler
from substrateinterface import SubstrateInterface

# -- User config: edit config.py, not this file --------------------------------
try:
    from config import WALLETS, MANAGER_ID, DB_PATH, HTML_PATH, DASHBOARD_HOST, DASHBOARD_PORT
except ImportError as e:
    raise SystemExit('config.py missing or incomplete ({}). '
                     'Run: cp config.example.py config.py  -- then edit it.'.format(e))

# ── Config ────────────────────────────────────────────────────────────────────
RPC            = 'wss://public-rpc.mainnet.acurast.com'
SCAN_BLOCKS    = 2000
POLL_SECS      = 90 * 60
DECIMALS       = 12
COINGECKO_URL  = 'https://api.coingecko.com/api/v3/simple/price?ids=acurast&vs_currencies=usd'
REWARDS_PALLET = '5EYCAe5g86uWAqpCzj2AMUzgybxYTycRNNxSBK17aziwTZAH'
PULSE_API      = 'https://www.acurastpulse.com/api'

MAINNET_LAUNCH = datetime(2026, 1, 20, tzinfo=timezone.utc)
MAX_CYCLES     = 48
MIN_CYCLES     = 3
CYCLE_DAYS     = 28
MIN_PCT        = 6.5
MAX_PCT        = 100.0

# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)s  %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger('acurast')

# ── Conversion helpers ────────────────────────────────────────────────────────
def conversion_progress(lock_start_block=None, current_block=None, amount=0.0):
    now        = datetime.now(timezone.utc)
    days_in    = (now - MAINNET_LAUNCH).days
    cycles_in  = days_in / CYCLE_DAYS
    total_days = MAX_CYCLES * CYCLE_DAYS
    full_date  = MAINNET_LAUNCH + timedelta(days=total_days)
    days_left  = max((full_date - now).days, 0)
    pct_time   = min(days_in / total_days * 100, 100)

    if cycles_in <= MIN_CYCLES:
        unlock_pct = MIN_PCT
    elif cycles_in >= MAX_CYCLES:
        unlock_pct = MAX_PCT
    else:
        unlock_pct = MIN_PCT + ((cycles_in - MIN_CYCLES) / (MAX_CYCLES - MIN_CYCLES)) * (MAX_PCT - MIN_PCT)

    now_value  = amount * (unlock_pct / 100.0)
    forfeit    = amount - now_value

    return {
        'days_in':    days_in,
        'days_left':  days_left,
        'total_days': total_days,
        'cycles_in':  round(cycles_in, 2),
        'pct_time':   round(pct_time, 2),
        'unlock_pct': round(unlock_pct, 2),
        'full_date':  full_date.strftime('%b %d, %Y'),
        'amount':     amount,
        'now_value':  round(now_value, 4),
        'forfeit':    round(forfeit, 4),
    }

def short_addr(addr):
    if not addr or len(addr) < 12:
        return addr or ''
    return addr[:6] + '...' + addr[-6:]

# ── Database ──────────────────────────────────────────────────────────────────
def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    return conn

def db_init():
    conn = db_connect()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS transfers (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet      TEXT NOT NULL,
            block       INTEGER NOT NULL,
            ts          TEXT NOT NULL,
            direction   TEXT NOT NULL,
            amount_raw  TEXT NOT NULL,
            tx_type     TEXT DEFAULT 'transfer',
            counterpart TEXT,
            UNIQUE(wallet, block, direction, counterpart)
        );
        CREATE TABLE IF NOT EXISTS balances (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet       TEXT NOT NULL,
            ts           TEXT NOT NULL,
            free_raw     TEXT NOT NULL,
            reserved_raw TEXT NOT NULL,
            locked_raw   TEXT DEFAULT '0'
        );
        CREATE TABLE IF NOT EXISTS scan_state (
            wallet       TEXT PRIMARY KEY,
            last_block   INTEGER NOT NULL DEFAULT 0,
            last_scan_ts TEXT
        );
        CREATE TABLE IF NOT EXISTS price_cache (
            id  INTEGER PRIMARY KEY CHECK (id = 1),
            usd REAL NOT NULL,
            ts  TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS airdrop_locks (
            wallet      TEXT PRIMARY KEY,
            amount_raw  TEXT NOT NULL,
            lock_start  INTEGER NOT NULL,
            ts          TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS baselines (
            wallet    TEXT PRIMARY KEY,
            total_raw TEXT NOT NULL,
            ts        TEXT NOT NULL
        );
    ''')
    for sql in [
        'ALTER TABLE balances ADD COLUMN locked_raw TEXT DEFAULT "0"',
        'ALTER TABLE transfers ADD COLUMN tx_type TEXT DEFAULT "transfer"',
        'ALTER TABLE scan_state ADD COLUMN last_scan_ts TEXT',
    ]:
        try:
            conn.execute(sql)
        except Exception:
            pass
    conn.commit()
    conn.close()
    log.info('DB ready: %s', DB_PATH)

# ── Helpers ───────────────────────────────────────────────────────────────────
def now_utc():
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')

def to_float(raw):
    try:
        return int(str(raw)) / (10 ** DECIMALS)
    except Exception:
        return 0.0

def fmt(v, dp=4):
    try:
        return ('%.' + str(dp) + 'f') % float(v)
    except Exception:
        return '0.0000'

def usd_str(v, price):
    return '$%.2f' % (float(v) * price)

def classify_tx(direction, counterpart):
    if direction == 'IN' and counterpart == REWARDS_PALLET:
        return 'reward'
    if direction == 'OUT':
        return 'heartbeat_fee'
    return 'transfer'

# ── Price ─────────────────────────────────────────────────────────────────────
def get_acu_price():
    # Try to get cached price first — avoids DB lock during scans
    try:
        conn = db_connect()
        row = conn.execute('SELECT usd, ts FROM price_cache WHERE id=1').fetchone()
        conn.close()
        if row:
            ts = datetime.fromisoformat(row['ts'].replace(' ', 'T') + '+00:00')
            age = (datetime.now(timezone.utc) - ts).seconds
            if age < 300:
                return float(row['usd'])
            # Cache stale but use it as fallback if fetch fails
            fallback = float(row['usd'])
        else:
            fallback = 0.0
    except Exception:
        fallback = 0.0
    try:
        req = urllib.request.Request(COINGECKO_URL, headers={'User-Agent': 'BlackshirtCrypto/2.0'})
        with urllib.request.urlopen(req, timeout=10) as r:
            price = float(json.loads(r.read())['acurast']['usd'])
        conn2 = db_connect()
        conn2.execute('INSERT OR REPLACE INTO price_cache(id,usd,ts) VALUES(1,?,?)', (price, now_utc()))
        conn2.commit()
        conn2.close()
        log.info('ACU price: $%.6f', price)
        return price
    except Exception as e:
        log.warning('Price fetch failed: %s — using cached', e)
        return fallback

# ── Substrate ─────────────────────────────────────────────────────────────────
def get_substrate():
    return SubstrateInterface(url=RPC, ss58_format=42,
                              ws_options={'timeout': 20})

def get_balance(sub, address):
    acct     = sub.query('System', 'Account', [address])
    free     = int(str(acct['data']['free'].value))
    frozen   = int(str(acct['data']['frozen'].value))
    reserved = int(str(acct['data']['reserved'].value))
    return free, frozen, reserved

def get_airdrop_lock(sub, address):
    try:
        r = sub.query('AcurastTokenConversion', 'LockedConversion', [address])
        if r.value:
            return int(str(r.value['amount'])), int(str(r.value['lock_start']))
    except Exception:
        pass
    return 0, 0



# ── Pulse API ─────────────────────────────────────────────────────────────────
def pulse_get(path, timeout=20):
    try:
        req = urllib.request.Request(
            PULSE_API + path,
            headers={"User-Agent": "BlackshirtCrypto-Daemon/2.0", "Accept": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
            if data.get("ok") is False:
                return None
            return data
    except Exception as e:
        log.warning("Pulse API %s: %s", path, e)
        return None

def pulse_sync_ledger(conn, label, address):
    # Get highest pulse_id already stored for this wallet
    row = conn.execute(
        "SELECT MAX(CAST(pulse_id AS INTEGER)) FROM pulse_ledger WHERE wallet=?", (label,)
    ).fetchone()
    last_id = row[0] if row and row[0] else 0

    offset, limit, new_count = 0, 50, 0
    done = False
    while not done:
        data = pulse_get("/accounts/{}/ledger?limit={}&offset={}".format(address, limit, offset))
        if not data:
            break
        entries = data.get("ledger", [])
        if not entries:
            break
        for e in entries:
            entry_id = int(str(e["id"]))
            if entry_id <= last_id:
                # Reached entries we already have — stop paginating
                done = True
                break
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO pulse_ledger "
                    "(pulse_id,wallet,block_number,ts,direction,amount_raw,section,method,"
                    "extrinsic_section,extrinsic_method,counterpart,classification,fee_raw,"
                    "success,acu_price_usd) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        str(entry_id), label, int(e["block_number"]),
                        e.get("block_timestamp",""), e.get("direction",""),
                        str(e.get("amount",0) or 0), e.get("section"),
                        e.get("method"), e.get("extrinsic_section"),
                        e.get("extrinsic_method"), e.get("counterparty_account_id"),
                        e.get("wallet_classification"), str(e.get("fee",0) or 0),
                        1 if e.get("success") else 0, e.get("acuPriceUsdAtTx")
                    )
                )
                new_count += 1
            except Exception as ex:
                log.debug("pulse_ledger insert %s: %s", e.get("id"), ex)
        if not data.get("ledgerHasMore"):
            break
        offset += limit
    return new_count

def pulse_sync_rewards(conn, label):
    rows = conn.execute(
        "SELECT substr(ts,1,10) as day, SUM(CAST(amount_raw AS REAL)) as total,"
        " COUNT(*) as cnt FROM pulse_ledger"
        " WHERE wallet=? AND classification=\"compute_reward\" AND direction=\"in\""
        " GROUP BY substr(ts,1,10)", (label,)
    ).fetchall()
    for row in rows:
        conn.execute(
            "INSERT OR REPLACE INTO pulse_rewards(wallet,day,reward_raw,claim_count)"
            " VALUES(?,?,?,?)",
            (label, row["day"], str(int(row["total"])), row["cnt"])
        )

def pulse_sync_delegations(conn, label, address, ts):
    data = pulse_get("/accounts/{}/claimable-rewards".format(address))
    if not data:
        return
    for c in data.get("commitments", []):
        conn.execute(
            "INSERT OR REPLACE INTO delegations"
            "(wallet,manager_id,commitment_id,delegated_raw,accrued_raw,paid_raw,ts)"
            " VALUES(?,?,?,?,?,?,?)",
            (label, str(c.get("managerId","")), str(c.get("commitmentId","")),
             "0", str(c.get("accruedPlanck",0)), str(c.get("paidPlanck",0)), ts)
        )

def pulse_sync_fleet(conn, ts):
    if not MANAGER_ID:
        return None   # no processor fleet configured
    data = pulse_get("/managers/{}".format(MANAGER_ID))
    if not data:
        log.warning("Fleet sync: no data returned")
        return
    fleet = data.get("fleet", {})
    stats = data.get("stats", {})
    conn.execute(
        "INSERT OR REPLACE INTO fleet_snapshots"
        "(ts,manager_id,total_devices,online,stale,down,fleet_health,"
        "fleet_uptime,deployed,earning_rate,avg_hb_lag_sec) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            ts, MANAGER_ID,
            fleet.get("total"), fleet.get("online"),
            fleet.get("stale"), fleet.get("down"),
            stats.get("fleetHealth"), stats.get("fleetUptime"),
            stats.get("deployed"), stats.get("earningRateDay"),
            stats.get("avgHbLagSec")
        )
    )

# ── Scan ──────────────────────────────────────────────────────────────────────
def scan_once():
    log.info("--- Scan start ---")
    try:
        _sub = get_substrate()
        head = _sub.get_block_number(_sub.get_chain_finalised_head())
        _sub.close()
    except Exception as e:
        log.error("RPC connect failed: %s", e)
        return
    ts    = now_utc()
    price = get_acu_price()
    log.info("Chain head: #%s  |  ACU: $%.6f", format(head, ","), price)

    conn = db_connect()

    for label, cfg in WALLETS.items():
        address = cfg["address"]
        sub = None
        try:
            # Live balance + airdrop lock from RPC
            sub = get_substrate()
            free, frozen, reserved = get_balance(sub, address)
            airdrop_raw, lock_start = get_airdrop_lock(sub, address)
            sub.close()
            sub = None

            conn.execute(
                "INSERT INTO balances(wallet,ts,free_raw,reserved_raw,locked_raw) VALUES(?,?,?,?,?)",
                (label, ts, str(free), str(reserved), str(frozen))
            )
            if airdrop_raw > 0:
                conn.execute(
                    "INSERT OR REPLACE INTO airdrop_locks(wallet,amount_raw,lock_start,ts) VALUES(?,?,?,?)",
                    (label, str(airdrop_raw), lock_start, ts)
                )
            if not conn.execute("SELECT 1 FROM baselines WHERE wallet=?", (label,)).fetchone():
                conn.execute(
                    "INSERT INTO baselines(wallet,total_raw,ts) VALUES(?,?,?)",
                    (label, str(free), ts)
                )
            airdrop_f = to_float(airdrop_raw)
            staked_f  = max(to_float(frozen) - airdrop_f, 0.0)
            free_f    = max(to_float(free) - staked_f, 0.0)
            log.info("%s: %.4f total | %.4f airdrop | %.4f staked | %.4f free | $%.2f",
                     label, to_float(free), airdrop_f, staked_f, free_f, to_float(free)*price)

            # Pulse ledger sync
            new_txs = pulse_sync_ledger(conn, label, address)
            log.info("%s: %d new Pulse ledger entries", label, new_txs)

            # Aggregate rewards by day
            pulse_sync_rewards(conn, label)

            # Delegation claimable rewards
            pulse_sync_delegations(conn, label, address, ts)

            # Update scan state
            conn.execute(
                "INSERT OR REPLACE INTO scan_state(wallet,last_block,last_scan_ts) VALUES(?,?,?)",
                (label, head, ts)
            )

        except Exception as e:
            log.error("%s scan error: %s", label, e)
        finally:
            if sub:
                try:
                    sub.close()
                except Exception:
                    pass

    # Fleet health snapshot
    try:
        pulse_sync_fleet(conn, ts)
        log.info("Fleet snapshot saved")
    except Exception as e:
        log.warning("Fleet sync failed: %s", e)

    conn.commit()
    conn.close()
    generate_dashboard(price)
    log.info("--- Scan complete ---")

# ── Dashboard ─────────────────────────────────────────────────────────────────
def generate_dashboard(price=None):
    if price is None:
        price = get_acu_price()

    conn = db_connect()

    wallet_data = []
    for label, cfg in WALLETS.items():
        color   = cfg['color']
        address = cfg['address']

        bal = conn.execute(
            'SELECT free_raw, reserved_raw, locked_raw, ts FROM balances WHERE wallet=? ORDER BY id DESC LIMIT 1',
            (label,)
        ).fetchone()
        free     = to_float(bal['free_raw'])      if bal else 0.0
        frozen   = to_float(bal['locked_raw'])    if bal else 0.0
        reserved = to_float(bal['reserved_raw'])  if bal else 0.0

        airdrop_row = conn.execute(
            'SELECT amount_raw, lock_start FROM airdrop_locks WHERE wallet=?', (label,)
        ).fetchone()
        airdrop_amt   = to_float(airdrop_row['amount_raw']) if airdrop_row else 0.0
        airdrop_start = airdrop_row['lock_start']           if airdrop_row else 0
        staked        = max(frozen - airdrop_amt, 0.0)
        free_f        = max(free - staked, 0.0)
        manual_lock   = float(cfg.get('manual_lock', 0) or 0)   # lock the RPC can't see (set in config.py)
        wallet_total  = airdrop_amt + staked + free_f + manual_lock
        conv          = conversion_progress(airdrop_start, None, airdrop_amt) if airdrop_amt > 0 else None

        base_row  = conn.execute('SELECT total_raw FROM baselines WHERE wallet=?', (label,)).fetchone()
        baseline  = to_float(base_row['total_raw']) if base_row else free

        total_in = conn.execute(
            'SELECT COALESCE(SUM(CAST(amount_raw AS REAL)),0) FROM transfers WHERE wallet=? AND direction="IN"',
            (label,)
        ).fetchone()[0] / (10 ** DECIMALS)

        total_out = conn.execute(
            'SELECT COALESCE(SUM(CAST(amount_raw AS REAL)),0) FROM transfers WHERE wallet=? AND direction="OUT"',
            (label,)
        ).fetchone()[0] / (10 ** DECIMALS)

        hb_fees = conn.execute(
            'SELECT COALESCE(SUM(CAST(amount_raw AS REAL)),0) FROM transfers WHERE wallet=? AND direction="OUT" AND tx_type="heartbeat_fee"',
            (label,)
        ).fetchone()[0] / (10 ** DECIMALS)

        xfer_fees = conn.execute(
            'SELECT COALESCE(SUM(CAST(amount_raw AS REAL)),0) FROM transfers WHERE wallet=? AND direction="OUT" AND tx_type="transfer"',
            (label,)
        ).fetchone()[0] / (10 ** DECIMALS)

        rewards_in = conn.execute(
            'SELECT COALESCE(SUM(CAST(amount_raw AS REAL)),0) FROM transfers WHERE wallet=? AND direction="IN" AND tx_type="reward"',
            (label,)
        ).fetchone()[0] / (10 ** DECIMALS)

        # Claim sessions — group reward INs within 200 blocks of each other
        claims = conn.execute(
            'SELECT block, ts, amount_raw FROM transfers WHERE wallet=? AND tx_type="reward" AND direction="IN" ORDER BY block ASC',
            (label,)
        ).fetchall()

        sessions = []
        if claims:
            sess_amt   = to_float(claims[0]['amount_raw'])
            sess_block = claims[0]['block']
            sess_ts    = claims[0]['ts']
            for i in range(1, len(claims)):
                if claims[i]['block'] - claims[i-1]['block'] < 200:
                    sess_amt += to_float(claims[i]['amount_raw'])
                else:
                    sessions.append({'block': sess_block, 'ts': sess_ts, 'amount': sess_amt})
                    sess_amt   = to_float(claims[i]['amount_raw'])
                    sess_block = claims[i]['block']
                    sess_ts    = claims[i]['ts']
            sessions.append({'block': sess_block, 'ts': sess_ts, 'amount': sess_amt})

        # 7-day rolling average from balance history
        daily_est = 0.0
        try:
            bal_7d = conn.execute(
                "SELECT MAX(CAST(free_raw AS REAL)) as hi, MIN(CAST(free_raw AS REAL)) as lo, "
                "COUNT(*) as cnt FROM balances WHERE wallet=? AND ts >= datetime('now', '-7 days')",
                (label,)
            ).fetchone()
            if bal_7d and bal_7d['cnt'] and bal_7d['cnt'] >= 2:
                delta = (bal_7d['hi'] - bal_7d['lo']) / (10 ** DECIMALS)
                daily_est = round(delta / 7.0, 4)
        except Exception:
            daily_est = 0.0

        bal_history = conn.execute(
            'SELECT date(ts) as day, AVG(CAST(free_raw AS REAL)) as avg_free FROM balances WHERE wallet=? GROUP BY day ORDER BY day',
            (label,)
        ).fetchall()

        # Daily fee/reward summary
        daily_summary = conn.execute(
            '''SELECT date(ts) as day,
               SUM(CASE WHEN tx_type="reward" AND direction="IN" THEN CAST(amount_raw AS REAL) ELSE 0 END) as rewards,
               SUM(CASE WHEN tx_type="heartbeat_fee" AND direction="OUT" THEN CAST(amount_raw AS REAL) ELSE 0 END) as hb_fees,
               COUNT(CASE WHEN tx_type="heartbeat_fee" AND direction="OUT" THEN 1 END) as hb_count,
               SUM(CASE WHEN tx_type="transfer" AND direction="OUT" THEN CAST(amount_raw AS REAL) ELSE 0 END) as xfer_fees
               FROM transfers WHERE wallet=? GROUP BY day ORDER BY day DESC LIMIT 14''',
            (label,)
        ).fetchall()

        recent_txs = conn.execute(
            'SELECT block, ts, direction, amount_raw, tx_type, counterpart FROM transfers WHERE wallet=? ORDER BY block DESC LIMIT 50',
            (label,)
        ).fetchall()

        # Pulse ledger claims — compute_reward IN entries
        pulse_claims = conn.execute(
            "SELECT pulse_id, block_number as block, ts, amount_raw, extrinsic_method, classification "
            "FROM pulse_ledger WHERE wallet=? AND direction='in' "
            "AND (classification='compute_reward' OR "
            "     (extrinsic_method='compoundDelegation' AND classification IS NULL)) "
            "ORDER BY block_number DESC LIMIT 100",
            (label,)
        ).fetchall()

        wallet_data.append({
            'label':         label,
            'color':         color,
            'address':       address,
            'short_addr':    short_addr(address),
            'free':          free,
            'wallet_total':  wallet_total,
            'staked':        staked,
            'frozen':        frozen,
            'free_f':        free_f,
            'airdrop_amt':   airdrop_amt,
            'manual_lock':   manual_lock,
            'airdrop_start': airdrop_start,
            'reserved':      reserved,
            'conv':          conv,
            'baseline':      baseline,
            'total_in':      total_in,
            'total_out':     total_out,
            'rewards_in':    rewards_in,
            'hb_fees':       hb_fees,
            'xfer_fees':     xfer_fees,
            'daily_est':     daily_est,
            'sessions':      sessions,
            'bal_history':   bal_history,
            'daily_summary': daily_summary,
            'recent_txs':    recent_txs,
            'pulse_claims':  pulse_claims,
            'pulse_hb_fees':  conn.execute(
                "SELECT COALESCE(SUM(CAST(amount_raw AS REAL)),0) FROM pulse_ledger "
                "WHERE wallet=? AND direction='fee' AND extrinsic_method='heartbeatWithMetrics'",
                (label,)).fetchone()[0] / (10**DECIMALS),
            'pulse_fee_entries': conn.execute(
                "SELECT pulse_id, block_number as block, ts, amount_raw, extrinsic_method "
                "FROM pulse_ledger WHERE wallet=? AND direction='fee' "
                "ORDER BY block_number DESC LIMIT 50",
                (label,)
            ).fetchall(),
            'pulse_tx_fees':  conn.execute(
                "SELECT COALESCE(SUM(CAST(amount_raw AS REAL)),0) FROM pulse_ledger "
                "WHERE wallet=? AND direction='fee' AND extrinsic_method!='heartbeatWithMetrics'",
                (label,)).fetchone()[0] / (10**DECIMALS),
        })

    conn.close()

    grand_free    = sum(w['wallet_total'] for w in wallet_data)
    grand_frozen  = sum(w['frozen']     for w in wallet_data)
    grand_free_f  = sum(w['free_f']     for w in wallet_data)
    grand_airdrop = sum(w['airdrop_amt'] + w['manual_lock'] for w in wallet_data)
    grand_in      = sum(w['total_in']   for w in wallet_data)
    grand_out     = sum(w['total_out']  for w in wallet_data)
    grand_hb      = sum(w['hb_fees']    for w in wallet_data)
    grand_xfer    = sum(w['xfer_fees']  for w in wallet_data)
    grand_daily   = sum(w['daily_est']  for w in wallet_data)
    grand_rewards = sum(w['rewards_in'] for w in wallet_data)

    all_days = sorted(set(r['day'] for w in wallet_data for r in w['bal_history']))

    combined_ds = []
    for w in wallet_data:
        day_map = {r['day']: round(r['avg_free'] / (10 ** DECIMALS), 4) for r in w['bal_history']}
        combined_ds.append({
            'label':                w['label'],
            'data':                 [day_map.get(d) for d in all_days],
            'borderColor':          w['color'],
            'backgroundColor':      w['color'] + '11',
            'pointBackgroundColor': w['color'],
            'pointRadius':          5,
            'tension':              0.3,
            'fill':                 False,
            'spanGaps':             True,
        })

    last_scan = now_utc()

    def usd(v):
        return '$%.2f' % (float(v) * price)

    def stat_cell(cls, label, value, sub_val, tooltip, lock=False):
        lock_html = '<span style="position:absolute;top:5px;right:7px;font-size:10px;opacity:0.5">&#x1F512;</span>' if lock else ''
        return (
            '<div class="cell ' + cls + '" style="position:relative">'
            + lock_html +
            '<div class="cell-lbl">' + label + '</div>'
            '<div class="cell-val">' + value + '</div>'
            '<div class="cell-sub">' + sub_val + '</div>'
            '<div class="tooltip">' + tooltip + '</div>'
            '</div>'
        )

    def conv_section_single(w):
        if not w['conv']:
            return (
                '<div class="block">'
                '<div class="block-title">cACU &rarr; ACU Conversion Lock</div>'
                '<div style="color:#3a5a20;font-size:11px;padding:6px 0">'
                'No active conversion lock on this wallet. '
                'Switch to a staking wallet tab to see conversion progress.'
                '</div></div>'
            )
        c = w['conv']
        bar_w = str(c['pct_time'])
        return (
            '<div class="block">'
            '<div class="block-title">'
            '<span>cACU &rarr; ACU Conversion Lock — ' + w['label'] + '</span>'
            '<span style="color:#3a5a20;font-size:10px">Full unlock: ' + c['full_date'] + ' &nbsp;&middot;&nbsp; ' + str(c['days_left']) + ' days remaining</span>'
            '</div>'
            '<div class="conv-track" style="margin-bottom:12px">'
            '<div class="conv-fill" style="width:' + bar_w + '%"></div>'
            '<div class="conv-lbl">' + bar_w + '% of time elapsed</div>'
            '</div>'
            '<div class="conv-stats">'
            '<div class="conv-stat"><div class="conv-stat-lbl">Locked Amount</div>'
            '<div class="conv-stat-val">' + fmt(c['amount']) + '</div>'
            '<div class="conv-stat-sub">ACU &nbsp;|&nbsp; ' + usd(c['amount']) + '</div></div>'
            '<div class="conv-stat"><div class="conv-stat-lbl">If Unlocked Today</div>'
            '<div class="conv-stat-val" style="color:#ffc845">' + fmt(c['now_value']) + '</div>'
            '<div class="conv-stat-sub">ACU (' + str(c['unlock_pct']) + '%) &nbsp;|&nbsp; ' + usd(c['now_value']) + '</div></div>'
            '<div class="conv-stat"><div class="conv-stat-lbl">At Full Term</div>'
            '<div class="conv-stat-val" style="color:#c8f135">' + fmt(c['amount']) + '</div>'
            '<div class="conv-stat-sub">ACU (100%) &nbsp;|&nbsp; ' + usd(c['amount']) + '</div></div>'
            '<div class="conv-stat"><div class="conv-stat-lbl">Would Forfeit Now</div>'
            '<div class="conv-stat-val" style="color:#ff6b6b">' + fmt(c['forfeit']) + '</div>'
            '<div class="conv-stat-sub">ACU &nbsp;|&nbsp; ' + usd(c['forfeit']) + '</div></div>'
            '</div></div>'
        )

    def conv_section_combined():
        cols = ''
        wallets_with_conv = [w for w in wallet_data if w['conv']]
        total_now  = sum(w['conv']['now_value'] for w in wallets_with_conv)
        total_full = sum(w['conv']['amount']    for w in wallets_with_conv)
        for w in wallets_with_conv:
            c = w['conv']
            cols += (
                '<div style="flex:1;min-width:180px;padding:10px;border:1px solid #1a2a0a;border-radius:6px">'
                '<div style="color:' + w['color'] + ';font-weight:bold;margin-bottom:6px">' + w['label'] + '</div>'
                '<div class="conv-track" style="margin-bottom:6px">'
                '<div class="conv-fill" style="width:' + str(c['pct_time']) + '%"></div>'
                '<div class="conv-lbl">' + str(c['pct_time']) + '% elapsed</div>'
                '</div>'
                '<div style="font-size:11px;color:#ffc845">' + fmt(c['now_value']) + ' ACU now</div>'
                '<div style="font-size:11px;color:#c8f135">' + fmt(c['amount']) + ' ACU at term</div>'
                '<div style="font-size:10px;color:#4a6a20;margin-top:4px">' + str(c['days_left']) + ' days remaining</div>'
                '</div>'
            )
        return (
            '<div class="block">'
            '<div class="block-title">'
            '<span>cACU &rarr; ACU Conversion Lock — All Wallets</span>'
            '<span style="color:#3a5a20;font-size:11px">Full unlock: '
            + str(next((w['conv']['full_date'] for w in wallets_with_conv), 'TBA')) +
            '</span>'
            '</div>'
            '<div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:10px">' + cols + '</div>'
            '<div style="font-size:11px;color:#4a5a20;border-top:1px solid #1a2a0a;padding-top:8px">'
            'If all unlocked today: <span style="color:#ffc845">' + fmt(total_now) + ' ACU</span>'
            ' &nbsp;|&nbsp; At full term: <span style="color:#c8f135">' + fmt(total_full) + ' ACU</span>'
            ' &nbsp;|&nbsp; Would forfeit: <span style="color:#ff6b6b">' + fmt(total_full - total_now) + ' ACU</span>'
            '</div></div>'
        )

    def chart_block(wid, days, bals, color, title='Balance History (ACU)'):
        return (
            '<div class="block">'
            '<div class="block-title">'
            '<span>' + title + '</span>'
            '<div class="chart-range" id="rb-' + wid + '">'
            '<button class="rbtn active" onclick="setRange(this,\'' + wid + '\')">1D</button>'
            '<button class="rbtn" onclick="setRange(this,\'' + wid + '\')">1W</button>'
            '<button class="rbtn" onclick="setRange(this,\'' + wid + '\')">1M</button>'
            '<button class="rbtn" onclick="setRange(this,\'' + wid + '\')">3M</button>'
            '<button class="rbtn" onclick="setRange(this,\'' + wid + '\')">ALL</button>'
            '</div></div>'
            '<canvas id="bc-' + wid + '" height="90"></canvas>'
            '<script>(function(){'
            'var D=' + json.dumps(days) + ';var B=' + json.dumps(bals) + ';'
            'window["D_' + wid + '"]=D;window["B_' + wid + '"]=B;'
            'var ctx=document.getElementById("bc-' + wid + '").getContext("2d");'
            'window["ch_' + wid + '"]=new Chart(ctx,{'
            'type:"line",'
            'data:{labels:D,datasets:[{label:"ACU",data:B,'
            'borderColor:"' + color + '",backgroundColor:"' + color + '18",'
            'pointBackgroundColor:"' + color + '",'
            'pointRadius:' + ('6' if len(bals) <= 1 else '4') + ','
            'tension:0.3,fill:true,spanGaps:true}]},'
            'options:{responsive:true,spanGaps:true,'
            'plugins:{legend:{display:false},'
            'tooltip:{callbacks:{label:function(c){return c.parsed.y.toFixed(4)+" ACU";}}}},'
            'scales:{'
            'x:{ticks:{color:"#4a7a10",font:{family:"Share Tech Mono",size:10}},grid:{color:"#1a2a0a"}},'
            'y:{ticks:{color:"#4a7a10",font:{family:"Share Tech Mono",size:10}},grid:{color:"#1a2a0a"}}'
            '}}});'
            '})()</script></div>'
        )

    def fee_bar(w):
        is_proc    = bool(WALLETS.get(w['label'], {}).get('processor'))
        hb_fees    = w.get('pulse_hb_fees', 0.0)
        tx_fees    = w.get('pulse_tx_fees', 0.0)
        total_fees = hb_fees + tx_fees
        net_daily  = max(w['daily_est'] - (total_fees / 30.0), 0.0)
        na = '<div class="fee-val" style="color:#3a5a20">N/A</div><div class="fee-sub" title="Staking wallets do not generate heartbeat or transfer fees">Not applicable</div>'
        def fc(lbl, val, sub):
            return '<div class="fee-cell"><div class="fee-lbl">' + lbl + '</div>' + val + sub + '</div>'
        def fv(v, dp=6): return '<div class="fee-val">' + fmt(v,dp) + '</div>'
        def fs(s): return '<div class="fee-sub">' + s + '</div>'
        return (
            '<div class="fee-bar">'
            + fc('Heartbeat Fees', fv(hb_fees) if is_proc else na, fs('ACU all time') if is_proc else '')
            + fc('Transfer Fees',  fv(tx_fees)  if is_proc else na, fs('ACU all time') if is_proc else '')
            + fc('Total Fees',     fv(total_fees) if is_proc else na, fs('ACU | ' + usd(total_fees)) if is_proc else '')
            + fc('Net Daily (est.)', fv(net_daily, 4), fs('ACU / day after fees'))
            + '</div>'
        )

    def history_block(w):
        wid = w['label'].replace(' ', '_')

        # Claim sessions table rows — prefer pulse_ledger, fall back to transfers table
        sess_rows = ''
        if w.get('pulse_claims'):
            # Group claims by day — total ACU + count per day
            from collections import defaultdict, OrderedDict
            day_totals  = defaultdict(float)
            day_counts  = defaultdict(int)
            day_blocks  = {}  # keep highest block per day for reference
            pc_list = list(w['pulse_claims'])
            for pc in pc_list:
                day = pc['ts'][:10] if pc['ts'] else '—'
                amt = to_float(pc['amount_raw'])
                day_totals[day] += amt
                day_counts[day] += 1
                blk = pc['block']
                if day not in day_blocks or blk > day_blocks[day]:
                    day_blocks[day] = blk
            # Sort days newest first
            sorted_days = sorted(day_totals.keys(), reverse=True)
            for i, day in enumerate(sorted_days):
                total = day_totals[day]
                count = day_counts[day]
                blk   = day_blocks[day]
                # Days since previous day entry
                if i < len(sorted_days) - 1:
                    try:
                        d1   = datetime.fromisoformat(sorted_days[i+1] + 'T00:00:00+00:00')
                        d2   = datetime.fromisoformat(day + 'T00:00:00+00:00')
                        days = max((d2 - d1).total_seconds() / 86400, 1.0)
                        gap  = '%.1f days' % days
                        rate = '%.4f ACU/day' % (total / days)
                    except Exception:
                        gap  = '—'
                        rate = '—'
                else:
                    gap  = '1st day'
                    rate = 'Need 2+ days'
                sess_rows += (
                    '<tr>'
                    '<td style="color:#6a9a40">' + day + '</td>'
                    '<td class="in">+' + fmt(total, 4) + ' ACU (' + str(count) + ' claims)</td>'
                    '<td style="color:#7ab520;font-size:10px">' + usd(total) + '</td>'
                    '<td style="color:#6a6a5a">' + gap + '</td>'
                    '<td class="rate">' + rate + '</td>'
                    '<td style="color:#3a5a10;font-size:10px">#' + format(blk, ',') + '</td>'
                    '</tr>'
                )
        else:
            # Fallback: transfers table sessions (our own chain scan backup)
            for i, s in enumerate(reversed(w['sessions'])):
                prev  = w['sessions'][-(i+2)] if i < len(w['sessions'])-1 else None
                if prev:
                    try:
                        d1   = datetime.fromisoformat(prev['ts'].replace(' ', 'T') + '+00:00')
                        d2   = datetime.fromisoformat(s['ts'].replace(' ', 'T') + '+00:00')
                        days = max((d2 - d1).total_seconds() / 86400, 0.1)
                        rate = '%.4f ACU/day' % (s['amount'] / days)
                        gap  = '%.1f days' % days
                    except Exception:
                        rate = '—'
                        gap  = '—'
                else:
                    rate = 'Need 2+ claims'
                    gap  = '1st claim'
                date_str = s['ts'][:10] if s['ts'] else '—'
                sess_rows += (
                    '<tr>'
                    '<td style="color:#6a9a40">' + date_str + '</td>'
                    '<td class="in">+' + fmt(s['amount'], 4) + ' ACU</td>'
                    '<td style="color:#7ab520;font-size:10px">' + usd(s['amount']) + '</td>'
                    '<td style="color:#6a6a5a">' + gap + '</td>'
                    '<td class="rate">' + rate + '</td>'
                    '<td style="color:#3a5a10;font-size:10px">#' + format(s['block'], ',') + '</td>'
                    '</tr>'
                )
        # Fall back to pulse_ledger claims if no sessions from transfers table
        if not sess_rows and w.get('pulse_claims'):
            for pc in w['pulse_claims']:
                amt = to_float(pc['amount_raw'])
                date_str = pc['ts'][:10] if pc['ts'] else '—'
                method = pc['extrinsic_method'] or '—'
                sess_rows += (
                    '<tr>'
                    '<td style="color:#6a9a40">' + date_str + '</td>'
                    '<td class="in">+' + fmt(amt, 4) + ' ACU</td>'
                    '<td style="color:#7ab520;font-size:10px">' + usd(amt) + '</td>'
                    '<td style="color:#6a6a5a">—</td>'
                    '<td class="rate">' + method + '</td>'
                    '<td style="color:#3a5a10;font-size:10px">#' + format(pc['block'], ',') + '</td>'
                    '</tr>'
                )
        if not sess_rows:
            sess_rows = '<tr><td colspan="6" style="color:#3a5a20;padding:10px;font-style:italic">No reward claims captured yet — daemon will catch them automatically</td></tr>'

        # Daily activity rows — built from pulse_ledger grouped by day
        # Each row gets data-type so filter buttons work
        daily_rows = ''
        if w.get('pulse_claims') or w.get('pulse_fee_entries'):
            from collections import defaultdict
            day_buckets = defaultdict(lambda: {'rewards': 0.0, 'hb_fees': 0.0, 'hb_count': 0, 'xfer_fees': 0.0})
            # Rewards from pulse_claims (compute_reward IN entries)
            for entry in (w['pulse_claims'] or []):
                raw_day = (entry['ts'] or '')[:10]
                if not raw_day:
                    continue
                amt = int(entry['amount_raw'] or 0) / (10 ** DECIMALS)
                day_buckets[raw_day]['rewards'] += amt
            # Heartbeat and transfer fees from pulse_fee_entries
            for entry in (w['pulse_fee_entries'] or []):
                raw_day = (entry['ts'] or '')[:10]
                if not raw_day:
                    continue
                amt = int(entry['amount_raw'] or 0) / (10 ** DECIMALS)
                method = entry['extrinsic_method'] or ''
                if method == 'heartbeatWithMetrics':
                    day_buckets[raw_day]['hb_fees'] += amt
                    day_buckets[raw_day]['hb_count'] += 1
                else:
                    day_buckets[raw_day]['xfer_fees'] += amt

            for day in sorted(day_buckets.keys(), reverse=True):
                b = day_buckets[day]
                if b['rewards'] > 0:
                    daily_rows += (
                        '<tr data-type="reward">'
                        '<td style="color:#6a9a40">' + day + '</td>'
                        '<td class="in">+' + fmt(b['rewards'], 4) + ' ACU</td>'
                        '<td class="rate">Reward</td>'
                        '<td style="color:#3a8a40">' + usd(b['rewards']) + '</td>'
                        '</tr>'
                    )
                if b['hb_count'] > 0:
                    daily_rows += (
                        '<tr data-type="hb">'
                        '<td style="color:#6a9a40">' + day + '</td>'
                        '<td class="out-hb">&minus;' + fmt(b['hb_fees'], 6) + ' ACU</td>'
                        '<td class="rate">' + str(b['hb_count']) + '&times; Heartbeat</td>'
                        '<td style="color:#8a6a20">' + usd(b['hb_fees']) + '</td>'
                        '</tr>'
                    )
                if b['xfer_fees'] > 0:
                    daily_rows += (
                        '<tr data-type="fee">'
                        '<td style="color:#6a9a40">' + day + '</td>'
                        '<td class="out-fee">&minus;' + fmt(b['xfer_fees'], 6) + ' ACU</td>'
                        '<td class="rate">Transfer Fee</td>'
                        '<td style="color:#8a4a20">' + usd(b['xfer_fees']) + '</td>'
                        '</tr>'
                    )
        if not daily_rows:
            daily_rows = '<tr><td colspan="4" style="color:#3a5a20;padding:10px;font-style:italic">No transactions yet</td></tr>'

        return (
            '<div class="block">'
            '<div class="block-title">Claim History</div>'
            '<table class="htbl" style="margin-bottom:16px">'
            '<thead><tr>'
            '<th>Date</th><th>Amount</th><th>USD</th><th>Days Since Last</th><th>Daily Rate Est.</th><th>Block</th>'
            '</tr></thead>'
            '<tbody>' + sess_rows + '</tbody>'
            '</table>'

            '<div class="block-title" style="margin-top:12px">'
            '<span>Daily Activity</span>'
            '<div style="display:flex;gap:6px">'
            '<button class="fbtn active" id="f-all-' + wid + '" onclick="filterTx(\'' + wid + '\',\'all\',this)">All</button>'
            '<button class="fbtn" id="f-reward-' + wid + '" onclick="filterTx(\'' + wid + '\',\'reward\',this)">Rewards</button>'
            '<button class="fbtn" id="f-hb-' + wid + '" onclick="filterTx(\'' + wid + '\',\'hb\',this)">Heartbeat</button>'
            '<button class="fbtn" id="f-fee-' + wid + '" onclick="filterTx(\'' + wid + '\',\'fee\',this)">Fees</button>'
            '</div></div>'
            '<table class="htbl" id="daily-tbl-' + wid + '">'
            '<thead><tr>'
            '<th>Date</th><th>Amount</th><th>Method</th><th>USD</th>'
            '</tr></thead>'
            '<tbody>' + daily_rows + '</tbody>'
            '</table>'
            '<div class="section-note">Reward claims auto-detected from Rewards Pallet &nbsp;&middot;&nbsp; '
            'Daily rate improves accuracy with each claim session &nbsp;&middot;&nbsp; '
            'Heartbeat fees grouped per day</div>'
            '</div>'
        )

    def wallet_pane(w):
        wid   = w['label'].replace(' ', '_')
        days  = [r['day'] for r in w['bal_history']]
        bals  = [round(r['avg_free'] / (10 ** DECIMALS), 4) for r in w['bal_history']]

        cells = (
            stat_cell('c-balance', 'Wallet Balance',
                      format(w['wallet_total'], ',.4f'),
                      usd(w['wallet_total']),
                      'Total ACU in this wallet on-chain (System.Account free field). Includes both locked and freely available portions.') +
            stat_cell('c-locked', 'Airdrop Lock',
                      fmt(w['airdrop_amt'], 4) if w['airdrop_amt'] > 0 else (fmt(w['manual_lock'], 4) if w['manual_lock'] > 0 else (fmt(w['reserved'], 4) if w['reserved'] > 0 else 'None')),
                      usd(w['airdrop_amt']) if w['airdrop_amt'] > 0 else (usd(w['manual_lock']) + ' (manual entry)' if w['manual_lock'] > 0 else (usd(w['reserved']) + ' system lock' if w['reserved'] > 0 else 'No active lock')),
                      'ACU locked from cACU conversion. Cannot transfer without triggering an early unlock that reduces your conversion rate. See progress below. For Processor: includes system-reserved ACU held as processor registration deposits.',
                      lock=w['airdrop_amt'] > 0 or w['manual_lock'] > 0 or w['reserved'] > 0) +
            stat_cell('c-locked', 'Staked',
                      format(w['staked'], ',.4f'),
                      usd(w['staked']),
                      'ACU locked in staking. Earns epoch rewards every ~90 min but cannot be moved without triggering a cooldown that halves rewards until exit.',
                      lock=True) +
            stat_cell('c-free', 'Free',
                      fmt(w['free_f'], 4),
                      usd(w['free_f']),
                      'ACU available right now with no restrictions. Can be transferred to another wallet, staked with a committer, or delegated.') +
            stat_cell('c-rewards', 'Daily Avg',
                      fmt(w['daily_est'], 4) if w['daily_est'] > 0 else '—',
                      (usd(w['daily_est']) + ' / day') if w['daily_est'] > 0 else 'Need 7 days data',
                      '7-day historical average: net balance change over the last 7 days divided by 7. Based on actual on-chain balance snapshots taken every 90 minutes. Reflects real earned rewards after claims and restaking.')
        )

        return (
            '<div class="wallet-header">'
            '<div>'
            '<div class="wallet-name">' + w['label'] + '</div>'
            '<div class="wallet-addr">' + w['address'] + '</div>'
            '</div>'
            '<a class="hub-link" href="https://hub.acurast.com/portal/staking" target="_blank">Check claimable &rarr; hub.acurast.com &#x2197;</a>'
            '</div>'
            '<div class="cells">' + cells + '</div>'
            + conv_section_single(w)
            + chart_block(wid, days, bals, w['color'])
            + fee_bar(w)
            + history_block(w)
        )

    def combined_pane():
        all_bals = [round(r['avg_free'] / (10**DECIMALS), 4) for w in wallet_data for r in w['bal_history']]
        days_combined = all_days
        bals_combined = []
        for d in days_combined:
            total = sum(
                next((round(r['avg_free']/(10**DECIMALS),4) for r in w['bal_history'] if r['day']==d), 0)
                for w in wallet_data
            )
            bals_combined.append(round(total, 4))

        cells = (
            stat_cell('c-balance', 'Total Balance',
                      format(grand_free, ',.2f'),
                      usd(grand_free),
                      'Sum of Wallet Balance (free field) across all 5 wallets.') +
            stat_cell('c-locked', 'Total Airdrop Lock',
                      format(grand_airdrop, ',.2f'),
                      usd(grand_airdrop),
                      'Total ACU locked in cACU conversion across all staking wallets.',
                      lock=True) +
            stat_cell('c-locked', 'Total Staked',
                      format(grand_frozen, ',.2f'),
                      usd(grand_frozen),
                      'Total ACU locked in staking across all wallets.',
                      lock=True) +
            stat_cell('c-free', 'Total Free',
                      fmt(grand_free_f, 4),
                      usd(grand_free_f),
                      'Total freely transferable ACU across all wallets (Wallet Balance minus Staked).') +
            stat_cell('c-rewards', 'Est. Daily',
                      fmt(grand_daily, 4) if grand_daily > 0 else '—',
                      (usd(grand_daily) + ' / day') if grand_daily > 0 else 'Need 2+ claims',
                      'Combined estimated daily rewards across all wallets.')
        )

        # Per-wallet summary table replacing combined chart + broken fee cells
        summary_rows = ''
        grand_7d = 0.0
        for w in wallet_data:
            try:
                gain_7d = round(w['daily_est'] * 7, 4)
            except Exception:
                gain_7d = 0.0
            grand_7d += gain_7d
            daily = w['daily_est']
            summary_rows += (
                '<tr>'
                '<td style="color:' + w['color'] + ';font-weight:bold">' + w['label'] + '</td>'
                '<td style="color:#c8f135">' + format(w['wallet_total'], ',.4f') + '</td>'
                '<td style="color:#6a9a40">' + usd(w['wallet_total']) + '</td>'
                '<td class="in">+' + fmt(gain_7d, 4) + '</td>'
                '<td style="color:#c8f135">' + fmt(daily, 4) + '</td>'
                '<td style="color:#6a9a40">' + fmt(daily * 30, 2) + '</td>'
                '<td style="color:#6a9a40">' + fmt(daily * 365, 2) + '</td>'
                '</tr>'
            )

        summary_table = (
            '<div class="block">'
            '<div class="block-title">Per-Wallet Summary</div>'
            '<table class="htbl">'
            '<thead><tr>'
            '<th>Wallet</th>'
            '<th>Balance</th>'
            '<th>USD</th>'
            '<th>7-Day Gain</th>'
            '<th>Daily Avg</th>'
            '<th>Est. Monthly</th>'
            '<th>Est. Yearly</th>'
            '</tr></thead>'
            '<tbody>' + summary_rows + '</tbody>'
            '<tfoot><tr>'
            '<td style="color:#c8f135;font-weight:bold">TOTAL</td>'
            '<td style="color:#c8f135">' + format(grand_free, ',.4f') + '</td>'
            '<td style="color:#6a9a40">' + usd(grand_free) + '</td>'
            '<td class="in">+' + fmt(grand_7d, 4) + '</td>'
            '<td style="color:#c8f135">' + fmt(grand_daily, 4) + '</td>'
            '<td style="color:#6a9a40">' + fmt(grand_daily * 30, 2) + '</td>'
            '<td style="color:#6a9a40">' + fmt(grand_daily * 365, 2) + '</td>'
            '</tr></tfoot>'
            '</table>'
            '</div>'
        )

        return (
            '<div class="wallet-header">'
            '<div><div class="wallet-name">All Wallets</div>'
            '<div class="wallet-addr">5 wallets tracked on Acurast Mainnet</div></div>'
            '<a class="hub-link" href="https://hub.acurast.com/portal/staking" target="_blank">hub.acurast.com &#x2197;</a>'
            '</div>'
            '<div class="cells">' + cells + '</div>'
            + conv_section_combined()
            + summary_table
        )

    # Build tabs
    tab_btns  = '<button class="tab" onclick="showTab(\'Combined\',this)" title="All 5 wallets">Combined</button>'
    tab_panes = '<div id="pane-Combined" class="tab-pane" style="display:none">' + combined_pane() + '</div>'
    for w in wallet_data:
        wid       = w['label'].replace(' ', '_')
        tab_btns += (
            '<button class="tab" onclick="showTab(\'' + wid + '\',this)"'
            ' title="' + w['address'] + '"'
            ' style="border-color:' + w['color'] + '44">' + w['label'] + '</button>'
        )
        tab_panes += '<div id="pane-' + wid + '" class="tab-pane" style="display:none">' + wallet_pane(w) + '</div>'

    css = '''
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'JetBrains Mono', monospace; background: #1a1c18; color: #e8e8e0; min-height: 100vh; font-size: 20px; }
a { color: #c8f135; text-decoration: none; }
.hdr { background: #161714; border-bottom: 1px solid #1a2a0a; padding: 20px 32px; display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 10px; min-height: 110px; position: relative; }
.logo { font-family: 'Pirata One', serif; font-size: 26px; color: #c8f135; }
.logo-sub { font-size: 10px; color: #4a7a10; letter-spacing: 2px; text-transform: uppercase; margin-top: 2px; }
.hdr-right { text-align: right; }
.live-dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%; background: #c8f135; margin-right: 5px; vertical-align: middle; animation: pulse 2s infinite; }
@keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.4; } }
.hdr-status { font-size: 11px; color: #4a7a10; }
.hdr-scan { font-size: 10px; color: #2a4a08; margin-top: 3px; }
.body { padding: 16px 20px; max-width: 100%; margin: 0; }

.tab-row { display: flex; gap: 6px; margin-bottom: 12px; flex-wrap: wrap; }
.tab { background: #1c1e18; border: 1px solid #1c2e0a; color: #4a7a10; font-size: 11px; padding: 6px 14px; border-radius: 6px; cursor: pointer; letter-spacing: 1px; }
.tab:hover { color: #c8f135; border-color: #c8f13544; }
.tab.active { background: #c8f135; color: #0a0a08; font-weight: 700; border-color: #c8f135; }

.wallet-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 14px; flex-wrap: wrap; gap: 8px; }
.wallet-name { font-family: 'Pirata One', serif; font-size: 28px; color: #c8f135; }
.wallet-addr { font-size: 12px; color: #3a5a10; margin-top: 2px; word-break: break-all; max-width: 600px; }
.hub-link { font-size: 10px; color: #c8f135; border: 1px solid #c8f13544; border-radius: 4px; padding: 4px 10px; white-space: nowrap; }

.cells { display: grid; grid-template-columns: repeat(5, 1fr); gap: 10px; margin-bottom: 12px; }
@media(max-width: 800px) { .cells { grid-template-columns: repeat(3, 1fr); } }
.cell { border-radius: 8px; padding: 16px 12px; text-align: center; cursor: help; border: 1px solid; }
.cell-lbl { font-size: 11px; letter-spacing: 1px; text-transform: uppercase; margin-bottom: 6px; }
.cell-val { font-size: 16px; font-weight: 700; line-height: 1.2; }
.cell-sub { font-size: 11px; margin-top: 5px; }
.c-balance { background: #192a0e; border-color: #4a7a10; }
.c-balance .cell-lbl { color: #4a7a10; } .c-balance .cell-val { color: #c8f135; } .c-balance .cell-sub { color: #6aaa20; }
.c-locked { background: #261010; border-color: #7a1010; }
.c-locked .cell-lbl { color: #9a3030; } .c-locked .cell-val { color: #ff6b6b; } .c-locked .cell-sub { color: #7a3030; }
.c-free { background: #152010; border-color: #2a5a10; }
.c-free .cell-lbl { color: #4a9a30; } .c-free .cell-val { color: #7fff7f; } .c-free .cell-sub { color: #4a7a30; }
.c-rewards { background: #261e10; border-color: #7a6010; }
.c-rewards .cell-lbl { color: #9a8030; } .c-rewards .cell-val { color: #ffc845; } .c-rewards .cell-sub { color: #8a6a20; }

.tooltip { display: none; position: absolute; bottom: calc(100% + 8px); left: 50%; transform: translateX(-50%); background: #1c1e18; border: 1px solid #333; border-radius: 6px; padding: 8px 10px; font-size: 11px; color: #ccc; width: 200px; text-align: left; z-index: 10; line-height: 1.5; white-space: normal; }
.tooltip::after { content: ''; position: absolute; top: 100%; left: 50%; transform: translateX(-50%); border: 5px solid transparent; border-top-color: #333; }
.cell:hover .tooltip { display: block; }

.block { background: #1c1e18; border: 1px solid #1c2e0a; border-radius: 8px; padding: 14px; margin-bottom: 12px; }
.block-title { font-size: 9px; color: #4a7a10; letter-spacing: 2px; text-transform: uppercase; margin-bottom: 12px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 6px; }

.conv-track { background: #1a2a0a; border-radius: 4px; height: 16px; position: relative; overflow: hidden; margin-bottom: 4px; }
.conv-fill { height: 100%; border-radius: 4px; background: linear-gradient(90deg, #3a5a10, #c8f135); }
.conv-lbl { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; font-size: 10px; color: #e8e8e0; font-weight: 700; }
.conv-stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin-top: 10px; }
.conv-stat { text-align: center; }
.conv-stat-lbl { font-size: 9px; color: #4a7a10; letter-spacing: 1px; text-transform: uppercase; margin-bottom: 3px; }
.conv-stat-val { font-size: 13px; font-weight: 700; }
.conv-stat-sub { font-size: 9px; color: #3a5a20; margin-top: 2px; }
.conv-row { display: flex; gap: 12px; align-items: center; margin-bottom: 6px; }
.conv-name { font-size: 11px; width: 90px; flex-shrink: 0; font-weight: 700; }

.chart-range { display: flex; gap: 4px; }
.rbtn { background: #1a1c18; border: 1px solid #1c2e0a; color: #4a7a10; font-size: 10px; padding: 2px 8px; border-radius: 4px; cursor: pointer; font-family: 'Share Tech Mono', monospace; }
.rbtn:hover, .rbtn.active { background: #c8f135; color: #0a0a08; border-color: #c8f135; font-weight: 700; }

.fee-bar { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin-bottom: 12px; }
.fee-cell { background: #1e1510; border: 1px solid #3a1a0a; border-radius: 8px; padding: 10px; text-align: center; }
.fee-lbl { font-size: 9px; color: #7a4020; letter-spacing: 1px; text-transform: uppercase; margin-bottom: 4px; }
.fee-val { font-size: 14px; font-weight: 700; color: #ff9055; }
.fee-sub { font-size: 9px; color: #5a3010; margin-top: 2px; }

.htbl { width: 100%; border-collapse: collapse; font-size: 13px; margin-bottom: 4px; }
.htbl th { text-align: left; padding: 5px 8px; color: #4a7a10; font-size: 9px; letter-spacing: 1px; text-transform: uppercase; border-bottom: 1px solid #1a2a0a; font-weight: 400; }
.htbl td { padding: 6px 8px; border-bottom: 1px solid #0f150a; }
.htbl tr:hover td { background: #1c1e12; }
.in { color: #c8f135; font-weight: 700; }
.out-hb { color: #ff9055; }
.out-fee { color: #ffc845; }
.rate { color: #ffc845; }
.fbtn { background: #1a1c18; border: 1px solid #1c2e0a; color: #4a7a10; font-size: 10px; padding: 3px 10px; border-radius: 4px; cursor: pointer; font-family: 'Share Tech Mono', monospace; letter-spacing: 1px; }
.fbtn.active { background: #1a2a0a; color: #c8f135; border-color: #c8f13566; }
.section-note { font-size: 10px; color: #3a5a20; margin-top: 8px; font-style: italic; }
.ftr { background: #1e201c; border-top: 1px solid #1a2a0a; padding: 12px 24px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; margin-top: 24px; }
.ftr-txt { font-size: 10px; color: #2a4a08; letter-spacing: 1px; }
.cg-badge { display: inline-flex; align-items: center; gap: 6px; background: #1c1e18; border: 1px solid #1c2e0a; border-radius: 4px; padding: 4px 10px; font-size: 10px; color: #4a7a10; }
'''

    js = (
        'function showTab(id,btn){'
        'document.querySelectorAll(".tab-pane").forEach(function(p){p.style.display="none";});'
        'document.querySelectorAll(".tab").forEach(function(b){b.classList.remove("active");});'
        'document.getElementById("pane-"+id).style.display="block";'
        'btn.classList.add("active");}'

        'function setRange(btn,wid){'
        'var chart=window["ch_"+wid];if(!chart)return;'
        'var range=btn.textContent;'
        'var cuts={"1D":1,"1W":7,"1M":30,"3M":90,"ALL":9999};'
        'var days=cuts[range]||9999;'
        'var cut=new Date(Date.now()-days*86400000);'
        'var src=window["D_"+wid]||chart.data.labels;if(!src)return;'
        'var idx=[];src.forEach(function(d,i){if(new Date(d)>=cut)idx.push(i);});'
        'chart.data.labels=idx.map(function(i){return src[i];});'
        'chart.data.datasets.forEach(function(ds){'
        'if(!ds._orig)ds._orig=ds.data.slice();'
        'ds.data=idx.map(function(i){return ds._orig[i];});});'
        'chart.update();'
        'document.querySelectorAll("#rb-"+wid+" .rbtn").forEach(function(b){b.classList.remove("active");});'
        'btn.classList.add("active");}'

                'function filterTx(wid,type,btn){'
        'document.querySelectorAll("[id^=\'f-\'][id$=\'-"+wid+"\']").forEach(function(b){b.classList.remove("active");});'
        'btn.classList.add("active");'
        'var tbl=document.getElementById("daily-tbl-"+wid);'
        'if(!tbl)return;'
        'tbl.querySelectorAll("tbody tr").forEach(function(r){'
        'if(type==="all"){r.style.display="";}'
        'else{r.style.display=(r.getAttribute("data-type")===type)?"":"none";}});}'

        'function setRange(btn,wid){'
        'var chart=window["ch_"+wid];if(!chart)return;'
        'var range=btn.textContent;'
        'var cuts={"1D":1,"1W":7,"1M":30,"3M":90,"ALL":9999};'
        'var days=cuts[range]||9999;'
        'var cut=new Date(Date.now()-days*86400000);'
        'var src=window["D_"+wid]||chart.data.labels;if(!src)return;'
        'var idx=[];src.forEach(function(d,i){if(new Date(d)>=cut)idx.push(i);});'
        'chart.data.labels=idx.map(function(i){return src[i];});'
        'chart.data.datasets.forEach(function(ds){'
        'if(!ds._orig)ds._orig=ds.data.slice();'
        'ds.data=idx.map(function(i){return ds._orig[i];});});'
        'chart.update();'
        'document.querySelectorAll("#rb-"+wid+" .rbtn").forEach(function(b){b.classList.remove("active");});'
        'btn.classList.add("active");}'


        '(function(){'
        'var s=' + str(POLL_SECS) + ';'
        'setInterval(function(){'
        's=Math.max(0,s-1);'
        'var el=document.getElementById("cntd");'
        'if(el)el.textContent=Math.floor(s/60)+":"+(s%60<10?"0":"")+s%60;'
        '},1000);})();'

        'document.querySelector(".tab[onclick*=\'Processor\']").click();'
    )

    html = (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n'
        '<meta charset="UTF-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        '<meta http-equiv="refresh" content="300">\n'
        '<title>Acurast Dashboard - Blackshirt Crypto</title>\n'
        '<link href="https://fonts.googleapis.com/css2?family=UnifrakturMaguntia&family=Share+Tech+Mono&family=Rajdhani:wght@400;600;700&display=swap" rel="stylesheet">\n'
        '<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>\n'
        '<style>' + css + '</style>\n'
        '</head>\n<body>\n'
        '<div class="hdr" style="display:flex;align-items:center;justify-content:space-between">\n'
        '  <div style="display:flex;align-items:center;gap:16px">'
        '<img src="data:image/jpeg;base64,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" style="height:110px;width:auto;flex-shrink:0" alt="Blackshirt Crypto">'
        '<div style="position:absolute;left:50%;transform:translateX(-50%);text-align:center">'
        '<div class="logo" style="font-size:48px">Acurast Tracker</div>'
        '<div class="logo-sub" style="font-size:14px">Blackshirt Crypto &nbsp;&middot;&nbsp; Mainnet Rewards</div>'
        '</div>\n'
        '</div>\n'
        '  <div class="hdr-right">'
        '<div class="hdr-status"><span class="live-dot"></span>Acurast Mainnet &nbsp;|&nbsp; ACU: <span style="color:#c8f135;font-size:18px">$' + '%.6f' % price + '</span></div>'
        '<div class="hdr-scan">Last scan: ' + last_scan + ' UTC &nbsp;|&nbsp; Next in: <span id="cntd" style="color:#c8f135">' + str(POLL_SECS // 60) + ':00</span></div>'
        '</div>\n</div>\n'
        '<div class="body">\n'
        '<div class="tab-row">' + tab_btns + '</div>\n'
        '<div>' + tab_panes + '</div>\n'
        '</div>\n'
        '<div class="ftr">'
        '<div class="ftr-txt"><a href="https://blkshirtpool.com" target="_blank">blkshirtpool.com</a>'
        ' &nbsp;|&nbsp; Acurast Mainnet &nbsp;|&nbsp; UTC day boundaries &nbsp;|&nbsp; 90 min scans</div>'
        '<div class="cg-badge">Price: <a href="https://www.coingecko.com/en/coins/acurast" target="_blank">CoinGecko</a>'
        ' &nbsp;|&nbsp; $' + '%.6f' % price + ' &nbsp;|&nbsp; ' + last_scan + ' UTC</div>'
        '</div>\n'
        '<script>' + js + '</script>\n'
        '</body>\n</html>\n'
    )

    with open(HTML_PATH, 'w') as f:
        f.write(html)
    log.info('Dashboard written -> %s', HTML_PATH)
    return html

# ── HTTP Server ───────────────────────────────────────────────────────────────
class DashHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        try:
            html = generate_dashboard(get_acu_price())
            status = 200
        except Exception as e:
            log.exception('Render error: %s', e)
            html = ('<html><body style="background:#1a1c18;color:#c8f135;font-family:monospace;padding:40px">'
                    '<h2>Dashboard temporarily unavailable</h2>'
                    '<p>The page failed to render. If the daemon just started, wait for the first scan to finish and refresh.</p>'
                    '<p style="color:#888">Details are in the daemon log (e.g. <code>pm2 logs acurast-dashboard</code>).</p>'
                    '</body></html>')
            status = 500
        self.send_response(status)
        self.send_header('Content-type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(html.encode('utf-8'))
    def log_message(self, *args):
        pass

def start_server():
    server = HTTPServer((DASHBOARD_HOST, DASHBOARD_PORT), DashHandler)
    log.info('Dashboard -> http://%s:%d', DASHBOARD_HOST, DASHBOARD_PORT)
    server.serve_forever()

# ── Entry ─────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    db_init()
    threading.Thread(target=start_server, daemon=True).start()
    log.info('Acurast Daemon v2 - Blackshirt Crypto')
    log.info('Wallets: %s', list(WALLETS.keys()))
    log.info('Dashboard: http://%s:%d', DASHBOARD_HOST, DASHBOARD_PORT)
    while True:
        try:
            scan_once()
        except Exception as e:
            log.error('Scan loop: %s', e)
        log.info('Sleeping %d min...', POLL_SECS // 60)
        time.sleep(POLL_SECS)
