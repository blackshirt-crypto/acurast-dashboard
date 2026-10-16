#!/bin/bash
# ============================================================================
#  Acurast Dashboard — One-Line Installer
#  curl -fsSL https://raw.githubusercontent.com/blackshirt-crypto/acurast-dashboard/main/install.sh | bash
# ============================================================================

set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${GREEN}[✓]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!]${NC} $1"; }
fail()  { echo -e "${RED}[✗]${NC} $1"; exit 1; }

echo ""
echo "============================================"
echo "  Acurast Dashboard — Installer"
echo "  github.com/blackshirt-crypto/acurast-dashboard"
echo "============================================"
echo ""

# ── Check Python ─────────────────────────────────────────────────────────────
PY=""
for cmd in python3.12 python3.11 python3.10 python3; do
    if command -v "$cmd" &>/dev/null; then
        ver=$("$cmd" -c "import sys; print(sys.version_info[:2])" 2>/dev/null)
        major=$("$cmd" -c "import sys; print(sys.version_info[0])" 2>/dev/null)
        minor=$("$cmd" -c "import sys; print(sys.version_info[1])" 2>/dev/null)
        if [ "$major" -ge 3 ] && [ "$minor" -ge 10 ]; then
            PY="$cmd"
            break
        fi
    fi
done

if [ -z "$PY" ]; then
    fail "Python 3.10+ is required but not found.
    Install it first:
      Ubuntu/Debian:  sudo apt install python3 python3-venv python3-pip
      macOS:          brew install python3
      Fedora:         sudo dnf install python3
    Then run this installer again."
fi
info "Found $PY ($($PY --version 2>&1))"

# ── Check venv module ────────────────────────────────────────────────────────
if ! $PY -m venv --help &>/dev/null; then
    fail "Python venv module not found.
    Install it:
      Ubuntu/Debian:  sudo apt install python3-venv
    Then run this installer again."
fi

# ── Check git ────────────────────────────────────────────────────────────────
if ! command -v git &>/dev/null; then
    fail "git is required but not found.
    Install it:
      Ubuntu/Debian:  sudo apt install git
      macOS:          xcode-select --install
    Then run this installer again."
fi
info "Found git ($(git --version))"

# ── Clone the repo ───────────────────────────────────────────────────────────
INSTALL_DIR="$HOME/acurast-dashboard"

if [ -d "$INSTALL_DIR" ]; then
    warn "Folder already exists: $INSTALL_DIR"
    echo "    Pulling latest changes..."
    cd "$INSTALL_DIR"
    git pull
else
    info "Downloading Acurast Dashboard..."
    git clone https://github.com/blackshirt-crypto/acurast-dashboard.git "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi
info "Code ready at $INSTALL_DIR"

# ── Create venv and install dependencies ─────────────────────────────────────
if [ ! -d "venv" ]; then
    info "Creating Python virtual environment..."
    $PY -m venv venv
fi

info "Installing dependencies (this may take a minute)..."
./venv/bin/pip install --quiet --upgrade pip
./venv/bin/pip install --quiet -r requirements.txt
info "Dependencies installed"

# ── Open setup.html ──────────────────────────────────────────────────────────
SETUP_FILE="$INSTALL_DIR/setup.html"

if [ ! -f "$INSTALL_DIR/config.py" ]; then
    echo ""
    echo "============================================"
    echo "  STEP 1: Create your config"
    echo "============================================"
    echo ""
    echo "  Opening setup.html in your browser now."
    echo "  Fill in your wallet addresses, then click"
    echo "  'Download config.py' and save it to:"
    echo ""
    echo "    $INSTALL_DIR/"
    echo ""

    # Try to open the browser
    if command -v xdg-open &>/dev/null; then
        xdg-open "$SETUP_FILE" 2>/dev/null &
    elif command -v open &>/dev/null; then
        open "$SETUP_FILE" 2>/dev/null &
    else
        warn "Couldn't open browser automatically."
        echo "    Open this file manually: $SETUP_FILE"
    fi

    echo "  Press Enter after you've saved config.py..."
    read -r

    if [ ! -f "$INSTALL_DIR/config.py" ]; then
        warn "config.py not found yet in $INSTALL_DIR/"
        echo "    Make sure you saved it to the right folder."
        echo "    Press Enter to check again, or Ctrl+C to quit..."
        read -r
    fi

    if [ ! -f "$INSTALL_DIR/config.py" ]; then
        fail "config.py still not found. Save it to $INSTALL_DIR/ and run the installer again."
    fi
fi
info "config.py found"

# ── Start the dashboard ─────────────────────────────────────────────────────
echo ""
echo "============================================"
echo "  STEP 2: Starting the dashboard"
echo "============================================"
echo ""

# Get the dashboard URL from config.py
DASH_URL=$(./venv/bin/python3 -c "
import sys
sys.path.insert(0, '.')
try:
    from config import DASHBOARD_HOST, DASHBOARD_PORT
    host = '127.0.0.1' if DASHBOARD_HOST == '0.0.0.0' else DASHBOARD_HOST
    print('http://{}:{}'.format(host, DASHBOARD_PORT))
except:
    print('http://127.0.0.1:8888')
" 2>/dev/null)

# Check for access URL comment in config.py
ACCESS_URL=$(grep "^# Access your dashboard at:" "$INSTALL_DIR/config.py" 2>/dev/null | sed 's/^# Access your dashboard at: //' || echo "")

if [ -n "$ACCESS_URL" ]; then
    DASH_URL="$ACCESS_URL"
fi

info "Starting the dashboard..."
echo ""
echo "  Your dashboard will be at: $DASH_URL"
echo ""
echo "  The first scan takes a few minutes. If the page says"
echo "  'temporarily unavailable', wait and refresh."
echo ""

# Start the daemon
./venv/bin/python3 acurast_daemon_v2.py &
DAEMON_PID=$!
sleep 5

# Check it's still running
if kill -0 $DAEMON_PID 2>/dev/null; then
    info "Dashboard is running (PID: $DAEMON_PID)"

    # Try to open the dashboard
    if command -v xdg-open &>/dev/null; then
        xdg-open "$DASH_URL" 2>/dev/null &
    elif command -v open &>/dev/null; then
        open "$DASH_URL" 2>/dev/null &
    else
        warn "Couldn't open browser automatically. Open this URL: $DASH_URL"
    fi
else
    fail "Dashboard failed to start. Check for errors above."
fi

echo ""
echo "============================================"
echo "  Setup complete!"
echo "============================================"
echo ""
echo "  Dashboard:  $DASH_URL"
echo "  Folder:     $INSTALL_DIR"
echo "  Stop:       kill $DAEMON_PID"
echo ""
echo "  To run 24/7 with PM2 (optional):"
echo "    npm install -g pm2"
echo "    pm2 start $INSTALL_DIR/acurast_daemon_v2.py \\"
echo "      --name acurast-dashboard \\"
echo "      --interpreter $INSTALL_DIR/venv/bin/python3"
echo "    pm2 save && pm2 startup"
echo ""
echo "  To backfill older reward history:"
echo "    cd $INSTALL_DIR && ./venv/bin/python3 backfill_pulse.py"
echo ""
