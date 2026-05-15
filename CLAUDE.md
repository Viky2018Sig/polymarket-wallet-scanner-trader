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

# Send Telegram performance report (main scanner pipeline)
python main.py notify

# Performance report to stdout
python main.py report

# Live trading via main pipeline (real CLOB orders)
python main.py live-trade           # continuous loop (30-min cycles)
python main.py live-trade --once    # single cycle
python main.py live-trade --status  # print portfolio summary, no trading

# Database must be initialised before first use
python main.py init-db
```

All commands accept `--log-level DEBUG` and `--log-file <path>` options. Logs go to `./logs/scanner.log`.

## FastCopier (sub-second mode)

A separate self-contained bot that bypasses the signal pipeline entirely. Runs from `fast_copier.py` and writes to `fast_copier.db` — completely independent of `polymarket_scanner.db`.

```bash
# Paper mode (default) — no real orders placed
python fast_copier.py --bankroll 750 --max-price 0.20

# Live mode — real CLOB orders, FOK first then GTC fallback
python fast_copier.py --bankroll 750 --max-price 0.20 \
  --live --live-env /root/copybot-live/polymarket-copybot/.env \
  --live-max-bet 5.0

# Profit-lock trailing stop (defaults shown)
python fast_copier.py --live ... \
  --profit-lock-at 2.0 \    # activate when bid hits 2× entry
  --profit-lock-trail 0.50  # sell when bid drops to 50% of peak
                             # disable with --profit-lock-at 0

# Telegram portfolio report (run from NJ VPS, uses fast_copier.db)
python fast_report.py --bankroll 750 --live-bankroll 100 \
  --live-env /root/copybot-live/polymarket-copybot/.env

# Quick DB stats check
sqlite3 fast_copier.db "SELECT status, COUNT(*), ROUND(SUM(COALESCE(pnl,0)),2) FROM fast_trades GROUP BY status"
```

### FastCopier workers (all run concurrently in `asyncio.gather`)

| Worker | Interval | Purpose |
|---|---|---|
| `_ws_worker` | continuous | CLOB WebSocket, `last_trade_price` BUY events |
| `_trade_processor` | queue-driven | dedup → Kelly size → DB insert → live order |
| `_activity_poller` | 5 s | REST fallback for brand-new markets not yet in WS map |
| `_position_closer` | 5 min | Close at ask ≥ 0.99 (WIN) or ≤ 0.01 (LOSS) |
| `_profit_lock_worker` | **10 s** | Trailing take-profit: track peak bid, sell on pullback |
| `_asset_refresher` | 5 min | Reload asset_id map from scanner DB, trigger WS resubscribe |
| `_snapshot_worker` | 30 min | Log portfolio stats |

**Profit-lock logic**: every 10 s, `_profit_lock_worker` fetches the order book for each open position. It tracks `peak_price` (highest bid seen). Once `peak ≥ entry × profit_lock_at`, the trailing stop is armed. It fires when `current_bid ≤ peak × profit_lock_trail`, placing a GTC SELL order (live mode) and recording status `CLOSED_PROFIT_LOCK`. `_position_closer` handles `CLOSED_WIN`/`CLOSED_LOSS` independently at 5-min cadence.

### FastCopier live order flow

`_place_live_order()` — BUY: try FOK first (instant fill at `price × (1 + slippage)`); if the exchange kills it with "fok"/"fully filled" error (thin liquidity), fall back to GTC limit order. Logged as WARNING not ERROR.

`_place_live_sell()` — SELL: GTC limit at `bid × (1 - slippage)`. Called by `_profit_lock_worker` when trailing stop fires.

**Credentials**: read from `--live-env` file. Requires `PK`, `CLOB_API_KEY`, `CLOB_SECRET`, `CLOB_PASSPHRASE`, `SIG_TYPE`. If `CLOB_PROXY=socks5://127.0.0.1:1080` is set, the underlying HTTP client routes through it.

### Deployment (NJ VPS — low-latency server)

The FastCopier is deployed on a separate NJ VPS (not this main scanner server). It only **reads** `polymarket_scanner.db` (wallet scores + asset IDs) — never writes to it. The systemd service:

```ini
[Service]
ExecStart=/usr/bin/python3 fast_copier.py \
    --bankroll 750 --max-price 0.20 --max-pos-pct 0.005 --kelly 0.25 \
    --live --live-env /root/copybot-live/polymarket-copybot/.env \
    --live-max-bet 5.0 --profit-lock-at 2.0 --profit-lock-trail 0.50
Environment=CLOB_PROXY=socks5://127.0.0.1:1080
```

Cron on the NJ VPS (hourly Telegram report):
```
0 * * * * cd /root/polymarket-wallet-scanner-trader && python3 fast_report.py \
    --bankroll 750 --live-bankroll 100 \
    --live-env /root/copybot-live/polymarket-copybot/.env >> logs/fast_report.log 2>&1
```

## Cron schedule (main server)

```
7 * * * *    notify (hourly Telegram report — main pipeline)
23 */4 * * * scan --skip-discovery (every 4h FIFO refresh, required for auto-resolution)
47 4 * * *   prune (daily DB vacuum — keeps disk bounded)
```

## Architecture

**Two independent runtime modes** on the main server:

**`realtime`** — Preferred monitor. Five concurrent async workers: `_ws_worker` (CLOB WebSocket, subscribes to 500 most-recent asset IDs, ~50ms event latency), `_trade_fetcher` (REST GET `/trades?asset=X` to identify maker wallet — ~200ms total detection latency), `_signal_processor` (guardrails: price range, market time remaining, price ceiling), `_housekeep_worker` (prunes DB every 6h), `_unfollow_checker` (refreshes WS subscriptions every 30min).

**`monitor`** — Legacy 30-min polling monitor. Still usable; `realtime` supersedes it.

**`paper-trade`** — Polls every 30 min. Reads unacted signals → opens `PaperTrade` rows (Kelly-sized, deduped by market+wallet) → calls `resolve_closed_markets()` → snapshot.

**Critical dependency**: `resolve_closed_markets()` requires `resolved_price` populated on `wallet_trades` rows, which only happens inside `scan --skip-discovery`. The 4-hourly cron closes this gap.

## Data flow

```
Data API (data-api.polymarket.com)
  └─ /activity?user=<address>       ← wallet trade history (paginated, max ~3500)
  └─ /trades                        ← global trade stream for discovery
  └─ /trades?asset=<token_id>       ← per-asset recent trades (WS trade fetcher)

CLOB WebSocket (ws-subscriptions-clob.polymarket.com/ws/market)
  └─ last_trade_price events        ← ~50ms trade detection, asset_id+price+side+tx_hash

CLOB REST (clob.polymarket.com)
  └─ /book?token_id=X               ← order book (bid/ask) — used by profit-lock worker
  └─ /last-trade-price?token_id=X   ← fallback mid-price when book is empty

wallet_trades → PnlCalculator.run() → resolved_price → PerformanceScorer → wallets (is_tracked=1)
  → signals → PaperTrader / LiveTrader
```

## Live trading (main pipeline)

`src/trader/live_trader.py` — places real FOK market orders on the CLOB via `py-clob-client-v2`. Reads unacted signals from the `signals` table.

**POLY_1271 patches**: required for SIG_TYPE=3 (ERC-7739 deposit wallet signing):
```bash
SITE_PKGS=$(python3 -c "import py_clob_client_v2,os; print(os.path.dirname(py_clob_client_v2.__file__))")
cp /root/copybot-live/polymarket-copybot/patches/exchange_order_builder_v2.py $SITE_PKGS/order_utils/
cp /root/copybot-live/polymarket-copybot/patches/order_builder_builder.py $SITE_PKGS/order_builder/builder.py
```

**Position management**: stop-loss (only if entry ≥ 0.30, triggers at 20% loss), take-profit (0.80 for entries < 0.30; +50% for entries ≥ 0.30). Positions held to resolution at 0.99/0.01.

**State**: persisted to `settings.live_state_file` (default: `data/live_state.json`).

## Key design decisions

**Gamma API avoidance**: `gamma-api.polymarket.com/markets/{hex_id}` returns 422 for hex condition IDs. Market resolution does NOT use Gamma API. Instead, it checks whether the tracked wallet has fully exited the position in `wallet_trades` (all BUY rows have `resolved_price` set from FIFO matching).

**FIFO PnL calculation**: `PnlCalculator` groups trades by `(wallet_address, market_id, outcome)` and matches SELLs against chronologically-earliest BUYs. The SELL price is written to `resolved_price` on the matched BUY row.

**Signal freshness**: Signals older than 60 min are discarded. Price drift > 1% from signal entry price also discards the signal.

**`_last_seen` seeding**: On `monitor` startup, `SignalEngine` seeds from the DB's most recent signal time per wallet. Without this, every restart replays ~24h of historical trades.

**Deduplication**: `_open_trade()` in FastCopier uses `INSERT OR IGNORE` on `UNIQUE(market_id, wallet_followed)`. The in-memory `_open_keys` set provides a fast pre-check.

**Bankroll reconstruction**: `take_daily_snapshot()` recomputes bankroll as `starting_bankroll + sum(realised_pnl)` — never accumulates from previous snapshots.

## Configuration

All settings in `config.py` (`Settings` class, pydantic-settings), overridable via `.env`.

| Setting | Default | Notes |
|---|---|---|
| `STARTING_BANKROLL` | 2000 | USD |
| `MAX_POSITION_PCT` | 0.0025 | 0.25% of bankroll per trade ≈ $5 |
| `KELLY_FRACTION` | 0.25 | Quarter Kelly safety multiplier |
| `LOW_PRICE_MIN_PCT` | 0.10 | Min fraction of wallet's trades in 0.01–0.15 range |
| `MIN_PROFIT_FACTOR` | 2.0 | Qualification threshold |
| `MIN_TRADES_REQUIRED` | 20 | Min resolved trades to qualify |
| `LOOKBACK_DAYS` | 90 | Trade history window |
| `MAX_COPY_PRICE_MULTIPLIER` | 4.0 | Skip if ask > signal_price × this |
| `MIN_MARKET_SECONDS_REMAINING` | 60 | Skip market if fewer than this many seconds remain |

FastCopier has no `config.py` — all settings are CLI flags passed to `fast_copier.py`.

## Databases

**`polymarket_scanner.db`** — main pipeline. SQLite WAL. Tables: `wallets`, `wallet_trades`, `signals`, `paper_trades`, `portfolio_snapshots`. Schema auto-migrated in `Database._migrate()` on every connect.

**`fast_copier.db`** — FastCopier only. Key columns in `fast_trades`:
- `order_id TEXT` — CLOB order ID if live order was placed (NULL = paper)
- `peak_price REAL` — highest bid seen since entry (used by profit-lock)
- `tp_order_id TEXT` — CLOB sell order ID placed by profit-lock
- `status` values: `OPEN`, `CLOSED_WIN`, `CLOSED_LOSS`, `CLOSED_PROFIT_LOCK`

All DB access is async via `aiosqlite`.

## API clients

- **`DataApiClient`** — primary, public, no auth. Returns 400 for `offset > 3500` (API hard limit).
- **`GammaClient`** — used only for Telegram signal alerts (market question text). Not for resolution.
- **`ClobClient`** (py-clob-client-v2) — authenticated. Used for live order placement in both `live_trader.py` and `fast_copier.py`. The FastCopier builds the client lazily in `_build_live_client()` and resets `self._clob_client = None` on auth errors to force a rebuild.
