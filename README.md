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
| Hard cap | Maximum % of bankroll per position | 3% |

**Example: $1,000 bankroll, entry at 0.12, wallet win rate 65%**
```
Full Kelly  = (0.65 × 0.88 - 0.35 × 0.12) / 0.88 = 60.2%
Quarter K   = 60.2% × 0.25 = 15.1%
Price mult  = 15.1% × 1.1  = 16.6%  (price=0.12 scales to 1.1×)
Hard cap    = min(16.6%, 3%) = 3.0%
Bet size    = 3% × $1,000 = $30
```

The price-scaled multiplier rewards entries closer to 0.01 with a slightly larger fraction (up to 1.5×), acknowledging the higher R:R at ultra-low prices. The hard 3% cap prevents over-concentration regardless of Kelly output.

---

## Architecture

```
polymarket-wallet-scanner-trader/
├── main.py                        # CLI entry point (8 commands)
├── config.py                      # All settings (env-overridable via .env)
├── src/
│   ├── api/
│   │   ├── data_client.py         # Data API (public) — trade discovery & wallet history
│   │   ├── gamma_client.py        # Gamma API — market info, resolution prices
│   │   ├── clob_client.py         # CLOB API — order books (optional auth)
│   │   └── models.py              # Pydantic v2 data models
│   ├── scanner/
│   │   ├── wallet_discovery.py    # Extract unique wallets from Data API trade stream
│   │   ├── pnl_calculator.py      # FIFO sell-matching to compute realised PnL
│   │   └── performance.py         # Score wallets against all criteria
│   ├── analysis/
│   │   ├── metrics.py             # Profit factor, RR, drawdown, recency, diversity
│   │   └── kelly.py               # Kelly criterion with fractional + price scaling
│   ├── trader/
│   │   ├── paper_trader.py        # Open/close paper positions, snapshots, reports
│   │   └── signals.py             # Monitor tracked wallets, generate buy signals
│   ├── storage/
│   │   └── database.py            # SQLite (WAL mode), 5-table schema
│   └── reporting/
│       ├── dashboard.py           # Rich terminal dashboard
│       └── telegram.py            # Telegram Bot API notifications
├── requirements.txt
└── .env.example
```

**Data flow:**
```
Data API (public) → wallet_discovery → wallet_trades (DB)
  → pnl_calculator (FIFO) → performance scorer
  → wallets (DB) → signal engine → signals (DB)
  → paper_trader (freshness check) → paper_trades (DB)
  → portfolio_snapshots (DB) → dashboard / Telegram
```

**APIs used:**

| API | Base URL | Auth | Purpose |
|-----|----------|------|---------|
| Data API | `https://data-api.polymarket.com` | None (public) | Wallet trade discovery & history |
| Gamma API | `https://gamma-api.polymarket.com` | None | Market info, resolution prices |
| CLOB API | `https://clob.polymarket.com` | Optional Bearer | Order books |

---

## PnL Calculation

The system does **not** rely on the Gamma API for PnL resolution. Instead it uses **FIFO sell-matching**:

- For every `(wallet, market_id, outcome)` group, chronologically match each SELL against the earliest open BUYs.
- The weighted-average sell price becomes the `resolved_price` for matched BUY trades.
- Unmatched BUYs remain open (unresolved).

This approach is:
- **Offline** — no API calls needed, runs on cached trade data.
- **Accurate** — handles partial fills and multiple SELLs against one BUY.
- **Fast** — resolves ~52,000 trades in under 3 minutes.

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
STARTING_BANKROLL=1000
KELLY_FRACTION=0.25
MAX_POSITION_PCT=0.03
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

# Re-score wallets already in DB without re-fetching (fast):
python3 main.py scan --skip-discovery

# Step 2 — Start signal monitor in background
nohup python3 main.py monitor >> logs/monitor.log 2>&1 &

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
```

### Monitor & paper trader flags

```bash
# Single cycle (useful for testing)
python3 main.py monitor --once
python3 main.py paper-trade --once

# Custom cycle interval (default 30 min)
python3 main.py paper-trade --interval 15
```

---

## Automated 2-Hour Telegram Reports (Cron)

A system cron job sends a performance update to Telegram every 2 hours:

```
0 */2 * * *  cd /path/to/polymarket-wallet-scanner-trader && python3 main.py notify >> logs/notify.log 2>&1
```

**Install:**
```bash
(crontab -l 2>/dev/null; echo "0 */2 * * *  cd $(pwd) && python3 main.py notify >> logs/notify.log 2>&1") | crontab -
```

**View cron:**
```bash
crontab -l
```

**Logs:** `logs/notify.log`

---

## Configuration Reference

All values can be set in `.env` or exported as environment variables.

| Variable | Default | Description |
|----------|---------|-------------|
| `STARTING_BANKROLL` | 10000 | Paper trading starting bankroll (USD) |
| `KELLY_FRACTION` | 0.25 | Quarter-Kelly safety multiplier |
| `MAX_POSITION_PCT` | 0.03 | Hard cap: max 3% of bankroll per position |
| `MIN_PROFIT_FACTOR` | 2.0 | Wallet qualification threshold |
| `MIN_TRADES_REQUIRED` | 20 | Minimum resolved trades for scoring |
| `LOW_PRICE_MIN_PCT` | 0.10 | Min fraction of wallet trades in 0.01–0.15 range |
| `MAX_DRAWDOWN_THRESHOLD` | 0.40 | Reject wallets with drawdown > 40% |
| `LOOKBACK_DAYS` | 90 | Historical window for wallet analysis |
| `SCAN_INTERVAL_MINUTES` | 30 | Minutes between monitor cycles |
| `MAX_TRACKED_WALLETS` | 50 | Max wallets to actively follow |
| `LOW_PRICE_MIN` | 0.01 | Low-price range lower bound |
| `LOW_PRICE_MAX` | 0.15 | Low-price range upper bound |
| `TELEGRAM_BOT_TOKEN` | — | Telegram Bot API token |
| `TELEGRAM_CHAT_ID` | — | Target chat/channel ID |
| `DATABASE_PATH` | ./polymarket_scanner.db | SQLite file path |
| `LOG_LEVEL` | INFO | DEBUG / INFO / WARNING / ERROR |
| `LOG_FILE` | — | Optional log file path |
| `MAX_REQUESTS_PER_SECOND` | 5.0 | API rate limit |
| `POLYMARKET_API_KEY` | — | Optional CLOB Bearer token |
| `UNFOLLOW_PROFIT_FACTOR_THRESHOLD` | 1.5 | Unfollow if last-10 PF drops below this |
| `UNFOLLOW_LOOKBACK_TRADES` | 10 | Trade window for unfollow check |
| `WEIGHT_PROFIT_FACTOR` | 0.30 | Composite score weight |
| `WEIGHT_WIN_RATE` | 0.20 | Composite score weight |
| `WEIGHT_LOW_PRICE_PCT` | 0.20 | Composite score weight |
| `WEIGHT_RECENCY` | 0.15 | Composite score weight |
| `WEIGHT_DIVERSITY` | 0.15 | Composite score weight |

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

Example 2-hour update:

```
🤖 Polymarket Scanner — 2h Update

💼 Portfolio
  Bankroll:     $  11,340.00
  Starting:     $  10,000.00
  Total P&L: 📈 +$1,340.00 (+13.4%)

📊 Performance
  Closed Trades:  47  (18W / 29L)
  Win Rate:       38.3%
  Profit Factor:  ✅ 2.31
  Gross Profit:   +$2,180.00
  Gross Loss:     -$840.00
  Max Drawdown:   ✅ 8.2%
  Open Positions: 6

🎯 Tracked Wallets: 8

🪣 Price Buckets
  0.01–0.05: 21 trades
  0.05–0.10: 14 trades
  0.10–0.15: 12 trades

🏆 Top Wallets (paper)
  0xf753…ccd4  12T 99%wr +$420.00
  0xbaa8…1644   8T 87%wr +$310.00

🕐 2026-05-12 18:00 UTC
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
- Ensure the monitor is running and generating fresh signals.

**400 errors on Data API at offset 3500**
- Expected — the Data API has a hard limit of 3,500 records per wallet. The paginator stops gracefully at this limit. These errors can be suppressed with `--log-level WARNING`.

---

## Disclaimer

This is a paper trading system for educational and research purposes. It does not execute real trades. Past performance of wallet addresses does not guarantee future results. Polymarket is a prediction market — all positions can expire worthless.
