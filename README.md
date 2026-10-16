# Acurast Dashboard

A self-hosted, personal dashboard for **Acurast (ACU)** wallets and processor fleets.
It watches your wallet addresses, records balances and reward history in a local SQLite database, and serves a live dashboard in your browser.

Built by [Blackshirt Crypto](https://blkshirtpool.com).

> **Read-only & safe:** the dashboard only needs your **public** wallet addresses.
> It never asks for, stores, or uses private keys or seed phrases. **Never put a seed phrase in any config file.**

## Features

- Balance breakdown per wallet: free, staked, airdrop lock, USD value
- Reward/claim history grouped by day, with daily averages and monthly/yearly estimates
- Daily activity log with filters (rewards, heartbeat fees, transfer fees)
- Processor manager support: fleet snapshots and real fee data
- Combined view across all your wallets
- Live ACU price (CoinGecko), refreshed every 5 minutes; full chain scan every 90 minutes

Data sources: the [Acurast](https://acurast.com) public RPC, the [Acurast Pulse](https://www.acurastpulse.com) API, and [CoinGecko](https://www.coingecko.com) for the ACU price. See [Credits](#credits).

## Requirements

- Linux or macOS (any always-on machine or VPS works)
- Python **3.10+** with `pip` and `venv`
- Optional: [PM2](https://pm2.keymetrics.io/) to keep it running 24/7

## Setup

### Step 1 — Download the code

Open a terminal and run:

```bash
git clone https://github.com/blackshirt-crypto/acurast-dashboard.git
cd acurast-dashboard
```

This downloads the dashboard into a folder called `acurast-dashboard` and moves you into it.

### Step 2 — Install dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

What these do:
- **Line 1** creates an isolated Python environment so nothing conflicts with your system.
- **Line 2** activates it. Your terminal prompt will change to show `(venv)` at the beginning.
- **Line 3** installs the one library the dashboard needs (`substrate-interface`, which talks to the Acurast chain).

### Step 3 — Create your config

Open **[setup.html](setup.html)** in your browser — just double-click the file inside the `acurast-dashboard` folder. The wizard walks you through:

1. Entering your Processor wallet address (with a link to find it on the [Acurast Hub](https://hub.acurast.com))
2. Your Manager ID (with a link to find it on [Acurast Pulse](https://www.acurastpulse.com))
3. Adding any extra wallets (staking, spending, etc.)
4. Picking colors for each tab
5. Choosing whether you're running locally or on a VPS

When you're done, click **Download config.py** and save the file into the `acurast-dashboard` folder.

**Prefer to skip the wizard?** You can do it manually instead:
```bash
cp config.example.py config.py
nano config.py
```

### Step 4 — Start the dashboard

Make sure your terminal still shows `(venv)` at the prompt. If it doesn't, run `source venv/bin/activate` first. Then:

```bash
python3 acurast_daemon_v2.py
```

That one command does everything: creates the database, connects to the Acurast chain, scans your wallets for rewards, and starts the web server.

### Step 5 — Open the dashboard in your browser

Go to the address that matches your setup:

- **Running on your own machine (laptop/desktop):** open [http://127.0.0.1:8888](http://127.0.0.1:8888)
- **Running on a VPS with Tailscale/VPN:** open `http://YOUR-TAILSCALE-IP:8888` — the exact URL is saved as a comment at the bottom of your `config.py`

The first scan takes a few minutes while it reads the chain. If the page says "temporarily unavailable", the scan is still running — wait a minute and refresh. After that, it rescans automatically every 90 minutes.

## Run 24/7 with PM2

The dashboard only runs while your terminal is open. To keep it running in the background (and restart automatically after reboots), use PM2:

```bash
npm install -g pm2          # install PM2 (requires Node.js)
pm2 start acurast_daemon_v2.py --name acurast-dashboard --interpreter ./venv/bin/python3
pm2 save
pm2 startup                 # follow the printed command so it starts on reboot
```

Useful commands:
- `pm2 logs acurast-dashboard` — see what the dashboard is doing
- `pm2 restart acurast-dashboard` — restart after changing `config.py`
- `pm2 stop acurast-dashboard` — stop the dashboard

## Viewing from another device

If the dashboard runs on a VPS or headless machine, **don't** just open the port to the internet. Pick one of these:

- **SSH tunnel (simplest):** on your own computer run `ssh -L 8888:127.0.0.1:8888 youruser@your-server`, then open http://127.0.0.1:8888.
- **Private VPN such as Tailscale:** the setup wizard sets `DASHBOARD_HOST = '0.0.0.0'` for you when you pick the VPS option. Then use a firewall so the port is only reachable over the VPN. For example with UFW: `sudo ufw allow in on tailscale0 to any port 8888`.

## Backfill older history

The dashboard records new rewards going forward from the moment you start it. To pull in older reward history from Acurast Pulse, run this once:

```bash
python3 backfill_pulse.py
```

It runs slowly on purpose (pausing between requests) to be polite to the Pulse API.

## Updating

```bash
git pull
pm2 restart acurast-dashboard
```

Your `config.py` and database are untouched by updates.

## Configuration reference

The setup wizard (`setup.html`) handles all of this for you. These settings are documented here in case you want to edit `config.py` by hand.

| Setting | What it does |
|---|---|
| `WALLETS` | Your wallets: a label (tab name), your public address (starts with `5...`), and a color. Add as many as you like. |
| `'processor': True` | Add this to your **Processor manager** wallet to enable fleet and fee features. Leave it off normal wallets. |
| `'manual_lock': 123.45` | Optional, per wallet. If the [Acurast Hub](https://hub.acurast.com) shows a "Locked by Airdrop" amount that the dashboard can't read from the chain, enter it here. It shows as a manual entry and gets added to the wallet total. Update or remove it when the amount changes. |
| `MANAGER_ID` | Your processor manager ID from [Acurast Pulse](https://www.acurastpulse.com), or `None` if you don't run a processor fleet. |
| `DB_PATH` | Where the SQLite database is stored (default: next to the script). |
| `DASHBOARD_HOST` | `127.0.0.1` = only this machine (safest). `0.0.0.0` = accessible over your network/VPN. |
| `DASHBOARD_PORT` | Port for the dashboard (default `8888`). |

`config.py` is in `.gitignore`, so your addresses are never committed if you fork this repo. If you edit it while the dashboard is running, restart with `pm2 restart acurast-dashboard`.

## Credits

This dashboard wouldn't exist without these projects:

- **[Acurast](https://acurast.com)** — the decentralized compute network this tracks. Manage your wallets, staking and claims on the **[Acurast Hub](https://hub.acurast.com)**.
- **[Acurast Pulse](https://www.acurastpulse.com)** — the explorer and API behind the reward history, fee data and processor fleet stats. Huge thanks to its creators for making that data available.
- **[CoinGecko](https://www.coingecko.com)** — the live ACU price.

## Disclaimer

Community tool, not affiliated with Acurast. Earnings estimates are projections from recent history, not guarantees. Use at your own risk.
