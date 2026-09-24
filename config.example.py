# ============================================================================
#  Acurast Dashboard — configuration
#  1. Copy this file:   cp config.example.py config.py
#  2. Edit config.py with your own wallet addresses and settings.
#  config.py is ignored by git, so your addresses never get committed.
# ============================================================================
import os

# Your wallets. Label = name shown on the dashboard tab.
# address = your Acurast (SS58, starts with 5...) address. color = tab/chart color.
# processor = True on your Processor MANAGER wallet (enables fleet/fee features). Leave it out for normal wallets.
WALLETS = {
    'My Processor': {'address': '5YOUR_PROCESSOR_MANAGER_ADDRESS_HERE', 'color': '#c8f135', 'processor': True},
    'Wallet 2':     {'address': '5YOUR_SECOND_ADDRESS_HERE',            'color': '#f5a623'},
    # Optional per wallet: 'manual_lock': 123.45  -> a locked amount the chain query can't see
    # (e.g. Hub shows 'Locked by Airdrop' but the dashboard shows None). Remove it once unlocked.
    # add or remove lines as needed
}

# Your Processor manager ID from Acurast Pulse. Set to None if you don't run a processor fleet.
MANAGER_ID = None

# Database + generated HTML. Default = next to this file.
DB_PATH   = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'acurast_rewards.db')
HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'dashboard_v2.html')

# Web server.
# '127.0.0.1' = only this machine can open the dashboard (safest default).
# '0.0.0.0'   = any device that can reach this machine. Only use behind a firewall/VPN (e.g. Tailscale)!
DASHBOARD_HOST = '127.0.0.1'
DASHBOARD_PORT = 8888
