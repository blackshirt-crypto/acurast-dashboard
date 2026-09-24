#!/usr/bin/env python3
"""
One-time full Pulse ledger backfill — run overnight.
Conservative rate limiting to avoid Pulse rate limits.
"""
import sqlite3, urllib.request, json, time, logging
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
log = logging.getLogger()

from config import WALLETS as CFG_WALLETS, DB_PATH as DB   # settings come from config.py
PULSE    = 'https://www.acurastpulse.com/api'
DELAY    = 5    # seconds between pages
WALLET_DELAY = 30  # seconds between wallets
LIMIT    = 50
RETRIES  = 5

WALLETS = {label: {'address': c['address'],
                   'max_entries': 500 if c.get('processor') else 200}
           for label, c in CFG_WALLETS.items()}

def fetch(url, retries=RETRIES):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': 'BlackshirtCrypto/2.0',
                'Accept': 'application/json'
            })
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read())
                if data.get('ok') is False:
                    log.warning('API ok=false: %s', url)
                    return None
                return data
        except Exception as e:
            wait = 30 * (attempt + 1)
            log.warning('Attempt %d failed: %s — waiting %ds', attempt+1, e, wait)
            time.sleep(wait)
    return None

def insert_entry(conn, label, e):
    try:
        conn.execute(
            'INSERT OR IGNORE INTO pulse_ledger '
            '(pulse_id,wallet,block_number,ts,direction,amount_raw,section,method,'
            'extrinsic_section,extrinsic_method,counterpart,classification,fee_raw,'
            'success,acu_price_usd) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (
                str(e['id']), label, int(e['block_number']),
                e.get('block_timestamp',''), e.get('direction',''),
                str(e.get('amount',0) or 0), e.get('section'), e.get('method'),
                e.get('extrinsic_section'), e.get('extrinsic_method'),
                e.get('counterparty_account_id'), e.get('wallet_classification'),
                str(e.get('fee',0) or 0), 1 if e.get('success') else 0,
                e.get('acuPriceUsdAtTx')
            )
        )
        return True
    except Exception as ex:
        log.debug('Insert %s: %s', e.get('id'), ex)
        return False

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

for label, cfg in WALLETS.items():
    address = cfg['address']
    max_entries = cfg['max_entries']
    log.info('=== %s === (max_entries: %s)', label, max_entries)

    # Get total entries and current count to figure out where to start
    current_count = conn.execute(
        'SELECT COUNT(*) FROM pulse_ledger WHERE wallet=?', (label,)
    ).fetchone()[0]
    log.info('%s: currently have %d entries in DB', label, current_count)

    # First get ledgerTotal
    url = f'{PULSE}/accounts/{address}/ledger?limit=1&offset=0'
    data = fetch(url)
    if not data:
        log.error('%s: cannot get ledgerTotal, skipping', label)
        time.sleep(WALLET_DELAY)
        continue
    ledger_total = data.get('ledgerTotal', 0)
    log.info('%s: ledgerTotal = %d, we have %d, need %d more',
             label, ledger_total, current_count, max(0, ledger_total - current_count))

    if current_count >= ledger_total and max_entries is None:
        log.info('%s: already fully backfilled, skipping', label)
        time.sleep(5)
        continue

    # Processor: only fetch first max_entries (most recent)
    # SubWallets: fetch all from where we left off
    if max_entries is not None:
        offset = 0  # Always start from most recent for Processor
    else:
        offset = current_count
    total = 0
    cap = max_entries or ledger_total

    while True:
        url = f'{PULSE}/accounts/{address}/ledger?limit={LIMIT}&offset={offset}'
        data = fetch(url)
        if not data:
            log.error('%s: failed at offset %d, stopping', label, offset)
            break

        entries = data.get('ledger', [])
        if not entries:
            log.info('%s: no more entries at offset %d', label, offset)
            break

        page_new = 0
        for e in entries:
            if insert_entry(conn, label, e):
                page_new += 1
                total += 1

        conn.commit()
        pct = round((offset + len(entries)) / max(cap, 1) * 100, 1)
        log.info('%s: offset %d — %d new | %d total inserted | %s%% done',
                 label, offset, page_new, total, pct)

        if not data.get('ledgerHasMore'):
            break
        if max_entries and total >= max_entries:
            log.info('%s: reached max_entries cap of %d', label, max_entries)
            break

        offset += LIMIT
        time.sleep(DELAY)

    # Rebuild rewards for this wallet
    conn.execute("DELETE FROM pulse_rewards WHERE wallet=?", (label,))
    rows = conn.execute(
        "SELECT substr(ts,1,10) as day, SUM(CAST(amount_raw AS REAL)) as total,"
        " COUNT(*) as cnt FROM pulse_ledger"
        " WHERE wallet=? AND classification='compute_reward' AND direction='in'"
        " GROUP BY substr(ts,1,10)", (label,)
    ).fetchall()
    for r in rows:
        conn.execute(
            'INSERT OR REPLACE INTO pulse_rewards(wallet,day,reward_raw,claim_count)'
            ' VALUES(?,?,?,?)',
            (label, r['day'], str(int(r['total'])), r['cnt'])
        )
    conn.commit()
    log.info('%s: complete — %d entries total in DB, %d reward days',
             label,
             conn.execute('SELECT COUNT(*) FROM pulse_ledger WHERE wallet=?',
                         (label,)).fetchone()[0],
             len(rows))

    time.sleep(WALLET_DELAY)

conn.close()
log.info('Backfill complete!')
