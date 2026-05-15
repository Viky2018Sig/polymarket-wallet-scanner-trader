#!/usr/bin/env bash
# =============================================================================
# Polymarket Scanner — Fresh Ubuntu Server Setup
# =============================================================================
# Tested on: Ubuntu 22.04 LTS, Ubuntu 24.04 LTS
# Run as root on a fresh Vultr (or any Ubuntu) VPS:
#
#   curl -fsSL https://raw.githubusercontent.com/Viky2018Sig/polymarket-wallet-scanner-trader/main/setup_server.sh | bash
#
# Or if you already cloned the repo:
#   bash setup_server.sh
#
# What this script does:
#   1. System update + essential packages
#   2. Python 3.11 + pip + venv
#   3. Git config helpers
#   4. Clone the repo (or skip if already cloned)
#   5. Python dependencies in a virtualenv
#   6. .env file with your Telegram credentials
#   7. Initialise the SQLite database
#   8. systemd services for auto-restart on reboot
#   9. Cron jobs (notify, scan, prune)
#  10. Log directory + logrotate config
# =============================================================================

set -euo pipefail

# ── Colour helpers ────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()    { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
success() { echo -e "${GREEN}[OK]${NC}   $*"; }

# ── Config — edit these before running ───────────────────────────────────────
REPO_URL="https://github.com/Viky2018Sig/polymarket-wallet-scanner-trader.git"
INSTALL_DIR="/root/polymarket-wallet-scanner-trader"
VENV_DIR="$INSTALL_DIR/.venv"
PYTHON="python3.11"          # will fall back to python3 if 3.11 not available

# ── 1. System update ──────────────────────────────────────────────────────────
info "Updating system packages..."
apt-get update -qq
apt-get upgrade -y -qq
apt-get install -y -qq \
    git \
    curl \
    wget \
    unzip \
    htop \
    screen \
    tmux \
    net-tools \
    iputils-ping \
    traceroute \
    dnsutils \
    ufw \
    logrotate \
    rsync \
    cron \
    software-properties-common
success "System packages installed"

# ── 2. Python 3.11 ───────────────────────────────────────────────────────────
info "Installing Python 3.11..."
if ! command -v python3.11 &>/dev/null; then
    add-apt-repository -y ppa:deadsnakes/ppa 2>/dev/null || true
    apt-get update -qq
    apt-get install -y -qq python3.11 python3.11-venv python3.11-dev
fi
apt-get install -y -qq python3-pip python3-venv 2>/dev/null || true

# Determine which python binary to use
if command -v python3.11 &>/dev/null; then
    PY_BIN="python3.11"
elif command -v python3.10 &>/dev/null; then
    PY_BIN="python3.10"
    warn "Python 3.11 not available, using 3.10"
else
    PY_BIN="python3"
    warn "Using system python3: $(python3 --version)"
fi
success "Python: $($PY_BIN --version)"

# ── 3. Clone or update repo ───────────────────────────────────────────────────
if [ -d "$INSTALL_DIR/.git" ]; then
    info "Repo already exists at $INSTALL_DIR — pulling latest..."
    git -C "$INSTALL_DIR" pull --ff-only
    success "Repo updated"
else
    info "Cloning repo to $INSTALL_DIR..."
    git clone "$REPO_URL" "$INSTALL_DIR"
    success "Repo cloned"
fi
cd "$INSTALL_DIR"

# ── 4. Python virtualenv + dependencies ──────────────────────────────────────
info "Creating Python virtualenv at $VENV_DIR..."
$PY_BIN -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"

info "Upgrading pip..."
pip install --upgrade pip --quiet

info "Installing Python dependencies..."
pip install -r requirements.txt --quiet
success "Python dependencies installed"

# Add venv python to PATH for this session
export PATH="$VENV_DIR/bin:$PATH"

# ── 5. Create wrapper scripts ─────────────────────────────────────────────────
# These let you run commands without manually activating the venv
info "Creating run wrapper scripts..."

cat > "$INSTALL_DIR/run_realtime.sh" << 'EOF'
#!/bin/bash
cd /root/polymarket-wallet-scanner-trader
source .venv/bin/activate
exec python3 main.py realtime "$@"
EOF

cat > "$INSTALL_DIR/run_fast_copier.sh" << 'EOF'
#!/bin/bash
cd /root/polymarket-wallet-scanner-trader
source .venv/bin/activate
exec python3 fast_copier.py "$@"
EOF

cat > "$INSTALL_DIR/run_notify.sh" << 'EOF'
#!/bin/bash
cd /root/polymarket-wallet-scanner-trader
source .venv/bin/activate
exec python3 main.py notify "$@"
EOF

cat > "$INSTALL_DIR/run_scan.sh" << 'EOF'
#!/bin/bash
cd /root/polymarket-wallet-scanner-trader
source .venv/bin/activate
exec python3 main.py scan "$@"
EOF

chmod +x "$INSTALL_DIR"/*.sh
success "Wrapper scripts created"

# ── 6. Log directory ──────────────────────────────────────────────────────────
info "Creating logs directory..."
mkdir -p "$INSTALL_DIR/logs"
success "logs/ directory ready"

# ── 7. .env file ──────────────────────────────────────────────────────────────
if [ ! -f "$INSTALL_DIR/.env" ]; then
    info "Creating .env from template..."
    cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
    warn ""
    warn "  *** ACTION REQUIRED ***"
    warn "  Edit $INSTALL_DIR/.env and set:"
    warn "    TELEGRAM_BOT_TOKEN=your_bot_token"
    warn "    TELEGRAM_CHAT_ID=your_chat_id"
    warn "    STARTING_BANKROLL=2000"
    warn ""
else
    info ".env already exists — skipping (not overwriting)"
fi

# ── 8. Initialise SQLite database ─────────────────────────────────────────────
if [ ! -f "$INSTALL_DIR/polymarket_scanner.db" ]; then
    info "Initialising SQLite database..."
    source "$VENV_DIR/bin/activate"
    cd "$INSTALL_DIR"
    python3 main.py init-db
    success "Database initialised"
else
    info "Database already exists — skipping init"
fi

# ── 9. Verify latency to Polymarket ──────────────────────────────────────────
info "Measuring RTT to Polymarket CLOB (clob.polymarket.com)..."
python3 - << 'PYEOF'
import urllib.request, time, statistics
times = []
for _ in range(5):
    t = time.time()
    try:
        urllib.request.urlopen("https://clob.polymarket.com/health", timeout=10)
    except Exception:
        pass
    times.append((time.time() - t) * 1000)
avg = statistics.mean(times)
print(f"  CLOB avg RTT: {avg:.0f}ms  (NJ Vultr target: 10-20ms | Brazil: ~274ms)")
if avg < 50:
    print("  ✅ Excellent latency — ideal for FastCopier")
elif avg < 100:
    print("  ⚠️  Moderate latency — FastCopier will work but may miss fastest markets")
else:
    print("  ❌ High latency — consider migrating to Vultr New Jersey for 10x improvement")
PYEOF

# ── 10. systemd services ──────────────────────────────────────────────────────
info "Creating systemd service: polymarket-realtime..."
cat > /etc/systemd/system/polymarket-realtime.service << EOF
[Unit]
Description=Polymarket Realtime Monitor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=$INSTALL_DIR
ExecStart=$VENV_DIR/bin/python3 main.py realtime
Restart=always
RestartSec=15
StandardOutput=append:$INSTALL_DIR/logs/realtime.log
StandardError=append:$INSTALL_DIR/logs/realtime.log
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

info "Creating systemd service: polymarket-fast-copier..."
cat > /etc/systemd/system/polymarket-fast-copier.service << EOF
[Unit]
Description=Polymarket FastCopier (sub-second mode)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=$INSTALL_DIR
ExecStart=$VENV_DIR/bin/python3 fast_copier.py --bankroll 2000 --max-price 0.20
Restart=always
RestartSec=15
StandardOutput=append:$INSTALL_DIR/logs/fast_copier.log
StandardError=append:$INSTALL_DIR/logs/fast_copier.log
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
# Enable but don't start yet — user needs to configure .env first
systemctl enable polymarket-realtime
systemctl enable polymarket-fast-copier
success "systemd services created (not started yet — configure .env first)"

# ── 11. Cron jobs ─────────────────────────────────────────────────────────────
info "Installing cron jobs..."
CRON_FILE=$(mktemp)
# Preserve any existing crontab
crontab -l 2>/dev/null > "$CRON_FILE" || true

# Only add if not already present
if ! grep -q "polymarket-wallet-scanner" "$CRON_FILE" 2>/dev/null; then
    cat >> "$CRON_FILE" << EOF

# Polymarket Scanner — added by setup_server.sh
7 * * * *    cd $INSTALL_DIR && $VENV_DIR/bin/python3 main.py notify >> logs/notify.log 2>&1
23 */4 * * * cd $INSTALL_DIR && $VENV_DIR/bin/python3 main.py scan --skip-discovery >> logs/scan.log 2>&1
47 4 * * *   cd $INSTALL_DIR && $VENV_DIR/bin/python3 main.py prune >> logs/prune.log 2>&1
EOF
    crontab "$CRON_FILE"
    success "Cron jobs installed"
else
    info "Cron jobs already present — skipping"
fi
rm -f "$CRON_FILE"
service cron restart 2>/dev/null || systemctl restart cron 2>/dev/null || true

# ── 12. Logrotate config ──────────────────────────────────────────────────────
info "Configuring logrotate..."
cat > /etc/logrotate.d/polymarket << EOF
$INSTALL_DIR/logs/*.log {
    daily
    rotate 7
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
EOF
success "Logrotate configured"

# ── 13. Firewall (optional but recommended) ───────────────────────────────────
info "Configuring UFW firewall..."
ufw --force reset 2>/dev/null || true
ufw default deny incoming
ufw default allow outgoing
ufw allow ssh
ufw --force enable
success "Firewall: SSH allowed, all other inbound blocked"

# ── 14. Final summary ─────────────────────────────────────────────────────────
echo ""
echo "=================================================================="
echo -e "${GREEN}  Setup complete!${NC}"
echo "=================================================================="
echo ""
echo "  Install directory : $INSTALL_DIR"
echo "  Python venv       : $VENV_DIR"
echo "  Logs              : $INSTALL_DIR/logs/"
echo ""
echo -e "${YELLOW}  NEXT STEPS${NC}"
echo ""
echo "  1. Edit your .env file:"
echo "       nano $INSTALL_DIR/.env"
echo "     Set: STARTING_BANKROLL, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID"
echo ""
echo "  2a. If this is a FRESH server (no existing scanner DB):"
echo "       cd $INSTALL_DIR"
echo "       source .venv/bin/activate"
echo "       python3 main.py scan --max-pages 200 --top-n 50"
echo "     (takes 1-2 hours — builds wallet history)"
echo ""
echo "  2b. If you have an existing scanner DB from another server:"
echo "       rsync -avz user@old-server:/path/to/polymarket_scanner.db \\"
echo "         $INSTALL_DIR/polymarket_scanner.db"
echo ""
echo "  3. Start the services:"
echo "       systemctl start polymarket-realtime"
echo "       systemctl start polymarket-fast-copier"
echo ""
echo "  4. Check they're running:"
echo "       systemctl status polymarket-realtime"
echo "       systemctl status polymarket-fast-copier"
echo "       tail -f $INSTALL_DIR/logs/fast_copier.log"
echo ""
echo "  5. Test Telegram:"
echo "       cd $INSTALL_DIR && source .venv/bin/activate"
echo "       python3 main.py notify"
echo ""
echo "  Useful commands:"
echo "    journalctl -u polymarket-fast-copier -f    # live service logs"
echo "    systemctl restart polymarket-fast-copier   # restart"
echo "    crontab -l                                 # view cron jobs"
echo ""
echo "=================================================================="
