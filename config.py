"""
Central configuration for Polymarket Wallet Scanner Trader.
All values can be overridden via environment variables or .env file.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import List, Tuple

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Paper trading ──────────────────────────────────────────────────────────
    starting_bankroll: float = Field(default=10_000.0, description="Starting bankroll in USD")
    kelly_fraction: float = Field(default=0.25, description="Kelly safety multiplier (0.25 = quarter Kelly)")
    max_position_pct: float = Field(default=0.03, description="Hard cap on position size as fraction of bankroll")

    # ── Wallet qualification thresholds ───────────────────────────────────────
    min_profit_factor: float = Field(default=2.0, description="Minimum profit factor to qualify wallet")
    min_trades_required: int = Field(default=20, description="Minimum completed trades for statistical significance")
    low_price_min_pct: float = Field(default=0.10, description="Min fraction of trades in low-price range")
    max_drawdown_threshold: float = Field(default=0.40, description="Max allowed drawdown (0.40 = 40%)")

    # ── Scanning parameters ────────────────────────────────────────────────────
    lookback_days: int = Field(default=90, description="Days to look back for trade history")
    scan_interval_minutes: int = Field(default=30, description="Minutes between scan cycles")
    max_tracked_wallets: int = Field(default=50, description="Max wallets to actively follow")

    # ── Price range definitions ────────────────────────────────────────────────
    low_price_min: float = Field(default=0.01, description="Low price range lower bound")
    low_price_max: float = Field(default=0.15, description="Low price range upper bound")

    # ── Storage ────────────────────────────────────────────────────────────────
    database_path: str = Field(default="./polymarket_scanner.db", description="SQLite database file path")

    # ── Logging ────────────────────────────────────────────────────────────────
    log_level: str = Field(default="INFO", description="Logging level")
    log_file: str = Field(default="", description="Log file path (empty = stdout only)")

    # ── Rate limiting ──────────────────────────────────────────────────────────
    max_requests_per_second: float = Field(default=5.0, description="Max API requests per second")
    http_timeout_seconds: float = Field(default=30.0, description="HTTP request timeout in seconds")
    max_retries: int = Field(default=3, description="Maximum retry attempts for failed requests")

    # ── Polymarket API key (optional — enables authenticated CLOB endpoints) ────
    polymarket_api_key: str = Field(default="", description="Polymarket API key (Bearer token for CLOB)")

    # ── Telegram notifications ─────────────────────────────────────────────────
    telegram_bot_token: str = Field(default="", description="Telegram Bot API token")
    telegram_chat_id: str = Field(default="", description="Telegram chat/channel ID to notify")

    # ── The Graph API ──────────────────────────────────────────────────────────
    graph_api_key: str = Field(default="", description="The Graph API key")

    # ── Real-time monitor ─────────────────────────────────────────────────────
    realtime_poll_seconds: int = Field(default=10, description="Seconds between poll cycles in realtime monitor (unused when WS is active)")
    ws_reconnect_max_delay: int = Field(default=60, description="Max seconds between WebSocket reconnect attempts")
    max_copy_price_multiplier: float = Field(
        default=4.0,
        description="Skip signal if current ask > signal_price × this (e.g. 4× means wallet bought 0.05, skip if ask > 0.20)",
    )
    signal_price_max: float = Field(
        default=0.20,
        description="Max entry price to trigger a copy signal. Wallet qualification still uses low_price_max (0.15). "
                    "Capped at 0.20 to stay focused on high R:R entries (5x+ potential).",
    )
    min_market_seconds_remaining: int = Field(
        default=60,
        description="Skip market if fewer than this many seconds remain before close",
    )

    # ── Data retention / disk management ──────────────────────────────────────
    wallet_trades_retention_days: int = Field(
        default=90,
        description="Days to keep resolved wallet_trades rows before pruning",
    )
    signals_retention_days: int = Field(
        default=7,
        description="Days to keep acted-on signal rows before pruning",
    )

    # ── Live trading ──────────────────────────────────────────────────────────
    live_env_file: str = Field(
        default="/root/copybot-live/polymarket-copybot/.env",
        description="Path to .env file with live trading credentials (PK, CLOB_API_KEY, etc.)",
    )
    live_state_file: str = Field(
        default="./data/live_state.json",
        description="Path to JSON file for persisting live trading state",
    )
    live_starting_balance: float = Field(
        default=100.0,
        description="Starting USDC balance for live trading (read from wallet on init)",
    )
    max_live_bet_usd: float = Field(
        default=5.0,
        description="Maximum USDC per single live order",
    )
    max_live_positions: int = Field(
        default=100,
        description="Maximum concurrent live positions. At $2/trade and $500 balance, 100 = $200 max deployed.",
    )
    live_stop_loss_pct: float = Field(
        default=0.20,
        description="Stop-loss threshold as fraction of entry price (0.20 = 20% loss)",
    )
    live_stop_loss_min_entry: float = Field(
        default=0.30,
        description="Stop-loss only applied when entry price >= this value (longshots held to resolution)",
    )
    live_take_profit_price: float = Field(
        default=0.80,
        description="Take-profit price for low-entry (<0.30) positions",
    )
    live_take_profit_gain_pct: float = Field(
        default=0.50,
        description="Take-profit trigger: percentage gain for mid-entry (>=0.30) positions",
    )
    live_max_position_age_days: int = Field(
        default=30,
        description="Auto-expire positions older than this many days",
    )
    live_clob_slippage: float = Field(
        default=0.005,
        description="Slippage buffer applied to limit price on FOK orders (0.005 = 0.5%)",
    )
    live_min_entry_price: float = Field(
        default=0.01,
        description="Minimum entry price for live trades. Raise to 0.05 to skip micro-longshots.",
    )
    live_max_entry_price: float = Field(
        default=0.10,
        description="Maximum entry price for live trades. 0.10 = 10x+ R:R only. "
                    "Set to 0.20 to include the full signal_price_max range.",
    )

    # ── Signal / follow management ────────────────────────────────────────────
    unfollow_profit_factor_threshold: float = Field(
        default=1.5,
        description="Unfollow wallet if last-10-trade profit factor drops below this",
    )
    unfollow_lookback_trades: int = Field(default=10, description="Trades window for unfollow check")

    # ── Composite score weights ────────────────────────────────────────────────
    weight_profit_factor: float = Field(default=0.30)
    weight_win_rate: float = Field(default=0.20)
    weight_low_price_pct: float = Field(default=0.20)
    weight_recency: float = Field(default=0.15)
    weight_diversity: float = Field(default=0.15)

    # ── Derived / constant values ──────────────────────────────────────────────
    @property
    def low_price_range(self) -> Tuple[float, float]:
        return (self.low_price_min, self.low_price_max)

    @property
    def price_buckets(self) -> List[Tuple[float, float]]:
        return [
            (0.01, 0.05),
            (0.05, 0.10),
            (0.10, 0.15),
            (0.15, 0.30),
            (0.30, 0.70),
        ]

    @property
    def db_path(self) -> Path:
        return Path(self.database_path)


# ── API base URLs ──────────────────────────────────────────────────────────────
GAMMA_API_BASE = "https://gamma-api.polymarket.com"
CLOB_API_BASE = "https://clob.polymarket.com"
DATA_API_BASE = "https://data-api.polymarket.com"
GRAPH_API_BASE = "https://api.thegraph.com/subgraphs/name/polymarket/matic-markets"

# Module-level singleton — import this everywhere
settings = Settings()
