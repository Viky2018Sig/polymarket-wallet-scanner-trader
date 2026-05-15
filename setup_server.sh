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
#   3. Clone the repo (or update if already cloned)
#   4. Python dependencies in a virtualenv
#   5. .env file with your credentials
#   6. Initialise the SQLite database
#   7. Geo-block detection + WireGuard bypass setup
#   8. systemd services for auto-restart on reboot
#   9. Cron jobs (notify, scan, prune)
#  10. Log directory + logrotate config
#  11. UFW firewall
# =============================================================================

set -euo pipefail

# ── Colour helpers ────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERR]${NC}   $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
step()    { echo -e "\n${CYAN}━━━ $* ━━━${NC}"; }

# ── Config — edit these before running ───────────────────────────────────────
REPO_URL="https://github.com/Viky2018Sig/polymarket-wallet-scanner-trader.git"
INSTALL_DIR="/root/polymarket-wallet-scanner-trader"
VENV_DIR="$INSTALL_DIR/.venv"
WG_CONF="/etc/wireguard/wg0.conf"
WG_IFACE="wg0"

# ── 1. System update ──────────────────────────────────────────────────────────
step "System update & packages"
apt-get update -qq
apt-get upgrade -y -qq
apt-get install -y -qq \
    git curl wget unzip \
    htop screen tmux \
    net-tools iputils-ping traceroute dnsutils \
    ufw logrotate rsync cron \
    software-properties-common \
    wireguard wireguard-tools \
    openresolv \
    jq
success "System packages installed"

# ── 2. Python 3.11 ───────────────────────────────────────────────────────────
step "Python 3.11"
if ! command -v python3.11 &>/dev/null; then
    add-apt-repository -y ppa:deadsnakes/ppa 2>/dev/null || true
    apt-get update -qq
    apt-get install -y -qq python3.11 python3.11-venv python3.11-dev
fi
apt-get install -y -qq python3-pip python3-venv 2>/dev/null || true

if command -v python3.11 &>/dev/null;   then PY_BIN="python3.11"
elif command -v python3.10 &>/dev/null; then PY_BIN="python3.10"; warn "Using Python 3.10"
else PY_BIN="python3"; warn "Using system python3: $(python3 --version)"; fi
success "Python: $($PY_BIN --version)"

# ── 3. Clone or update repo ───────────────────────────────────────────────────
step "Repository"
if [ -d "$INSTALL_DIR/.git" ]; then
    info "Repo exists — pulling latest..."
    git -C "$INSTALL_DIR" pull --ff-only
else
    info "Cloning $REPO_URL..."
    git clone "$REPO_URL" "$INSTALL_DIR"
fi
success "Repo ready at $INSTALL_DIR"
cd "$INSTALL_DIR"

# ── 4. Python virtualenv + dependencies ──────────────────────────────────────
step "Python virtualenv & dependencies"
$PY_BIN -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"
pip install --upgrade pip --quiet
pip install -r requirements.txt --quiet
export PATH="$VENV_DIR/bin:$PATH"
success "Dependencies installed"

# ── 5. Wrapper shell scripts ──────────────────────────────────────────────────
step "Wrapper scripts"
for script_name in realtime fast_copier notify scan; do
    case $script_name in
        realtime)    CMD="python3 main.py realtime" ;;
        fast_copier) CMD="python3 fast_copier.py" ;;
        notify)      CMD="python3 main.py notify" ;;
        scan)        CMD="python3 main.py scan" ;;
    esac
    cat > "$INSTALL_DIR/run_${script_name}.sh" << WRAPPER
#!/bin/bash
cd $INSTALL_DIR
source .venv/bin/activate
exec $CMD "\$@"
WRAPPER
    chmod +x "$INSTALL_DIR/run_${script_name}.sh"
done
success "Wrapper scripts created (run_realtime.sh, run_fast_copier.sh, etc.)"

# ── 6. Logs directory ─────────────────────────────────────────────────────────
mkdir -p "$INSTALL_DIR/logs"

# ── 7. .env file ──────────────────────────────────────────────────────────────
step ".env configuration"
if [ ! -f "$INSTALL_DIR/.env" ]; then
    cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
    warn "ACTION REQUIRED: edit $INSTALL_DIR/.env"
    warn "  Set: STARTING_BANKROLL, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID"
else
    info ".env already exists — not overwriting"
fi

# ── 8. Initialise database ────────────────────────────────────────────────────
step "SQLite database"
if [ ! -f "$INSTALL_DIR/polymarket_scanner.db" ]; then
    python3 main.py init-db
    success "Database initialised"
else
    info "Database already exists — skipping"
fi

# =============================================================================
# ── 9. GEO-BLOCK CHECK + WIREGUARD BYPASS ────────────────────────────────────
# =============================================================================
step "Polymarket geo-block check"

# Pull our public IP and country from Polymarket's own geoblock endpoint
# Response: {"blocked": true/false, "ip": "x.x.x.x", "country": "US", "region": "NJ"}
GEO_JSON=$(curl -s --max-time 10 "https://polymarket.com/api/geoblock" 2>/dev/null || echo '{}')
GEO_BLOCKED=$(echo "$GEO_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('blocked','unknown'))" 2>/dev/null || echo "unknown")
GEO_COUNTRY=$(echo "$GEO_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('country','??'))" 2>/dev/null || echo "??")
GEO_IP=$(echo     "$GEO_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('ip','??'))"      2>/dev/null || echo "??")
GEO_REGION=$(echo "$GEO_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('region','??'))"  2>/dev/null || echo "??")

echo "  Public IP : $GEO_IP"
echo "  Country   : $GEO_COUNTRY ($GEO_REGION)"
echo "  Blocked   : $GEO_BLOCKED"

if [ "$GEO_BLOCKED" = "False" ] || [ "$GEO_BLOCKED" = "false" ]; then
    success "Not geo-blocked — trading accessible from this IP"
    NEED_BYPASS=false
else
    error "Geo-blocked! Country=$GEO_COUNTRY is restricted by Polymarket"
    warn "Setting up WireGuard bypass to route Polymarket API traffic via non-US exit..."
    NEED_BYPASS=true
fi

# ── WireGuard setup ───────────────────────────────────────────────────────────
setup_wireguard() {
    step "WireGuard VPN setup"

    # Generate local key pair
    mkdir -p /etc/wireguard
    chmod 700 /etc/wireguard

    if [ ! -f /etc/wireguard/privatekey ]; then
        wg genkey | tee /etc/wireguard/privatekey | wg pubkey > /etc/wireguard/publickey
        chmod 600 /etc/wireguard/privatekey
        info "WireGuard key pair generated"
    else
        info "WireGuard keys already exist"
    fi

    LOCAL_PRIVKEY=$(cat /etc/wireguard/privatekey)
    LOCAL_PUBKEY=$(cat  /etc/wireguard/publickey)

    # Only create template if no config exists yet
    if [ ! -f "$WG_CONF" ]; then
        cat > "$WG_CONF" << WGEOF
# ============================================================
# WireGuard config — fill in the [Peer] section below
# ============================================================
# HOW TO GET A PEER:
#
# OPTION A — Mullvad VPN (recommended, ~$5/month, non-US servers)
#   1. Go to https://mullvad.net/en/account/wireguard-config
#   2. Generate a config for any non-US server (UK, NL, DE, etc.)
#   3. Copy the [Interface] PrivateKey and [Peer] block from their config
#   4. Paste Endpoint, PublicKey, AllowedIPs below
#
# OPTION B — Your own non-US VPS (Vultr Amsterdam/Frankfurt)
#   On that VPS, install WireGuard and run:
#     wg genkey | tee /etc/wireguard/server_private | wg pubkey > /etc/wireguard/server_public
#   Then configure it as a WireGuard server and add this client's pubkey.
#
# OPTION C — ProtonVPN WireGuard
#   https://protonvpn.com/support/wireguard-manual-setup/
#   Choose any non-US server.
#
# This VPS public key (share this with your peer/server):
#   $LOCAL_PUBKEY
# ============================================================

[Interface]
PrivateKey = $LOCAL_PRIVKEY
# VPN tunnel IP — use 10.0.0.2/32 (or match what your provider assigns)
Address = 10.0.0.2/32
# Use Cloudflare DNS inside the tunnel
DNS = 1.1.1.1, 1.0.0.1

[Peer]
# Paste your peer's PublicKey here:
PublicKey = REPLACE_WITH_PEER_PUBLIC_KEY

# Paste your peer's endpoint (IP:port) here:
Endpoint = REPLACE_WITH_PEER_ENDPOINT:51820

# Route ONLY Polymarket API traffic through the VPN (split tunnel).
# This keeps WS event delivery fast (direct) while API calls exit via VPN.
#
# Polymarket Cloudflare IPs (as of 2026):
AllowedIPs = 104.18.34.205/32, 172.64.153.51/32
#
# If you want ALL traffic through the VPN instead (simpler but adds latency):
# AllowedIPs = 0.0.0.0/0
#
PersistentKeepalive = 25
WGEOF
        chmod 600 "$WG_CONF"
        warn ""
        warn "  ┌──────────────────────────────────────────────────────┐"
        warn "  │  WireGuard config created at $WG_CONF  │"
        warn "  │                                                      │"
        warn "  │  ACTION REQUIRED — edit the [Peer] section:         │"
        warn "  │    PublicKey = REPLACE_WITH_PEER_PUBLIC_KEY          │"
        warn "  │    Endpoint  = REPLACE_WITH_PEER_ENDPOINT:51820      │"
        warn "  │                                                      │"
        warn "  │  Quickest option: Mullvad VPN (https://mullvad.net)  │"
        warn "  │    $5/month, pick any UK/NL/DE server, download      │"
        warn "  │    WireGuard config, paste the [Peer] block here.    │"
        warn "  │                                                      │"
        warn "  │  This VPS public key (add to your peer/server):      │"
        warn "  │    $LOCAL_PUBKEY │"
        warn "  └──────────────────────────────────────────────────────┘"
        warn ""
    else
        info "WireGuard config already exists at $WG_CONF"
    fi

    # Enable WireGuard on boot
    systemctl enable "wg-quick@$WG_IFACE" 2>/dev/null || true

    # Only try to bring up the interface if the config looks complete
    if grep -q "REPLACE_WITH_PEER" "$WG_CONF" 2>/dev/null; then
        warn "Skipping wg-quick up — peer details not filled in yet"
        warn "After filling in the peer, run:  wg-quick up $WG_IFACE"
    else
        info "Starting WireGuard interface $WG_IFACE..."
        wg-quick up "$WG_IFACE" 2>/dev/null || warn "wg-quick up failed — check config"
        sleep 2

        # Re-check geoblock through the tunnel
        GEO2=$(curl -s --max-time 15 "https://polymarket.com/api/geoblock" 2>/dev/null || echo '{}')
        BLOCKED2=$(echo "$GEO2" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('blocked','unknown'))" 2>/dev/null)
        COUNTRY2=$(echo "$GEO2" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('country','??'))"  2>/dev/null)
        IP2=$(echo      "$GEO2" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('ip','??'))"       2>/dev/null)

        echo "  Via VPN — IP: $IP2  Country: $COUNTRY2  Blocked: $BLOCKED2"
        if [ "$BLOCKED2" = "False" ] || [ "$BLOCKED2" = "false" ]; then
            success "WireGuard active — geo-block bypassed via $COUNTRY2 exit"
        else
            warn "Still blocked via VPN exit in $COUNTRY2 — try a different server/country"
        fi
    fi
}

# ── Create geo-check script users can run any time ───────────────────────────
cat > "$INSTALL_DIR/check_geoblock.sh" << 'GEOEOF'
#!/bin/bash
# Run this any time to check if Polymarket is accessible from this server
echo "=== Polymarket Geo-Block Check ==="
RESULT=$(curl -s --max-time 10 "https://polymarket.com/api/geoblock" 2>/dev/null)
if [ -z "$RESULT" ]; then
    echo "ERROR: Could not reach polymarket.com/api/geoblock"
    exit 1
fi
echo "$RESULT" | python3 -c "
import sys, json
d = json.load(sys.stdin)
ip      = d.get('ip', '?')
country = d.get('country', '?')
region  = d.get('region', '?')
blocked = d.get('blocked', '?')
print(f'  Public IP : {ip}')
print(f'  Country   : {country} ({region})')
print(f'  Blocked   : {blocked}')
if str(blocked).lower() == 'false':
    print('')
    print('  ✅ Trading is ACCESSIBLE from this server')
else:
    print('')
    print('  ❌ Trading is BLOCKED — run: wg-quick up wg0')
    print('     (if WireGuard is configured and peer is filled in)')
"

# Also show WireGuard status if installed
if command -v wg &>/dev/null; then
    echo ""
    echo "=== WireGuard Status ==="
    wg show 2>/dev/null || echo "  WireGuard not running (wg-quick up wg0 to start)"
fi
GEOEOF
chmod +x "$INSTALL_DIR/check_geoblock.sh"
success "Geo-check script created: $INSTALL_DIR/check_geoblock.sh"

# Run WireGuard setup if blocked or preemptively (we're on a US IP)
BLOCKED_COUNTRIES=("US")
IS_BLOCKED_COUNTRY=false
for bc in "${BLOCKED_COUNTRIES[@]}"; do
    [ "$GEO_COUNTRY" = "$bc" ] && IS_BLOCKED_COUNTRY=true && break
done

if [ "$NEED_BYPASS" = "true" ] || [ "$IS_BLOCKED_COUNTRY" = "true" ]; then
    setup_wireguard
else
    # Still install WireGuard tools even if not currently blocked
    # so the user can enable it if needed without re-running the script
    info "Not currently geo-blocked — installing WireGuard tools preemptively..."
    if [ ! -f /etc/wireguard/privatekey ]; then
        mkdir -p /etc/wireguard && chmod 700 /etc/wireguard
        wg genkey | tee /etc/wireguard/privatekey | wg pubkey > /etc/wireguard/publickey
        chmod 600 /etc/wireguard/privatekey
    fi
    LOCAL_PUBKEY=$(cat /etc/wireguard/publickey)
    info "WireGuard keys ready (if needed later, run: setup_wireguard function)"
    info "This VPS public key: $LOCAL_PUBKEY"
fi

# =============================================================================
# ── 10. systemd services ──────────────────────────────────────────────────────
# =============================================================================
step "systemd services"

# Determine proxy env line for systemd (empty if no WireGuard yet)
PROXY_ENV=""
if [ -f "$WG_CONF" ] && ! grep -q "REPLACE_WITH_PEER" "$WG_CONF" 2>/dev/null; then
    # WireGuard is configured — no proxy needed (split-tunnel routes Polymarket IPs)
    PROXY_ENV=""
fi

cat > /etc/systemd/system/polymarket-realtime.service << EOF
[Unit]
Description=Polymarket Realtime Monitor
After=network-online.target wg-quick@wg0.service
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
$PROXY_ENV

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/polymarket-fast-copier.service << EOF
[Unit]
Description=Polymarket FastCopier (sub-second mode)
After=network-online.target wg-quick@wg0.service
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
$PROXY_ENV

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable polymarket-realtime
systemctl enable polymarket-fast-copier
success "systemd services created (not started — configure .env + WireGuard peer first)"

# ── 11. Cron jobs ─────────────────────────────────────────────────────────────
step "Cron jobs"
CRON_FILE=$(mktemp)
crontab -l 2>/dev/null > "$CRON_FILE" || true

if ! grep -q "polymarket-wallet-scanner" "$CRON_FILE" 2>/dev/null; then
    cat >> "$CRON_FILE" << EOF

# Polymarket Scanner — added by setup_server.sh
7 * * * *    cd $INSTALL_DIR && $VENV_DIR/bin/python3 main.py notify >> logs/notify.log 2>&1
23 */4 * * * cd $INSTALL_DIR && $VENV_DIR/bin/python3 main.py scan --skip-discovery >> logs/scan.log 2>&1
47 4 * * *   cd $INSTALL_DIR && $VENV_DIR/bin/python3 main.py prune >> logs/prune.log 2>&1
EOF
    crontab "$CRON_FILE"
    success "Cron jobs installed (notify hourly, scan every 4h, prune daily)"
else
    info "Cron jobs already present"
fi
rm -f "$CRON_FILE"
service cron restart 2>/dev/null || systemctl restart cron 2>/dev/null || true

# ── 12. Logrotate ─────────────────────────────────────────────────────────────
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
success "Logrotate configured (7 days, compressed)"

# ── 13. Firewall ──────────────────────────────────────────────────────────────
step "UFW Firewall"
ufw --force reset 2>/dev/null || true
ufw default deny incoming
ufw default allow outgoing
ufw allow ssh
# Allow WireGuard UDP
ufw allow 51820/udp
ufw --force enable
success "Firewall: SSH + WireGuard (UDP 51820) in, all outbound allowed"

# ── 14. Latency check ─────────────────────────────────────────────────────────
step "Latency verification"
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
mn  = min(times)
print(f"  CLOB RTT: avg={avg:.0f}ms  min={mn:.0f}ms")
if avg < 30:
    print("  ✅ Excellent — NJ Vultr location confirmed, FastCopier will be near real-time")
elif avg < 80:
    print("  ✅ Good — FastCopier will work well")
elif avg < 150:
    print("  ⚠️  Moderate — FastCopier will work but won't catch the very fastest markets")
else:
    print("  ❌ High latency — consider Vultr New Jersey for 10-20ms RTT")
PYEOF

# =============================================================================
# ── 15. Final summary ─────────────────────────────────────────────────────────
# =============================================================================
echo ""
echo "══════════════════════════════════════════════════════════════════"
echo -e "${GREEN}  Setup complete!${NC}"
echo "══════════════════════════════════════════════════════════════════"
echo ""
echo "  Install dir : $INSTALL_DIR"
echo "  Python venv : $VENV_DIR"
echo "  Logs        : $INSTALL_DIR/logs/"
echo ""
echo -e "${CYAN}  Geo-block status: $GEO_COUNTRY ($GEO_IP) — blocked=$GEO_BLOCKED${NC}"
echo ""

if [ "${NEED_BYPASS:-false}" = "true" ] || [ "${IS_BLOCKED_COUNTRY:-false}" = "true" ]; then
    echo -e "${YELLOW}  ⚠️  WIREGUARD PEER REQUIRED BEFORE TRADING${NC}"
    echo ""
    echo "  1. Get a non-US WireGuard peer:"
    echo "       Mullvad VPN (easiest): https://mullvad.net"
    echo "         - Create account, add device, download WireGuard config"
    echo "         - Choose UK / Netherlands / Germany server"
    echo "         - Copy [Peer] block into $WG_CONF"
    echo ""
    echo "       Your own VPS alternative: set up WireGuard on a Vultr"
    echo "       Amsterdam/Frankfurt server and peer with this machine."
    echo ""
    echo "  2. Edit the WireGuard config:"
    echo "       nano $WG_CONF"
    echo "       (replace REPLACE_WITH_PEER_* placeholders)"
    echo ""
    echo "  3. Start WireGuard:"
    echo "       wg-quick up wg0"
    echo ""
    echo "  4. Verify unblocked:"
    echo "       bash $INSTALL_DIR/check_geoblock.sh"
    echo ""
else
    echo -e "${GREEN}  ✅ No geo-block — WireGuard installed but not required right now${NC}"
    echo "  Run anytime to check: bash $INSTALL_DIR/check_geoblock.sh"
    echo ""
fi

echo -e "${YELLOW}  NEXT STEPS${NC}"
echo ""
echo "  A. Configure credentials:"
echo "       nano $INSTALL_DIR/.env"
echo "       (set STARTING_BANKROLL, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)"
echo ""
echo "  B. Get wallet data (choose one):"
echo "       # Fresh scan — takes 1-2 hours:"
echo "       cd $INSTALL_DIR && source .venv/bin/activate"
echo "       python3 main.py scan --max-pages 200 --top-n 50"
echo ""
echo "       # Faster — rsync from existing server:"
echo "       rsync -avz root@<old-vps>:$INSTALL_DIR/polymarket_scanner.db \\"
echo "         $INSTALL_DIR/polymarket_scanner.db"
echo ""
echo "  C. Start services:"
echo "       systemctl start polymarket-realtime"
echo "       systemctl start polymarket-fast-copier"
echo ""
echo "  D. Monitor:"
echo "       tail -f $INSTALL_DIR/logs/fast_copier.log"
echo "       journalctl -u polymarket-fast-copier -f"
echo "       bash $INSTALL_DIR/check_geoblock.sh"
echo ""
echo "══════════════════════════════════════════════════════════════════"
