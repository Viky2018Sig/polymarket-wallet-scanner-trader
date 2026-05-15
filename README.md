# Polymarket Wallet Scanner & Paper Trader

A strategy engine that scans Polymarket wallets for proven high-RR traders, copies their low-price entries (0.01–0.20), and paper-trades them with Kelly criterion position sizing. Includes two independent monitor modes: a conservative signal pipeline with guardrails, and a sub-second FastCopier that removes the price-ceiling block to capture fast-resolving markets.

---

## Table of Contents

1. [Strategy Overview](#strategy-overview)
2. [Architecture](#architecture)
3. [FastCopier — Sub-Second Mode](#fastcopier--sub-second-mode)
4. [VPS Selection Guide (Vultr)](#vps-selection-guide-vultr)
5. [Full Server Setup (Step-by-Step)](#full-server-setup-step-by-step)
6. [Systemd Services](#systemd-services)
7. [WireGuard VPN (Live Trading)](#wireguard-vpn-live-trading)
8. [Cron Jobs](#cron-jobs)
9. [Running the System](#running-the-system)
10. [Configuration Reference](#configuration-reference)
11. [Database Schema](#database-schema)
12. [Telegram Notifications](#telegram-notifications)
13. [Troubleshooting](#troubleshooting)

---

## Strategy Overview

### Core Idea

Most retail traders focus on win rate. This system focuses on **Risk:Reward ratio**. Buying YES/NO tokens at 0.01–0.20 means:

| Entry | Payout on WIN | R:R Ratio | Win rate needed for +EV |
|-------|--------------|-----------|--------------------------|
| 0.01  | 99x          | 99:1      | > 1%                     |
| 0.05  | 19x          | 19:1      | > 5%                     |
| 0.10  | 9x           | 9:1       | > 10%                    |
| 0.20  | 4x           | 4:1       | > 20%                    |

A wallet with a 20% win rate buying at 0.08 average still has a **profit factor of 2.25+** because each winner returns ~11×.

### Wallet Selection Criteria

**Hard gates (all must pass):**

| Criterion | Threshold | Why |
|-----------|-----------|-----|
| Profit Factor | > 2.0 | Total gross profit / gross loss must be 2× |
| Low-price trade exposure | ≥ 10% of positions at 0.01–0.15 | Confirms the wallet operates in high-RR space |
| Minimum resolved trades | ≥ 20 | Prevents noise / small sample luck |
| Max drawdown | ≤ 40% | Filters wallets with extreme volatility |

**Composite scoring (ranking among qualifiers):**

| Factor | Weight | Notes |
|--------|--------|-------|
| Profit Factor (norm to 5× = 1.0) | 30% | Primary edge signal |
| Win Rate | 20% | Higher still preferred |
| Low-Price % | 20% | More focus on target range = better |
| Recency (last 30d weighted 2×) | 15% | Recent edge matters more |
| Market Diversity (HHI) | 15% | Avoids single-market flukes |

**Additional guardrails:**

1. **Negative EV guard** — signal generated only if the wallet's historical win rate for that price bucket produces positive expected value at the current entry price.
2. **Signal freshness** — signals older than 60 minutes are discarded.
3. **Auto-unfollow** — if a tracked wallet's last 10 resolved trades drop below profit factor 1.5, it is automatically unfollowed.
4. **Recency decay** — scoring weights trades in the last 30 days 2× more than older trades.
5. **Bucket-specific win rates** — Kelly sizing uses win rate specific to the price bucket (e.g., 0.01–0.05 vs 0.10–0.15), not the wallet's overall average.

---

## Architecture

The system has two runtime modes that operate independently:

```
polymarket-wallet-scanner-trader/
├── main.py                        # CLI entry point (scan, realtime, paper-trade, etc.)
├── fast_copier.py                 # FastCopier entry point (sub-second mode)
├── config.py                      # All settings (env-overridable via .env)
├── src/
│   ├── api/
│   │   ├── data_client.py         # Data API (public) — trade discovery & wallet history
│   │   ├── gamma_client.py        # Gamma API — market metadata
│   │   ├── clob_client.py         # CLOB API — order books, best ask prices
│   │   └── models.py              # Pydantic v2 data models
│   ├── scanner/
│   │   ├── wallet_discovery.py    # Extract unique wallets from Data API trade stream
│   │   ├── pnl_calculator.py      # FIFO sell-matching to compute realised PnL
│   │   └── performance.py         # Score wallets against all criteria
│   ├── analysis/
│   │   ├── metrics.py             # Profit factor, RR, drawdown, recency, diversity
│   │   └── kelly.py               # Kelly criterion with fractional + price scaling
│   ├── trader/
│   │   ├── realtime_monitor.py    # Conservative monitor: WS detection + G3 guardrail
│   │   ├── fast_copier.py         # Sub-second monitor: no G3, direct paper trade
│   │   ├── paper_trader.py        # Open/close paper positions, snapshots, reports
│   │   ├── live_trader.py         # Real CLOB order execution (FOK via py-clob-client-v2)
│   │   └── signals.py             # Signal engine + unfollow logic
│   ├── storage/
│   │   └── database.py            # SQLite (WAL mode), schema, prune/vacuum
│   └── reporting/
│       ├── dashboard.py           # Rich terminal dashboard
│       └── telegram.py            # Telegram Bot API notifications
├── polymarket_scanner.db          # Main database (wallets, signals, paper_trades)
└── fast_copier.db                 # FastCopier database (fast_trades — separate)
```

### RealtimeMonitor (Conservative Mode)

Seven concurrent async workers. G3 guardrail prevents copying into already-resolved markets.

```
┌──────────────────────────────────────────────────────────────────────┐
│  _ws_worker           ← CLOB WebSocket last_trade_price events       │
│  subscribed to recent   puts (asset_id, price, tx_hash) on queue     │
│  tracked asset IDs      ~50ms event latency                           │
│       │                                                               │
│  _trade_fetcher       ← REST GET /trades?asset_id=X to identify      │
│                         maker wallet address (~200ms REST latency)    │
│       │                                                               │
│  _signal_processor    ← applies guardrails:                          │
│    G0: BUY in 0.01–signal_price_max range                            │
│    G1: dedup — skip if (market, wallet) already open                 │
│    G2: market time remaining ≥ MIN_MARKET_SECONDS_REMAINING          │
│    G3: current ask ≤ signal_price × MAX_COPY_PRICE_MULTIPLIER        │
│    EV: expected value must be positive                               │
│       │                                                               │
│  writes to: signals table → paper_trades table                       │
│                                                                       │
│  _position_closer     ← every 5 min: close at ask ≥ 0.99 or ≤ 0.01  │
│  _wallet_activity_poller ← every 3s per wallet: fallback REST poll   │
│  _unfollow_checker    ← every 30 min: refresh tracked wallets + WS   │
│  _housekeep_worker    ← every 6h: prune old DB rows                  │
└──────────────────────────────────────────────────────────────────────┘

Total latency: ~200–300ms detection + 280ms G3 ask check = ~500ms
G3 blocks 100% of fast-resolving markets (ask already at 0.99 by check time)
```

**When to use**: safe paper trading where you want to avoid entering already-resolved markets.

### Data Flow (Scanner → RealtimeMonitor)

```
Data API (data-api.polymarket.com)
  └─ /activity?user=<address>       ← wallet trade history (max ~3500)
  └─ /trades                        ← global trade stream for discovery

  wallet_trades table (raw cache)
    └─ PnlCalculator.run()          ← FIFO: sets resolved_price on BUY rows
    └─ PerformanceScorer            ← computes WalletScore

  wallets table (scored)
    └─ top-N marked as is_tracked=1

  RealtimeMonitor
    └─ signals table                ← qualifying BUYs with Kelly sizing
    └─ paper_trades table           ← open/closed positions
    └─ portfolio_snapshots          ← periodic equity snapshots
```

---

## FastCopier — Sub-Second Mode

Designed for **low-latency VPS deployments** where round-trip time to Polymarket's API is under 30ms. Removes the G3 price-ceiling check entirely and eliminates the 200ms REST wallet-lookup step.

### Why G3 Blocks Everything on High-Latency VPS

Fast-resolving markets (the most profitable trades from top wallets) behave like this:

```
T=0ms    Wallet buys at 0.03
T=50ms   WebSocket event fires
T=250ms  REST call identifies wallet (current VPS adds 200ms)
T=530ms  G3 check: GET /book → ask is already 0.99 ← BLOCKED
```

On a US-East VPS, the same sequence:

```
T=0ms    Wallet buys at 0.03
T=50ms   WebSocket event fires
T=55ms   Asset map lookup: O(1), no REST call
T=55ms   Paper trade opened at 0.03 ← CAPTURED
T=5000ms Market resolves to 0.99 → auto-closed as WIN
```

### FastCopier Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│  Startup                                                              │
│    Load asset_id → (wallet, market_id) map from scanner DB           │
│    16 tracked wallets → 10,000+ asset IDs subscribed on WS           │
│                                                                       │
│  _ws_worker           ← CLOB WebSocket last_trade_price              │
│  subscribed to ALL      price ≤ max_entry AND asset in map           │
│  tracked asset IDs      → put (asset_id, price, tx_hash) on queue    │
│       │                  NO REST call — wallet from map in O(1)       │
│       │                                                               │
│  _trade_processor     ← dedup → Kelly size → write to fast_trades    │
│                         round trip from WS event: ~1ms               │
│                                                                       │
│  _activity_poller     ← every 5s: poll /activity for each wallet     │
│                         fallback for brand-new markets not yet in map │
│                         adds new asset_ids → triggers WS resubscribe  │
│                                                                       │
│  _position_closer     ← every 5 min: close at ask ≥ 0.99 / ≤ 0.01   │
│  _asset_refresher     ← every 5 min: reload map from scanner DB      │
│  _snapshot_worker     ← every 30 min: log portfolio stats            │
│                                                                       │
│  Writes to: fast_copier.db (separate from main scanner DB)           │
└──────────────────────────────────────────────────────────────────────┘
```

### FastCopier vs RealtimeMonitor Comparison

| Feature | RealtimeMonitor | FastCopier |
|---------|----------------|------------|
| G3 price ceiling check | ✅ Yes (blocks fast markets) | ❌ Removed |
| REST wallet lookup per trade | ✅ ~200ms | ❌ Asset map O(1) |
| Detection latency (US-East VPS) | ~250ms | ~5–20ms |
| Detection latency (Brazil VPS) | ~500ms | ~300ms (still activity-poll limited) |
| Max entry price | 0.30 (configurable) | 0.20 (configurable) |
| Signals table | Uses `signals` table | Bypassed — direct to `fast_trades` |
| Database | `polymarket_scanner.db` | `fast_copier.db` (separate) |
| Paper-trade loop needed | Yes (`python main.py paper-trade`) | No — self-contained |
| Best for | Conservative validation | Sub-second market copying |

### Running FastCopier

```bash
# Basic — uses all defaults (bankroll $2000, max price 0.20)
python fast_copier.py

# Tighter price filter
python fast_copier.py --max-price 0.15

# With custom bankroll matching your actual scanner bankroll
python fast_copier.py --bankroll 3733

# Point at scanner DB if in different directory
python fast_copier.py \
  --scanner-db /path/to/polymarket_scanner.db \
  --fast-db /path/to/fast_copier.db

# Debug logging
python fast_copier.py --log-level DEBUG

# All options
python fast_copier.py --help
```

The FastCopier only **reads** from `polymarket_scanner.db` (for wallet scores and asset IDs) — it never writes to it. All positions are written to `fast_copier.db`. You can copy just the scanner DB to a low-latency VPS and run the FastCopier there independently.

### Checking FastCopier Results

```bash
# Quick stats
python3 -c "
import sqlite3
conn = sqlite3.connect('fast_copier.db')
c = conn.cursor()
c.execute(\"SELECT status, COUNT(*), ROUND(SUM(COALESCE(pnl,0)),2) FROM fast_trades GROUP BY status\")
for r in c.fetchall(): print(r)
c.execute(\"SELECT ROUND(SUM(CASE WHEN status IN ('CLOSED_WIN','CLOSED_LOSS') THEN pnl ELSE 0 END),2) FROM fast_trades\")
print('Realized PnL:', c.fetchone()[0])
conn.close()
"
```

---

## VPS Selection Guide (Vultr)

### Why Location Matters

Polymarket's API (CLOB, WebSocket, Data API) is served through **Cloudflare** with IPs `104.18.34.205` and `172.64.153.51`. Cloudflare routes each request to the nearest PoP, which then forwards to the Polymarket origin.

Current measured RTTs:

| Location | CLOB REST RTT | WS Latency | Cloudflare PoP |
|----------|--------------|------------|----------------|
| Brazil (current VPS) | **274ms avg** | ~260ms | GRU (São Paulo) |
| US East (New Jersey) | ~10–20ms | ~10ms | EWR (Newark) |
| US East (Atlanta) | ~15–25ms | ~15ms | ATL (Atlanta) |
| US Central (Chicago) | ~20–35ms | ~20ms | ORD (Chicago) |
| US West (Los Angeles) | ~60–80ms | ~60ms | LAX |

**New Jersey is the #1 choice** — Cloudflare's EWR PoP is in Newark, NJ, a few milliseconds from any NJ Vultr server, and the Polymarket origin is almost certainly in AWS US-East-1 (N. Virginia / Ashburn), which connects to EWR with <5ms inter-datacenter latency.

### Recommended Vultr Configuration

#### Step 1: Choose Location — New Jersey (Newark)

Go to **vultr.com → Deploy Server → Cloud Compute**:
- **Region**: New Jersey — `ewr` (Newark)
- This gives ~10–20ms RTT vs the current ~274ms — a **13–27× improvement**

#### Step 2: Choose Plan

| Plan | vCPU | RAM | Storage | Cost | Recommendation |
|------|------|-----|---------|------|----------------|
| Cloud Compute (Intel) | 1 | 1GB | 25GB | $6/mo | ⚠️ Minimum (tight on RAM) |
| Cloud Compute (AMD) | 2 | 2GB | 55GB | $12/mo | ✅ **Recommended** |
| Cloud Compute (AMD) | 2 | 4GB | 80GB | $24/mo | ✅ Best value if budget allows |
| Optimized Cloud (AMD) | 2 | 4GB | 100GB NVMe | $28/mo | ✅ Best performance |

**Choose the $12/mo AMD plan as the minimum** (2 vCPU, 2GB RAM). The realtime monitor + fast copier together use ~100–150MB RAM, so 2GB leaves comfortable headroom.

**Avoid:**
- Shared CPU plans labeled "High Frequency" with burst credits — latency spikes during burst recovery
- HDD storage — use NVMe or SSD
- DDoS-protected IP option — adds latency in the protection layer
- Locations outside US-East for this use case

#### Step 3: Choose OS

- **Ubuntu 22.04 LTS** (most tested, long support window)
- or **Ubuntu 24.04 LTS** (newer, also fine)
- Debian 12 also works

#### Step 4: Optional — Enable IPv6

Not required. IPv4 is fine.

#### Step 5: SSH Key

Add your SSH public key during setup for passwordless login.

---

## Full Server Setup (Step-by-Step)

This is the exact sequence used to provision the NJ Vultr VPS. Follow these steps in order on a fresh Ubuntu 22.04 server.

### 1. System Packages

```bash
apt update && apt upgrade -y
apt install -y python3.11 python3.11-venv python3.11-dev \
               git sqlite3 wireguard curl rsync ufw
```

### 2. Clone Repository

```bash
git clone https://github.com/Viky2018Sig/polymarket-wallet-scanner-trader.git
cd /root/polymarket-wallet-scanner-trader
```

### 3. Python Virtual Environment

```bash
python3.11 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

> **Always use `.venv/bin/python3` (not the system `python3`) for all commands on this server.** The venv contains all dependencies and is what the systemd services and cron jobs use.

### 4. Configure `.env`

```bash
cp .env.example .env
nano .env   # or vim .env
```

Minimum required values:

```env
STARTING_BANKROLL=750
KELLY_FRACTION=0.25
MAX_POSITION_PCT=0.005
TELEGRAM_BOT_TOKEN=<your-bot-token>
TELEGRAM_CHAT_ID=<your-chat-id>
```

To create a Telegram bot: message **@BotFather**, send `/newbot`, copy the token. Get your chat ID from `https://api.telegram.org/bot<TOKEN>/getUpdates` after sending the bot a message.

### 5. Sync Scanner Database from Existing VPS

The FastCopier reads wallet scores and trade history from `polymarket_scanner.db`. Copy it from your existing scanner VPS (only needs to be done once — the 4h cron keeps it refreshed):

```bash
# Run this on the NEW VPS (pulls from old VPS)
rsync -avz root@<old-vps-ip>:/root/polymarket-wallet-scanner-trader/polymarket_scanner.db \
  /root/polymarket-wallet-scanner-trader/polymarket_scanner.db
```

### 6. Initialise Databases

```bash
.venv/bin/python3 main.py init-db
```

This creates `polymarket_scanner.db` schema (if not already present from the rsync) and `fast_copier.db`.

### 7. Verify Latency

```bash
.venv/bin/python3 -c "
import httpx, time
times = []
for _ in range(5):
    t = time.time()
    httpx.get('https://clob.polymarket.com/health')
    times.append((time.time()-t)*1000)
print(f'CLOB RTT: avg={sum(times)/len(times):.0f}ms  min={min(times):.0f}ms')
"
# Expected on NJ Vultr: avg=10-20ms min=8ms
# If >100ms, check your region — you may be on the wrong datacenter
```

### 8. Create Logs Directory

```bash
mkdir -p /root/polymarket-wallet-scanner-trader/logs
```

### 9. Install Systemd Services

See [Systemd Services](#systemd-services) section below.

### 10. Install WireGuard VPN

Required **only for live trading** (geo-block bypass for `POST /order`). Not needed for paper trading.
See [WireGuard VPN (Live Trading)](#wireguard-vpn-live-trading) section below.

### 11. Install Cron Jobs

See [Cron Jobs](#cron-jobs) section below.

### 12. Test Telegram

```bash
cd /root/polymarket-wallet-scanner-trader
.venv/bin/python3 fast_report.py --bankroll 750
```

---

## Systemd Services

Using systemd ensures both services auto-start on boot and restart automatically if they crash.

### FastCopier Service

Create `/etc/systemd/system/polymarket-fast-copier.service`:

```ini
[Unit]
Description=Polymarket FastCopier (sub-second, NJ)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/polymarket-wallet-scanner-trader
ExecStart=/root/polymarket-wallet-scanner-trader/.venv/bin/python3 fast_copier.py \
    --bankroll 750 \
    --max-price 0.20 \
    --max-pos-pct 0.005 \
    --kelly 0.25 \
    --log-file logs/fast_copier.log
Restart=always
RestartSec=10
StandardOutput=append:/root/polymarket-wallet-scanner-trader/logs/fast_copier.log
StandardError=append:/root/polymarket-wallet-scanner-trader/logs/fast_copier.log
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

> Adjust `--bankroll` to match your actual starting capital and `--max-pos-pct` to the fraction per trade (0.005 = 0.5%, so $3.75 per trade on $750).

### Realtime Monitor Service

Create `/etc/systemd/system/polymarket-realtime.service`:

```ini
[Unit]
Description=Polymarket Realtime Monitor (NJ)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/polymarket-wallet-scanner-trader
ExecStart=/root/polymarket-wallet-scanner-trader/.venv/bin/python3 main.py realtime
Restart=always
RestartSec=10
StandardOutput=append:/root/polymarket-wallet-scanner-trader/logs/realtime.log
StandardError=append:/root/polymarket-wallet-scanner-trader/logs/realtime.log
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

### Enable and Start

```bash
systemctl daemon-reload
systemctl enable polymarket-fast-copier polymarket-realtime
systemctl start  polymarket-fast-copier polymarket-realtime

# Verify both are running
systemctl status polymarket-fast-copier polymarket-realtime

# Tail live logs
journalctl -u polymarket-fast-copier -f
journalctl -u polymarket-realtime -f

# Restart after config changes
systemctl restart polymarket-fast-copier
```

---

## WireGuard VPN (Live Trading)

**Paper trading does not need a VPN.** All read-only API calls (WebSocket, GET /activity, GET /book) are geo-unblocked. The VPN is only required when placing live orders (`POST /order` to the CLOB) from a US IP.

A **split-tunnel** is used so only Polymarket's CLOB API IPs route through the VPN. SSH and all other traffic stays direct — you won't lose your SSH session if the VPN drops.

### 1. Generate WireGuard Keys on the Server

```bash
wg genkey | tee /etc/wireguard/private.key | wg pubkey > /etc/wireguard/public.key
chmod 600 /etc/wireguard/private.key
cat /etc/wireguard/public.key   # copy this — you'll need it for Mullvad
```

### 2. Register Key with Mullvad

1. Go to **mullvad.net/en/account/wireguard-config**
2. Select platform: **Linux**
3. Click **Import key** and paste the public key from step 1
4. Select exit location: **UK → London → any server** (e.g. `gb-lon-wg-001`)
5. Download the generated `.conf` file

### 3. Create Split-Tunnel Config

Create `/etc/wireguard/wg0.conf` using values from the downloaded Mullvad config:

```ini
[Interface]
PrivateKey = <paste PrivateKey from downloaded Mullvad config>
Address = <paste Address from downloaded Mullvad config>
# No DNS line — split-tunnel only, DNS stays local

[Peer]
PublicKey = <paste PublicKey from downloaded Mullvad config>
# Split-tunnel: ONLY Polymarket CLOB Cloudflare IPs go through VPN
# clob.polymarket.com resolves to 104.18.34.205
AllowedIPs = 104.18.34.205/32, 172.64.153.51/32
Endpoint = <paste Endpoint from downloaded Mullvad config>
PersistentKeepalive = 25
```

```bash
chmod 600 /etc/wireguard/wg0.conf
```

### 4. Bring Up the Tunnel

```bash
wg-quick up wg0

# Verify — should show handshake and correct AllowedIPs
wg show wg0

# Verify CLOB routes through VPN
ip route get 104.18.34.205
# Expected: "104.18.34.205 dev wg0 ..."

# Enable on boot
systemctl enable wg-quick@wg0
```

### 5. Verify Geo-Block Status

```bash
# This checks the CLOB API specifically (routes through VPN)
curl -s https://clob.polymarket.com/health
# Should respond with {"status":"ok"} from a UK IP

# The main polymarket.com website is NOT in the tunnel (that's correct)
```

### Mullvad on Your Local Machine

Mullvad allows up to 5 devices on one account. To access Polymarket on your laptop/desktop:

1. Download the **Mullvad app** from mullvad.net/download (Windows/Mac/Linux)
2. Log in with your account number
3. Connect to any **UK or EU** server
4. Polymarket.com will load immediately

The server's WireGuard key and your desktop Mullvad app use separate device slots — they don't interfere.

---

## Cron Jobs

Install with `crontab -e`. Note: always use `.venv/bin/python3`, not `python3`.

```bash
# Hourly FastCopier report to Telegram (fast_copier.db stats)
0 * * * *    cd /root/polymarket-wallet-scanner-trader && .venv/bin/python3 fast_report.py --bankroll 750 >> logs/fast_report.log 2>&1

# Hourly scanner notify to Telegram (main scanner DB stats)
7 * * * *    cd /root/polymarket-wallet-scanner-trader && .venv/bin/python3 main.py notify >> logs/notify.log 2>&1

# Every 4h: refresh FIFO resolved prices (required for paper trade resolution)
23 */4 * * * cd /root/polymarket-wallet-scanner-trader && .venv/bin/python3 main.py scan --skip-discovery >> logs/scan.log 2>&1

# Daily at 4:47 AM: prune old DB rows + VACUUM
47 4 * * *   cd /root/polymarket-wallet-scanner-trader && .venv/bin/python3 main.py prune >> logs/prune.log 2>&1
```

| Job | Schedule | Purpose |
|-----|----------|---------|
| `fast_report.py` | Every hour at :00 | FastCopier P&L summary → Telegram |
| `notify` | Every hour at :07 | Scanner portfolio report → Telegram |
| `scan --skip-discovery` | Every 4h at :23 | FIFO PnL refresh → enables position resolution |
| `prune` | 4:47 AM daily | Delete old rows, VACUUM SQLite → bounds disk |

**Critical:** paper trades are resolved by detecting wallet exits via FIFO-resolved `resolved_price` values, which are only set when `scan --skip-discovery` runs. The 4h cron keeps resolution lag under 4 hours.

---

## Running the System

### Managing Services

```bash
# Status
systemctl status polymarket-fast-copier polymarket-realtime

# Restart
systemctl restart polymarket-fast-copier
systemctl restart polymarket-realtime

# Tail live logs
journalctl -u polymarket-fast-copier -f --no-pager
journalctl -u polymarket-realtime -f --no-pager

# Or tail log files directly
tail -f /root/polymarket-wallet-scanner-trader/logs/fast_copier.log
tail -f /root/polymarket-wallet-scanner-trader/logs/realtime.log
```

### One-Off Commands

```bash
cd /root/polymarket-wallet-scanner-trader

.venv/bin/python3 main.py dashboard --live     # Auto-refreshing terminal dashboard
.venv/bin/python3 main.py report               # P&L report to stdout
.venv/bin/python3 main.py notify               # Send scanner performance to Telegram
.venv/bin/python3 fast_report.py --bankroll 750  # Send FastCopier report now
.venv/bin/python3 main.py scan --skip-discovery  # Refresh FIFO PnL manually
.venv/bin/python3 main.py prune                # Prune old DB rows + VACUUM
```

### Quick FastCopier Stats

```bash
sqlite3 fast_copier.db "
SELECT
  status,
  COUNT(*) as trades,
  ROUND(SUM(dollar_amount),2) as deployed,
  ROUND(SUM(COALESCE(pnl,0)),2) as pnl
FROM fast_trades GROUP BY status;
"
```

---

## Kelly Criterion Sizing

For a binary Polymarket bet at price `p` with historical win rate `w`:

```
Full Kelly = (w × (1 - p) - (1 - w) × p) / (1 - p)
           = w - (1 - w) × p / (1 - p)
```

Three layers of adjustment applied before final bet size:

| Layer | Adjustment | Default |
|-------|-----------|---------|
| Fractional Kelly | Multiply by safety factor | 0.25 (quarter Kelly) |
| Price-scaled multiplier | Lower price → higher R:R → 1.0–1.5× | Linear over 0.01–0.15 |
| Hard cap | Maximum % of bankroll per position | 0.25% (≈ $5 on $2,000) |

**Example: $2,000 bankroll, entry at 0.06, wallet win rate 55%**
```
Full Kelly  = (0.55 × 0.94 - 0.45 × 0.06) / 0.94 = 52.1%
Quarter K   = 52.1% × 0.25 = 13.0%
Price mult  = 13.0% × 1.3  = 16.9%
Hard cap    = min(16.9%, 0.25%) = 0.25%
Bet size    = 0.25% × $2,000 = $5.00
```

---

## Configuration Reference

All values can be set in `.env` or as environment variables.

| Variable | Default | Description |
|----------|---------|-------------|
| `STARTING_BANKROLL` | 2000 | Paper trading starting bankroll (USD) |
| `KELLY_FRACTION` | 0.25 | Quarter-Kelly safety multiplier |
| `MAX_POSITION_PCT` | 0.0025 | Hard cap: max 0.25% of bankroll per position |
| `MIN_PROFIT_FACTOR` | 2.0 | Wallet qualification threshold |
| `MIN_TRADES_REQUIRED` | 20 | Minimum resolved trades for scoring |
| `LOW_PRICE_MIN_PCT` | 0.10 | Min fraction of wallet trades in 0.01–0.15 range |
| `MAX_DRAWDOWN_THRESHOLD` | 0.40 | Reject wallets with drawdown > 40% |
| `LOOKBACK_DAYS` | 90 | Historical window for wallet analysis |
| `SCAN_INTERVAL_MINUTES` | 30 | Minutes between legacy monitor / paper-trade cycles |
| `MAX_TRACKED_WALLETS` | 50 | Max wallets to actively follow |
| `LOW_PRICE_MIN` | 0.01 | Low-price range lower bound |
| `LOW_PRICE_MAX` | 0.15 | Low-price range upper bound (scanner) |
| `SIGNAL_PRICE_MAX` | 0.30 | Upper limit for signal generation |
| `REALTIME_POLL_SECONDS` | 10 | Poll interval for realtime monitor (seconds) |
| `MAX_COPY_PRICE_MULTIPLIER` | 4.0 | G3 guard: skip if ask > signal_price × this |
| `MIN_MARKET_SECONDS_REMAINING` | 60 | G2 guard: skip if fewer than this many seconds to close |
| `WALLET_TRADES_RETENTION_DAYS` | 90 | Prune resolved wallet_trades older than this |
| `SIGNALS_RETENTION_DAYS` | 7 | Prune acted-on signals older than this |
| `TELEGRAM_BOT_TOKEN` | — | Telegram Bot API token |
| `TELEGRAM_CHAT_ID` | — | Target chat/channel ID |
| `DATABASE_PATH` | ./polymarket_scanner.db | SQLite file path |
| `LOG_LEVEL` | INFO | DEBUG / INFO / WARNING / ERROR |
| `LOG_FILE` | ./logs/scanner.log | Log file path |
| `MAX_REQUESTS_PER_SECOND` | 5.0 | API rate limit |
| `UNFOLLOW_PROFIT_FACTOR_THRESHOLD` | 1.5 | Unfollow if last-10 PF drops below this |

FastCopier has its own CLI flags (`--max-price`, `--bankroll`, `--kelly`, `--max-pos-pct`, `--lookback-days`) and does not use the `.env` file — pass settings directly as arguments.

---

## Database Schema

### Main Database (`polymarket_scanner.db`)

| Table | Description |
|-------|-------------|
| `wallets` | Scored wallets with all metrics and `is_tracked` flag |
| `wallet_trades` | Raw trade data cache from Data API |
| `signals` | Generated BUY signals with Kelly sizing |
| `paper_trades` | Open and closed paper positions |
| `portfolio_snapshots` | Periodic bankroll snapshots for equity curve |

### FastCopier Database (`fast_copier.db`)

| Table | Description |
|-------|-------------|
| `fast_trades` | Open/closed positions from FastCopier (UNIQUE on market+wallet) |
| `fast_snapshots` | 30-min portfolio snapshots |

All databases run in **WAL mode** (write-ahead logging) with `busy_timeout=30000` for concurrent read/write access.

---

## Telegram Notifications

Two separate hourly reports fire from cron:

### FastCopier Report (`fast_report.py` — sent at :00)

Tracks positions in `fast_copier.db`:

```
⚡ FastCopier Report — 2026-05-15 06:00 UTC
━━━━━━━━━━━━━━━━━━━━
📂 Open positions:    89  ($333.75 deployed)
✅ Closed trades:     52  (31✓ / 21✗,  60% hit rate)

💰 Realised P&L:     +$221.44  (+29.53%)
🏦 Portfolio value:   $971.44  (started $750)

🕐 Running since 2026-05-15 05:35 UTC
```

Send manually at any time:
```bash
.venv/bin/python3 fast_report.py --bankroll 750
```

### Scanner Report (`main.py notify` — sent at :07)

Tracks the full scanner pipeline (paper_trades table in `polymarket_scanner.db`):

```
🤖 Polymarket Scanner — Hourly Update

💼 Portfolio
  Total Value:         $3,915.20
  Starting Capital:    $2,000.00
  Realised P&L:   📈 +$1,733.90 (+86.7%)
  Bankroll (realised): $3,733.90

📊 Performance
  Closed Trades:  234  (28W / 206L)
  Win Rate:       11.9%
  Profit Factor:  ✅ 3.85
  Open Positions: 25
  Tracked Wallets: 16

🕐 2026-05-15 02:00 UTC
```

Real-time signal alerts are also sent by the RealtimeMonitor when a tracked wallet opens a new low-price position.

---

## PnL Calculation

The system does **not** use the Gamma API for trade resolution. Instead it uses **FIFO sell-matching**:

- For every `(wallet, market_id, outcome)` group, chronologically match each SELL against the earliest open BUYs.
- The weighted-average sell price becomes the `resolved_price` for matched BUY rows.
- Unmatched BUYs remain open (unresolved).

**Auto-close worker (both monitors):** every 5 minutes, fetches best ask from CLOB for each open position:
- ask ≥ 0.99 → `CLOSED_WIN` at 0.99
- ask ≤ 0.01 → `CLOSED_LOSS` at 0.01

This closes resolved positions the same day they resolve, without waiting for the wallet to explicitly SELL on-chain.

---

## Troubleshooting

**FastCopier shows 0 asset IDs**
- The scanner DB must have `wallet_trades` rows from tracked wallets. Run `python3 main.py scan` first (takes ~1–2 hours), or rsync an existing `polymarket_scanner.db` to the new server.

**FastCopier not catching any trades**
- Check WS connection: `tail -f logs/fast_copier.log | grep "WS"`
- If WS is connected but 0 matches, the tracked wallets may not be actively trading markets in the subscription list. The activity poller (5s interval) will catch any new markets.
- Reduce `--max-price` to 0.15 if you only want ultra-low entries.

**RealtimeMonitor: all trades blocked by G3**
- This is expected when tracking fast-resolving wallets (e.g. wallets with win rate near 100% on markets that resolve in seconds). G3 catches the already-resolved ask price of 0.99. Use FastCopier on a low-latency VPS to bypass G3.

**0 wallets qualify after scan**
- Check `LOW_PRICE_MIN_PCT` — default is 0.10 (10%). Empirically, top wallets by profit factor trade 10–30% of positions in the low-price range. Increasing threshold above 0.20 will exclude most profitable wallets.
- Run `python3 main.py scan --skip-discovery` to recalculate PnL without re-fetching all trades.

**Paper trades not resolving**
- Resolution requires `scan --skip-discovery` to have run (sets `resolved_price` via FIFO).
- Run manually: `python3 main.py scan --skip-discovery && python3 main.py paper-trade --once`

**400 errors on Data API at offset 3500**
- Expected — the Data API has a hard limit of 3,500 records per wallet. The paginator stops gracefully. Suppress with `--log-level WARNING`.

**"database table is locked" errors**
- Only one process should write to each DB at a time. The `busy_timeout=30000` setting (30s) should handle brief contention. If persistent, check you don't have two RealtimeMonitor processes running: `ps aux | grep "python3 main"`.

---

## Disclaimer

This is a paper trading system for research and educational purposes. It does not execute real trades. Past performance of wallet addresses does not guarantee future results. Polymarket is a prediction market — all positions can expire worthless.
