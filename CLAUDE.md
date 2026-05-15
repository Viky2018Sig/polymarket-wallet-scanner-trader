# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Real-time monitor — 10s polling, three concurrent workers, guardrails applied
python main.py realtime

# Prune old DB rows and VACUUM (also runs automatically via cron)
python main.py prune

# Run the full discovery + scoring pipeline
python main.py scan

# Re-score already-discovered wallets (no API discovery), runs FIFO PnL calc
python main.py scan --skip-discovery

# Start continuous signal monitoring (every 30 min, runs until killed)
python main.py monitor

# Single monitor cycle (for debugging)
python main.py monitor --once

# Start continuous paper trading loop
python main.py paper-trade

# Single paper-trade cycle (process signals + resolve markets + snapshot)
python main.py paper-trade --once

# Terminal dashboard
python main.py dashboard
python main.py dashboard --live

# Send Telegram performance report
python main.py notify

# Performance report to stdout
python main.py report

# Live trading (real CLOB orders via py-clob-client-v2)
python main.py live-trade           # continuous loop (30-min cycles)
python main.py live-trade --once    # single cycle
python main.py live-trade --status  # print portfolio summary, no trading

# Database must be initialised before first use
python main.py init-db
```

All commands accept `--log-level DEBUG` and `--log-file <path>` options. Logs also go to `./logs/scanner.log` via `.env`.

## Cron schedule

```
7 * * * *    notify (hourly Telegram report)
23 */4 * * * scan --skip-discovery (every 4h FIFO refresh, required for auto-resolution)
47 4 * * *   prune (daily DB vacuum — keeps disk bounded)
```

## Architecture

The system has two runtime loops that run as separate processes:

**`realtime`** — Preferred monitor. Five concurrent async workers: `_ws_worker` (CLOB WebSocket at `wss://ws-subscriptions-clob.polymarket.com/ws/market`, subscribes to 500 most-recent asset IDs, ~50ms event latency), `_trade_fetcher` (REST GET `/trades?asset=X` on Data API to identify maker wallet — ~200ms total detection latency), `_signal_processor` (guardrails: price range, market time remaining, price ceiling), `_housekeep_worker` (prunes DB every 6h), `_unfollow_checker` (refreshes WS subscriptions every 30min). Detection latency: ~200ms vs 0–10s with old REST poll.

**`monitor`** — Legacy 30-min polling monitor. Still usable; `realtime` supersedes it for signal quality.

**`paper-trade`** — Polls every 30 min. Each cycle:
1. Reads unacted signals → opens `PaperTrade` rows (Kelly-sized, deduped by market+wallet)
2. Calls `resolve_closed_markets()` → checks `get_wallet_exit_price()` for each open trade
3. Takes a portfolio snapshot

**Critical dependency**: `resolve_closed_markets()` detects exits via `get_wallet_exit_price()`, which requires `resolved_price` to be populated on `wallet_trades` rows. This only happens when `pnl_calculator.run()` executes — inside `scan --skip-discovery`. The nightly cron closes this gap.

## Data flow

```
Data API (data-api.polymarket.com)
  └─ /activity?user=<address>       ← wallet trade history (paginated, max ~3500)
  └─ /trades                        ← global trade stream for discovery
  └─ /trades?asset=<token_id>       ← per-asset recent trades (WS trade fetcher)

CLOB WebSocket (ws-subscriptions-clob.polymarket.com/ws/market)
  └─ last_trade_price events        ← ~50ms trade detection, asset_id+price+side+tx_hash

wallet_trades table (raw cache)
  └─ PnlCalculator.run()            ← FIFO: sets resolved_price on BUY rows
  └─ PerformanceScorer              ← reads resolved trades to compute WalletScore

wallets table (scored)
  └─ top-N marked as tracked=1

signals table
  └─ SignalEngine._check_wallet()   ← BUY in low-price range → Signal row

paper_trades table
  └─ PaperTrader.process_signals()  ← Signal → PaperTrade (OPEN)
  └─ PaperTrader.resolve_closed_markets() ← wallet exit detected → PaperTrade (CLOSED_*)
```

## Live trading

`src/trader/live_trader.py` — places real FOK market orders on the Polymarket CLOB via `py-clob-client-v2`. Reads unacted signals from the same `signals` DB table as `paper-trade`, then executes CLOB orders instead of recording paper positions.

**Credentials**: loaded from `settings.live_env_file` (default: `/root/copybot-live/polymarket-copybot/.env`). Requires `PK`, `PROXY_WALLET`, `CLOB_API_KEY`, `CLOB_SECRET`, `CLOB_PASSPHRASE`, `SIG_TYPE=3`.

**POLY_1271 patches**: required for SIG_TYPE=3 (ERC-7739 deposit wallet signing). Applied at install time:
```bash
SITE_PKGS=$(python3 -c "import py_clob_client_v2,os; print(os.path.dirname(py_clob_client_v2.__file__))")
cp /root/copybot-live/polymarket-copybot/patches/exchange_order_builder_v2.py $SITE_PKGS/order_utils/
cp /root/copybot-live/polymarket-copybot/patches/order_builder_builder.py $SITE_PKGS/order_builder/builder.py
```

**Position management**: stop-loss (only if entry ≥ 0.30, triggers at 20% loss), take-profit (0.80 for entries < 0.30; +50% for entries ≥ 0.30). Positions held to resolution at price 0.99/0.01 (auto-settle on-chain, no sell order needed).

**State**: persisted to `settings.live_state_file` (default: `data/live_state.json`). Config overrides via `.env` or pydantic settings: `LIVE_ENV_FILE`, `MAX_LIVE_BET_USD`, `MAX_LIVE_POSITIONS`, etc.

**Wallet balance**: $0.37 USDC (essentially empty — deposit USDC to proxy wallet `0x8905a77bB590138bA5d7d29f218Bd6De609Bb1cB` before live trading).

## Key design decisions

**Gamma API avoidance**: `gamma-api.polymarket.com/markets/{hex_id}` returns 422 for hex condition IDs. Market resolution does NOT use Gamma API. Instead, it checks whether the tracked wallet has fully exited the position in `wallet_trades` (all BUY rows have `resolved_price` set from FIFO matching). The exit price becomes the paper trade's exit price.

**FIFO PnL calculation**: `PnlCalculator` groups trades by `(wallet_address, market_id, outcome)` and matches SELLs against chronologically-earliest BUYs. The SELL price is written to `resolved_price` on the matched BUY row. Partial fills are handled correctly.

**Signal freshness**: Signals older than 60 min are discarded. Price drift > 1% from a signal's entry price (checked against last DB trade for the asset) also discards the signal.

**`_last_seen` seeding**: On `monitor` startup, `SignalEngine` seeds its `_last_seen` dict from the DB's most recent signal time per wallet. Without this, every restart would replay ~24h of historical trades as new signals.

**Deduplication**: `PaperTrader.process_signals()` skips signals where `(market_id, wallet_followed)` already has an open trade.

**Bankroll reconstruction**: `take_daily_snapshot()` always recomputes bankroll as `starting_bankroll + sum(realised_pnl)`. It does not accumulate from previous snapshots, preventing drift.

## Configuration

All settings are in `config.py` (`Settings` class, pydantic-settings) and overridable via `.env`. Key values:

| Setting | Default | Notes |
|---|---|---|
| `STARTING_BANKROLL` | 2000 | Current bankroll in USD |
| `MAX_POSITION_PCT` | 0.0025 | 0.25% of bankroll per trade ≈ $5 |
| `KELLY_FRACTION` | 0.25 | Quarter Kelly safety multiplier |
| `LOW_PRICE_MIN_PCT` | 0.10 | Min fraction of wallet's trades in 0.01–0.15 range |
| `MIN_PROFIT_FACTOR` | 2.0 | Qualification threshold |
| `MIN_TRADES_REQUIRED` | 20 | Min resolved trades to qualify |
| `LOOKBACK_DAYS` | 90 | Trade history window |
| `SCAN_INTERVAL_MINUTES` | 30 | Monitor/paper-trade cycle interval |
| `REALTIME_POLL_SECONDS` | 10 | Realtime monitor poll interval |
| `MAX_COPY_PRICE_MULTIPLIER` | 4.0 | Price ceiling: skip if ask > signal_price × this |
| `MIN_MARKET_SECONDS_REMAINING` | 60 | Skip market if fewer than this many seconds remain |
| `WALLET_TRADES_RETENTION_DAYS` | 90 | Prune resolved wallet_trades older than this |
| `SIGNALS_RETENTION_DAYS` | 7 | Prune acted-on signals older than this |

## Database

SQLite WAL mode at `./polymarket_scanner.db`. Five tables: `wallets`, `wallet_trades`, `signals`, `paper_trades`, `portfolio_snapshots`. Schema is auto-migrated in `Database._migrate()` on every connect — it's additive (CREATE IF NOT EXISTS), safe to re-run.

All DB access is async via `aiosqlite`. Use `async with Database() as db:` or call `connect()`/`close()` manually.

## API clients

- **`DataApiClient`** — primary, public, no auth. Used for wallet trade history (`/activity`) and discovery (`/trades`). Returns 400 for `offset > 3500` (API hard limit on deep pagination).
- **`GammaClient`** — used only for Telegram signal alerts (market question text). Do not use for market resolution.
- **`ClobClient`** — optional authenticated CLOB endpoints; not used in current hot path.

All clients are async context managers with built-in rate limiting (`asyncio.Semaphore`) and `tenacity` retry on transient errors.
