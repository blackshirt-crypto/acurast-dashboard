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

```bash
# 1. Get the code
git clone https://github.com/blackshirt-crypto/acurast-dashboard.git
cd acurast-dashboard

# 2. Create a virtual environment and install dependencies
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 3. Create your personal config
cp config.example.py config.py
nano config.py        # add your wallet addresses (see below)

# 4. Run it
python3 acurast_daemon_v2.py
```

Open **http://127.0.0.1:8888** in a browser on the same machine.
The first scan can take a few minutes. If the page says "temporarily unavailable", wait and refresh.

## Configuration (`config.py`)

| Setting | What it does |
|---|---|
| `WALLETS` | Your wallets: a label (tab name), your public address (starts with `5...`), and a color. Add as many as you like. |
| `'processor': True` | Add this to your **Processor manager** wallet to enable fleet and fee features. Leave it off normal wallets. |
| `'manual_lock': 123.45` | Optional, per wallet. Use it if the Acurast Hub shows a "Locked by Airdrop" amount that the dashboard can't see. It's shown as a manual entry and added to the wallet total. Update or remove it when it changes. |
| `MANAGER_ID` | Your processor manager ID from Acurast Pulse, or `None` if you don't run a processor fleet. |
| `DB_PATH` | Where the SQLite database is stored (default: next to the script). |
| `DASHBOARD_HOST` | `127.0.0.1` = only this machine can view it (**default, safest**). |
| `DASHBOARD_PORT` | Port for the web page (default `8888`). |

`config.py` is listed in `.gitignore`, so your addresses are never committed if you fork this repo.

### Example `config.py`

Here's a filled-in example that uses every option. **Replace the placeholder addresses with your own public addresses.**

```python
import os

WALLETS = {
    # Processor manager wallet: fleet + fee features turned on.
    # manual_lock = an airdrop lock the Acurast Hub shows but the dashboard can't read from chain.
    'My Fleet':  {'address': '5ExampleProcessorManagerAddressXXXXXXXXXXXXXXXXX',
                  'color': '#c8f135', 'processor': True, 'manual_lock': 286.2072},

    # Staking wallets with a cACU -> ACU airdrop lock (detected automatically, no extra options needed)
    'Staking 1': {'address': '5ExampleStakingWalletNumberOneXXXXXXXXXXXXXXXXXXX', 'color': '#4fc3f7'},
    'Staking 2': {'address': '5ExampleStakingWalletNumberTwoXXXXXXXXXXXXXXXXXXX', 'color': '#ce93d8'},

    # A plain wallet: just an address and a color
    'Spending':  {'address': '5ExampleEverydaySpendingWalletXXXXXXXXXXXXXXXXXXX', 'color': '#f5a623'},
}

# Processor manager ID from Acurast Pulse. Use None if you don't run a processor fleet.
MANAGER_ID = '1234'

# Keep the defaults unless you want the database somewhere else
DB_PATH   = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'acurast_rewards.db')
HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'dashboard_v2.html')

# '127.0.0.1' = this machine only (recommended). See "Viewing it from another device" below.
DASHBOARD_HOST = '127.0.0.1'
DASHBOARD_PORT = 8888
```

Tips:
- Labels (`'My Fleet'`, `'Staking 1'`...) are just the tab names. Call them whatever you like.
- `color` is any hex color and is used for that wallet's tab and chart.
- Only one wallet normally needs `'processor': True`: the one that manages your phones.
- If you edit `config.py` while the dashboard is running, restart it: `pm2 restart acurast-dashboard`.

## Run 24/7 with PM2

```bash
npm install -g pm2          # if you don't have it
pm2 start acurast_daemon_v2.py --name acurast-dashboard --interpreter ./venv/bin/python3
pm2 save
pm2 startup                 # follow the printed command so it starts on reboot
```

Useful commands: `pm2 logs acurast-dashboard`, `pm2 restart acurast-dashboard`, `pm2 stop acurast-dashboard`.

## Viewing it from another device

If the dashboard runs on a VPS or headless machine, **don't** just open the port to the internet. Pick one of these:

- **SSH tunnel (simplest):** on your own computer run
  `ssh -L 8888:127.0.0.1:8888 youruser@your-server`
  and then open http://127.0.0.1:8888.
- **Private VPN such as Tailscale:** set `DASHBOARD_HOST = '0.0.0.0'` and use a firewall so the port is only reachable over the VPN. For example, with UFW: `sudo ufw allow in on tailscale0 to any port 8888`.

## Optional: backfill older history

The daemon records new activity going forward. To pull older reward history from Acurast Pulse, run this once:

```bash
python3 backfill_pulse.py
```

It is deliberately slow (it pauses between requests) to be polite to the Pulse API.

## Updating

```bash
git pull
pm2 restart acurast-dashboard
```

Your `config.py` and database are untouched by updates.

## Credits

This dashboard wouldn't exist without these projects. Go check them out:

- **[Acurast](https://acurast.com)**: the decentralized compute network this tracks. Manage your wallets, staking and claims on the **[Acurast Hub](https://hub.acurast.com)**.
- **[Acurast Pulse](https://www.acurastpulse.com)**: the explorer and API behind the reward history, fee data and processor fleet stats. Huge thanks to its creators for making that data available.
- **[CoinGecko](https://www.coingecko.com)**: the live ACU price.

## Disclaimer

Community tool, not affiliated with Acurast. Earnings estimates are projections from recent history, not guarantees. Use at your own risk.
