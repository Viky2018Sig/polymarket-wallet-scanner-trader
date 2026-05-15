# Polymarket Wallet Scanner & Paper Trader

A strategy engine that scans Polymarket wallets for proven high-RR traders, copies their low-price entries (0.01–0.15), and paper-trades them with Kelly criterion position sizing. Focus is on **high Risk:Reward**, not high win rate.

---

## Strategy Overview

### Core Idea
Most retail traders focus on win rate. This system focuses on **Risk:Reward ratio**. Buying YES/NO tokens at 0.01–0.15 means:

| Entry | Payout on WIN | R:R Ratio | Win rate needed for +EV |
|-------|--------------|-----------|--------------------------|
| 0.01  | 99x          | 99:1      | > 1%                     |
| 0.05  | 19x          | 19:1      | > 5%                     |
| 0.10  | 9x           | 9:1       | > 10%                    |
| 0.15  | 5.7x         | 5.7:1     | > 15%                    |

A wallet with a 20% win rate buying at 0.08 average still has a **profit factor of 2.25+** because each winner returns ~11x.

### Wallet Selection Criteria

**Hard gates (all must pass):**
| Criterion | Threshold | Why |
|-----------|-----------|-----|
| Profit Factor | > 2.0 | Total gross profit / gross loss must 2× |
| Low-price trade exposure | ≥ 10% of positions at 0.01–0.15 | Confirms the wallet operates in high-RR space |
| Minimum resolved trades | ≥ 20 | Prevents noise / small sample luck |
| Max drawdown | ≤ 40% | Filters wallets with extreme volatility |

> **Why 10% and not 50%?** Real-world analysis of 159 Polymarket wallets showed that the best performers (PF > 10) trade 10–30% of positions in the 0.01–0.15 range — not 50%+. The top wallets by profit factor actively mix low-price "lottery" entries with mid-range positions. A 50% threshold would disqualify every profitable wallet found in the dataset.

**Composite scoring (determines ranking among qualifiers):**
| Factor | Weight | Notes |
|--------|--------|-------|
| Profit Factor (norm to 5× = 1.0) | 30% | Primary edge signal |
| Win Rate | 20% | Higher still preferred |
| Low-Price % | 20% | More focus on target range = better |
| Recency (last 30d weighted 2×) | 15% | Recent edge matters more |
| Market Diversity (HHI) | 15% | Avoids single-market flukes |

**Additional strategy guardrails:**
1. **Negative EV guard** — signal generated only if the wallet's historical win rate for that price bucket produces positive expected value at the current entry price.
2. **Signal freshness** — signals older than 60 minutes are discarded; price must not have drifted > 1% from the signal's entry price before execution.
3. **Auto-unfollow** — if a tracked wallet's last 10 resolved trades drop below profit factor 1.5, it is automatically unfollowed.
4. **Recency decay** — scoring weights trades in the last 30 days 2× more than trades from 31–90 days ago. Edge on Polymarket is event-driven and decays quickly.
5. **Bucket-specific win rates** — Kelly sizing uses win rate specific to the price bucket (e.g., 0.01–0.05 vs 0.10–0.15), not the wallet's overall average.
6. **First-cycle seeding** — on restart, the monitor seeds its "last seen" timestamps from the database so it never re-emits old signals as fresh.

---

## Kelly Criterion Sizing

For a binary Polymarket bet at price `p` with historical win rate `w`:

```
Full Kelly = (w × (1 - p) - (1 - w) × p) / (1 - p)
           = w - (1 - w) × p / (1 - p)
```

Three layers of adjustment are applied before the final bet size:

| Layer | Adjustment | Default |
|-------|-----------|---------|
| Fractional Kelly | Multiply by safety factor | 0.25 (quarter Kelly) |
| Price-scaled multiplier | Lower price → higher R:R → 1.0–1.5× | Linear over 0.01–0.15 |
| Hard cap | Maximum % of bankroll per position | 0.25% (≈ $5 on $2,000) |

**Example: $2,000 bankroll, entry at 0.06, wallet win rate 55%**
```
Full Kelly  = (0.55 × 0.94 - 0.45 × 0.06) / 0.94 = 52.1%
Quarter K   = 52.1% × 0.25 = 13.0%
Price mult  = 13.0% × 1.3  = 16.9%  (price=0.06 scales to ~1.3×)
Hard cap    = min(16.9%, 0.25%) = 0.25%
Bet size    = 0.25% × $2,000 = $5.00
```

The price-scaled multiplier rewards entries closer to 0.01 with a slightly larger fraction (up to 1.5×), acknowledging the higher R:R at ultra-low prices. The hard 0.25% cap prevents over-concentration regardless of Kelly output.

---

## Architecture

```
polymarket-wallet-scanner-trader/
├── main.py                        # CLI entry point (9 commands)
├── config.py                      # All settings (env-overridable via .env)
├── src/
│   ├── api/
│   │   ├── data_client.py         # Data API (public) — trade discovery & wallet history
│   │   ├── gamma_client.py        # Gamma API — market info for signal alerts
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
│   │   ├── realtime_monitor.py    # Real-time monitor (10s poll, WebSocket-ready)
│   │   ├── paper_trader.py        # Open/close paper positions, snapshots, reports
│   │   └── signals.py             # Legacy 30-min monitor + unfollow logic
│   ├── storage/
│   │   └── database.py            # SQLite (WAL mode), 5-table schema, prune/vacuum
│   └── reporting/
│       ├── dashboard.py           # Rich terminal dashboard
│       └── telegram.py            # Telegram Bot API notifications
├── requirements.txt
└── .env.example
```

### Real-time Monitor Architecture

The preferred monitor (`realtime` command) uses a producer/consumer pattern over an `asyncio.Queue`:

```
┌─────────────────────┐
│  _poll_worker       │  ← polls Data API every 10s (all wallets concurrently)
│  (swap for WS here) │    puts new WalletTrade events onto the queue
└────────┬────────────┘
         │ asyncio.Queue (maxsize=2000)
┌────────▼────────────┐
│  _signal_processor  │  ← applies guardrails, writes signals to DB
│                     │    guardrail 0: BUY in 0.01–0.15 price range
│                     │    guardrail 1: dedup (market already open → skip)
│                     │    guardrail 2: market has ≥ MIN_SECONDS_REMAINING
│                     │    guardrail 3: current ask ≤ signal_price × MAX_MULTIPLIER
└────────┬────────────┘
         │
┌────────▼────────────┐    ┌──────────────────────┐
│  _housekeep_worker  │    │  _unfollow_checker   │
│  (prune DB, 6h)     │    │  (check exits, 30min)│
└─────────────────────┘    └──────────────────────┘
```

**Price ceiling guardrail example**: wallet bought at 0.05 → copy price ceiling = 0.05 × 4 = 0.20. If the current best ask exceeds $0.20, the signal is skipped. This prevents chasing price in fast-moving markets.

**WebSocket upgrade path**: replace `_poll_worker` with a WebSocket client that connects to `wss://ws-subscriptions-clob.polymarket.com/ws/` and puts parsed `WalletTrade` objects onto `self._queue`. Everything downstream (signal processor, housekeep, unfollow) is identical.

**Data flow:**
```
Data API (public) → wallet_discovery → wallet_trades (DB)
  → pnl_calculator (FIFO) → performance scorer
  → wallets (DB) → realtime_monitor (10s poll) → signals (DB)
  → paper_trader (freshness check) → paper_trades (DB)
  → portfolio_snapshots (DB) → dashboard / Telegram
```

**APIs used:**

| API | Base URL | Auth | Purpose |
|-----|----------|------|---------|
| Data API | `https://data-api.polymarket.com` | None (public) | Wallet trade discovery & history |
| Gamma API | `https://gamma-api.polymarket.com` | None | Market end times, signal alert text |
| CLOB API | `https://clob.polymarket.com` | Optional Bearer | Best ask prices for price ceiling check |

---

## PnL Calculation

The system does **not** rely on the Gamma API for PnL resolution. Instead it uses **FIFO sell-matching**:

- For every `(wallet, market_id, outcome)` group, chronologically match each SELL against the earliest open BUYs.
- The weighted-average sell price becomes the `resolved_price` for matched BUY trades.
- Unmatched BUYs remain open (unresolved).

**Resolution chain for paper trades:**
1. Tracked wallet places SELL on Polymarket
2. `realtime` monitor detects the SELL and caches it in `wallet_trades`
3. Nightly cron runs `scan --skip-discovery` → FIFO calculator sets `resolved_price` on BUY rows
4. Next `paper-trade` cycle calls `resolve_closed_markets()` → detects all BUYs resolved → closes paper trade at wallet's exit price

This approach is:
- **Offline** — no API calls needed, runs on cached trade data.
- **Accurate** — handles partial fills and multiple SELLs against one BUY.
- **Fast** — resolves ~57,000 trades in under 3 minutes.

---

## Setup

### 1. Clone and install

```bash
git clone https://github.com/aumelvik/polymarket-wallet-scanner-trader.git
cd polymarket-wallet-scanner-trader
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
```

Edit `.env`:

```env
STARTING_BANKROLL=2000
KELLY_FRACTION=0.25
MAX_POSITION_PCT=0.0025
TELEGRAM_BOT_TOKEN=123456789:ABCdefGhIJKlmNoPQRstuVWXyz
TELEGRAM_CHAT_ID=-100123456789
```

### 3. Create the Telegram bot

1. Message **@BotFather** in Telegram
2. Send `/newbot` → copy the **bot token**
3. Add the bot to your channel/group (or start a DM)
4. Visit `https://api.telegram.org/bot<TOKEN>/getUpdates` after sending a message to get your **chat ID**
5. For a private channel, the chat ID starts with `-100`

### 4. Initialise the database

```bash
python3 main.py init-db
```

### 5. Test Telegram

```bash
python3 main.py notify --test
```

---

## Running the System

### Full pipeline (background processes)

```bash
# Step 1 — Discover and score wallets (run once, then weekly)
python3 main.py scan --max-pages 200 --top-n 50

# Re-score wallets already in DB without re-fetching (fast, also resolves FIFO):
python3 main.py scan --skip-discovery

# Step 2 — Start real-time signal monitor in background (preferred)
nohup python3 main.py realtime >> logs/realtime.log 2>&1 &

# Step 3 — Start paper trader in background
nohup python3 main.py paper-trade >> logs/paper-trade.log 2>&1 &
```

### One-off commands

```bash
# Live dashboard (auto-refreshes every 30s)
python3 main.py dashboard --live

# Single dashboard snapshot
python3 main.py dashboard

# Performance report in terminal
python3 main.py report

# Performance report as JSON
python3 main.py report --json

# Replay all historical signals through paper trader logic
python3 main.py backtest

# Send current performance to Telegram now
python3 main.py notify

# Prune old DB rows and VACUUM (reclaim disk space)
python3 main.py prune
```

### Flags

```bash
# Single cycle (useful for testing)
python3 main.py monitor --once       # legacy 30-min monitor
python3 main.py paper-trade --once

# Custom cycle interval (default 30 min)
python3 main.py paper-trade --interval 15

# All commands accept:
python3 main.py <cmd> --log-level DEBUG --log-file logs/debug.log
```

---

## Automated Cron Jobs

```
7 * * * *    cd /root/polymarket-wallet-scanner-trader && python3 main.py notify >> logs/notify.log 2>&1
23 */4 * * * cd /root/polymarket-wallet-scanner-trader && python3 main.py scan --skip-discovery >> logs/scan.log 2>&1
47 4 * * *   cd /root/polymarket-wallet-scanner-trader && python3 main.py prune >> logs/prune.log 2>&1
```

| Job | Schedule | Purpose |
|-----|----------|---------|
| `notify` | Every hour (at :07) | Send Telegram performance report |
| `scan --skip-discovery` | Every 4h (at :23) | Refresh FIFO resolved prices → enables paper trade resolution |
| `prune` | 4:47 AM daily | Delete old rows, VACUUM SQLite → bounds disk usage |

**Critical dependency**: paper trades are resolved by detecting wallet exits via FIFO-resolved `resolved_price` values. These are only set when `scan --skip-discovery` runs. The every-4-hour cron keeps resolution lag under 4 hours.

**Install all three crons:**
```bash
crontab -e
# Add the three lines above
```

---

## Disk Management

The `prune` command (also auto-runs in `realtime` every 6 hours) deletes:
- **Resolved `wallet_trades`** older than `WALLET_TRADES_RETENTION_DAYS` (default 90 days) — unresolved BUY rows are never deleted
- **Acted-on signals** older than `SIGNALS_RETENTION_DAYS` (default 7 days)
- **Duplicate portfolio snapshots** older than 7 days (keeps one per day)

After deletion it runs `PRAGMA wal_checkpoint(TRUNCATE)` then `VACUUM` via a dedicated synchronous SQLite connection to fully reclaim disk space.

---

## Configuration Reference

All values can be set in `.env` or exported as environment variables.

| Variable | Default | Description |
|----------|---------|-------------|
| `STARTING_BANKROLL` | 2000 | Paper trading starting bankroll (USD) |
| `KELLY_FRACTION` | 0.25 | Quarter-Kelly safety multiplier |
| `MAX_POSITION_PCT` | 0.0025 | Hard cap: max 0.25% of bankroll per position (≈ $5 on $2,000) |
| `MIN_PROFIT_FACTOR` | 2.0 | Wallet qualification threshold |
| `MIN_TRADES_REQUIRED` | 20 | Minimum resolved trades for scoring |
| `LOW_PRICE_MIN_PCT` | 0.10 | Min fraction of wallet trades in 0.01–0.15 range |
| `MAX_DRAWDOWN_THRESHOLD` | 0.40 | Reject wallets with drawdown > 40% |
| `LOOKBACK_DAYS` | 90 | Historical window for wallet analysis |
| `SCAN_INTERVAL_MINUTES` | 30 | Minutes between legacy monitor / paper-trade cycles |
| `MAX_TRACKED_WALLETS` | 50 | Max wallets to actively follow |
| `LOW_PRICE_MIN` | 0.01 | Low-price range lower bound |
| `LOW_PRICE_MAX` | 0.15 | Low-price range upper bound |
| `REALTIME_POLL_SECONDS` | 10 | Poll interval for realtime monitor (seconds) |
| `MAX_COPY_PRICE_MULTIPLIER` | 4.0 | Skip signal if ask > signal_price × this (price ceiling) |
| `MIN_MARKET_SECONDS_REMAINING` | 60 | Skip market if fewer than this many seconds until close |
| `WALLET_TRADES_RETENTION_DAYS` | 90 | Prune resolved wallet_trades older than this |
| `SIGNALS_RETENTION_DAYS` | 7 | Prune acted-on signals older than this |
| `TELEGRAM_BOT_TOKEN` | — | Telegram Bot API token |
| `TELEGRAM_CHAT_ID` | — | Target chat/channel ID |
| `DATABASE_PATH` | ./polymarket_scanner.db | SQLite file path |
| `LOG_LEVEL` | INFO | DEBUG / INFO / WARNING / ERROR |
| `LOG_FILE` | ./logs/scanner.log | Log file path |
| `MAX_REQUESTS_PER_SECOND` | 5.0 | API rate limit |
| `POLYMARKET_API_KEY` | — | Optional CLOB Bearer token |
| `UNFOLLOW_PROFIT_FACTOR_THRESHOLD` | 1.5 | Unfollow if last-10 PF drops below this |
| `UNFOLLOW_LOOKBACK_TRADES` | 10 | Trade window for unfollow check |

---

## Database Schema

| Table | Description |
|-------|-------------|
| `wallets` | Scored wallets with all metrics and tracking flag |
| `wallet_trades` | Raw trade data cache from Data API |
| `signals` | Generated BUY signals with Kelly sizing |
| `paper_trades` | Open and closed paper positions |
| `portfolio_snapshots` | Periodic bankroll snapshots for equity curve |

All monetary values stored as `TEXT`/`Decimal` — no floating point drift.  
Database runs in **WAL mode** (write-ahead logging) for concurrent read/write access.

---

## Telegram Message Format

Hourly update (sent at :07 each hour):

```
🤖 Polymarket Scanner — Hourly Update

💼 Portfolio
  Bankroll:          $   2,319.24
  Starting:          $   2,000.00
  Realised P&L:   📈 +$  319.24 (+16.0%)
  Unrealised P&L: 📈 +$  450.00 (open positions marked to last price)

📊 Performance
  Closed Trades:  48  (26W / 22L)
  Win Rate:       54.2%
  Profit Factor:  ✅ 75.72
  Gross Profit:   +$345.00
  Gross Loss:     -$26.00
  Max Drawdown:   ✅ 1.2%
  Open Positions: 237

🎯 Tracked Wallets: 8

🏆 Top Trades by Profit
  btc-5min-up-jun2026 | 0xe9076a87… | +$18.40 | 0.04→0.98
  eth-5min-up-may2026 | 0xe9076a87… | +$14.20 | 0.03→0.97

🕐 2026-05-13 14:07 UTC
```

Real-time signal alerts are also sent when a tracked wallet opens a new low-price position.

---

## Performance Targets

| Metric | Target | Notes |
|--------|--------|-------|
| Tracked wallet profit factor | > 2.0 | Hard gate for inclusion |
| Paper portfolio profit factor | > 1.5 | Lagging the wallets is expected |
| Win rate | 15–40% | Low is fine given high R:R |
| Max drawdown | < 25% | Quarter-Kelly limits this structurally |
| Avg R:R on winners | > 5:1 | The reason low win rate is acceptable |

**On win rate:** At 20% win rate with 9:1 average R:R:
`(0.20 × 9) / (0.80 × 1) = 1.8 / 0.8 = 2.25` — above the 2.0 threshold.

---

## Troubleshooting

**0 wallets qualify after scan**
- Check `LOW_PRICE_MIN_PCT` — default is 0.10 (10%). The 0.01–0.15 exposure threshold is empirically calibrated; increasing it above 0.20 will exclude most profitable wallets.
- Ensure `pnl_calculator` has run — resolved trades are required for profit factor. Run `python3 main.py scan --skip-discovery` to recalculate.

**Monitor says "No tracked wallets"**
- Run `scan` first, or run `scan --skip-discovery` to re-score and re-mark wallets without re-fetching all trades.

**Paper trader not opening trades**
- Signals older than 60 minutes are automatically discarded (price may have moved).
- Check `logs/paper-trade.log` for "Skipped N stale/invalid signals" messages.
- Ensure the realtime monitor is running and generating fresh signals.

**Open paper trades not resolving**
- Resolution requires the nightly `scan --skip-discovery` cron to have run (sets `resolved_price` via FIFO).
- Run manually: `python3 main.py scan --skip-discovery && python3 main.py paper-trade --once`

**400 errors on Data API at offset 3500**
- Expected — the Data API has a hard limit of 3,500 records per wallet. The paginator stops gracefully at this limit. These errors can be suppressed with `--log-level WARNING`.

**Price ceiling triggering too often (missing trades)**
- Lower `MAX_COPY_PRICE_MULTIPLIER` to 3.0 or raise to 5.0 in `.env`. Default 4× means wallet entry of 0.05 allows copy up to 0.20.

---
Claude Code built-ins (/command)

/help — full help
/config — change theme, model, etc.
/clear — clear conversation
/cost — show token usage
/compact — compress context
/memory — view/edit memory files
Available skills (/skill-name)

/update-config — configure hooks, permissions, env vars in settings.json
/keybindings-help — customize keyboard shortcuts
/simplify — review and clean up changed code
/fewer-permission-prompts — auto-allowlist common read-only commands
/loop — run a prompt on a recurring interval
/schedule — schedule a recurring remote agent (cron)
/claude-api — build/debug Anthropic SDK apps
/init — generate a CLAUDE.md for a codebase
/review — review a pull request
/security-review — security review of current branch changes
For this project specifically, the common commands are:


python main.py realtime      # WS monitor (running, PID 253499)
python main.py paper-trade   # paper trader (running, PID 113759)
python main.py report        # P&L report to stdout
python main.py notify        # send Telegram report
python main.py scan --skip-discovery  # FIFO refresh (run every 4h via cron)
---

---
This is very revealing. The VPS is in Brazil (São Paulo), which means:

CLOB API latency: 280ms (very high for an exchange - likely due to geographic distance from servers in US/EU)
Data API: 115ms
WS endpoint: 5ms TCP connect but actual WebSocket event delivery would be similar to CLOB (~200-300ms round trip)
The Polymarket infrastructure is likely in the US (AWS US-East or similar). From Brazil to US, typical latency is 100-200ms.

For sub-second copying to work, you'd ideally want a VPS close to Polymarket's servers.

So the infrastructure is probably in AWS US-East-1 or a similar region. Looking at the actual timing breakdown: WebSocket detection takes 200-300ms, the CLOB order book check adds another 280ms, and order placement another 280ms—that's roughly 760ms total from when a wallet trade happens to when our order fills. For markets resolving in 5-15 seconds, that should theoretically be fast enough. The real bottleneck isn't the hardware itself but the G3 check eating up 280ms per call, plus the activity poller's 3-second delay. The WebSocket path should actually be quick enough if I bypass the poller.

The catch is that we need to be subscribed to the exact asset a tracked wallet will buy before they buy it. The WebSocket worker subscribes to recent asset IDs from the past 7 days, which is broad coverage, but if a tracked wallet buys a completely new asset we haven't seen yet, we might not be listening for it. Actually, looking closer at the worker, it does pull all asset IDs from tracked wallets' recent activity, so we should be covered there—but I need to verify the subscription is actually capturing brand new assets in real time.

The real constraint is that these prolific wallets like 0xe9076a87 trade across thousands of different markets, and we can't subscribe to all of them due to WebSocket limits. We're only subscribed to the most recent 500 or so assets. So the actual bottleneck is: for assets already in the database, the WebSocket fires and we detect within 200ms, but for completely new assets we're not subscribed to, we miss the signal entirely.

The solution involves three key changes: relocating the VPS from Brazil to US-East to cut latency from 280ms down to ~20ms, skipping the G3 check that's already stale by the time we look at it and instead using the trade price as our entry point, and placing FOK orders immediately at slightly above the signal price when the WebSocket fires. But I'm realizing there's something odd here—if 0xe9076a87 buys at 0.05 and the market resolves to YES at 0.99 within seconds, there must be significant counter-liquidity available at those prices.

The practical approach is to aggressively subscribe to all assets from the wallet's history, then fire off a FOK order at the exact price the moment the WebSocket signal hits—no G3 validation, just immediate execution. If it fills, we're in; if the market moved and it gets rejected, we lose nothing since it's fill-or-kill. This needs a VPS closer to Polymarket's infrastructure, modifications to the live_trader to bypass the G3 check on WebSocket signals, and USDC sitting in the proxy wallet ready to go.

Looking at the latency breakdown: from Brazil to US-East is roughly 130ms one-way, so by the time our WebSocket client receives a trade event and we send a FOK order back, we're looking at around 263ms total—well within the 5-15 second resolution window for these markets. The real constraint is whether there's actually ask-side liquidity available at the price point we're targeting when our order arrives. moved to higher prices. If other bots are also frontrunning these trades, the liquidity at 0.05 gets eaten up fast. So the real constraint is whether we can get our order to the exchange before the book shifts—our current setup with ~280ms latency is too slow, but moving to a better location like AWS US-East-1 could cut that to ~20ms. The wallet's empty balance is also blocking us from testing this right now, and there's a chance these wallets have some information edge that lets them move ahead of us anyway.

Here's the full technical picture:

Current VPS latency to Polymarket:

Location: Brazil (São Paulo) — this is the main problem
CLOB REST: 280ms round trip
WebSocket: ~260ms for events to arrive
What sub-second copying actually requires:

The WebSocket path already gives ~200ms detection. The chain for a live copy would be:


Wallet buys at 0.05 → WS event fires → we receive it (+260ms) 
→ place FOK order (+260ms) = ~520ms total
For markets resolving in 5-15 seconds, 520ms is theoretically fast enough. The architecture is mostly already built — the WS worker, live_trader.py with FOK orders. The two blockers are:

1. G3 kills us before placing the order
Right now we check current ask (another 280ms CLOB call) before placing. By then ask = 0.99. The fix: skip G3 for the live path — place the FOK order optimistically at the signal price. If the order book has already moved, FOK gets rejected harmlessly (no fill = no loss).

2. The proxy wallet has $0.37 USDC
Zero capital deployed = zero trades possible even if the code is perfect.

What a better VPS would give:

Location	RTT to CLOB	Total latency
Brazil (current)	280ms	~520ms
AWS US-East-1 (Virginia)	~20ms	~50ms
Moving to US-East would be a 10× improvement and would let you get in while there's still order book depth at the entry price.

Bottom line — can the current VPS do it?

Technically yes for markets resolving in >1 second — 520ms is inside the window
Practically limited by Brazil latency — you'd compete poorly against bots on US servers
Real blocker right now: empty proxy wallet
Recommended path:

Deposit USDC to proxy wallet 0x8905a77bB590138bA5d7d29f218Bd6De609Bb1cB
Modify live trader to skip G3 and place FOK immediately on WS signal
If results are good, migrate VPS to AWS US-East for 10× speed improvement
Want me to make the code change for the live trader (skip G3, place FOK on WS signal directly)?

no lets keep it like what it is now and monitor for 2 weeks
i want to know if the other markets gives great results from other wallets.
---
## Disclaimer

This is a paper trading system for educational and research purposes. It does not execute real trades. Past performance of wallet addresses does not guarantee future results. Polymarket is a prediction market — all positions can expire worthless.
