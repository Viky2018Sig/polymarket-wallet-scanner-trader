"""
Pydantic v2 models for Polymarket API responses and internal data structures.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


# ── Enums ──────────────────────────────────────────────────────────────────────

class TradeType(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class PositionSide(str, Enum):
    YES = "YES"
    NO = "NO"


class MarketStatus(str, Enum):
    ACTIVE = "active"
    CLOSED = "closed"
    RESOLVED = "resolved"
    ARCHIVED = "archived"


class SignalType(str, Enum):
    BUY = "BUY"
    CLOSE = "CLOSE"
    ALERT = "ALERT"


class PaperTradeStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED_WIN = "CLOSED_WIN"
    CLOSED_LOSS = "CLOSED_LOSS"
    CLOSED_PARTIAL = "CLOSED_PARTIAL"


# ── Data API Models ────────────────────────────────────────────────────────────

class DataApiTrade(BaseModel):
    """
    A trade record from data-api.polymarket.com/trades or /activity.
    Field names match the actual API response (camelCase aliased).
    """

    proxy_wallet: Optional[str] = Field(default=None, alias="proxyWallet")
    condition_id: Optional[str] = Field(default=None, alias="conditionId")
    side: Optional[str] = None          # "buy" or "sell"
    price: Optional[float] = None
    size: Optional[float] = None        # USD amount
    timestamp: Optional[Any] = None     # unix int or ISO string
    outcome: Optional[str] = None       # "Yes" / "No"
    transaction_hash: Optional[str] = Field(default=None, alias="transactionHash")
    asset: Optional[str] = None         # token ID
    title: Optional[str] = None
    slug: Optional[str] = None
    pseudonym: Optional[str] = None

    model_config = {"populate_by_name": True, "arbitrary_types_allowed": True}

    @property
    def timestamp_unix(self) -> Optional[int]:
        if self.timestamp is None:
            return None
        try:
            return int(float(str(self.timestamp)))
        except (ValueError, TypeError):
            return None

    @property
    def matched_at(self) -> Optional[datetime]:
        ts = self.timestamp_unix
        if ts is None:
            return None
        try:
            return datetime.utcfromtimestamp(ts)
        except (OSError, ValueError):
            return None


# ── Gamma API Models ───────────────────────────────────────────────────────────

class GammaMarket(BaseModel):
    """A Polymarket market from the Gamma API."""

    id: str
    question: str = ""
    condition_id: Optional[str] = Field(default=None, alias="conditionId")
    slug: Optional[str] = None
    description: Optional[str] = None
    end_date_iso: Optional[str] = Field(default=None, alias="endDateIso")
    game_start_time: Optional[str] = Field(default=None, alias="gameStartTime")
    market_type: Optional[str] = Field(default=None, alias="marketType")
    resolution_source: Optional[str] = Field(default=None, alias="resolutionSource")
    category: Optional[str] = None
    active: bool = True
    closed: bool = False
    archived: bool = False
    new: bool = False
    featured: bool = False
    restricted: bool = False
    liquidity: float = 0.0
    volume: float = 0.0
    volume_24hr: float = Field(default=0.0, alias="volume24hr")
    best_bid: Optional[float] = Field(default=None, alias="bestBid")
    best_ask: Optional[float] = Field(default=None, alias="bestAsk")
    last_trade_price: Optional[float] = Field(default=None, alias="lastTradePrice")
    outcome_prices: Optional[str] = Field(default=None, alias="outcomePrices")
    clob_token_ids: Optional[str] = Field(default=None, alias="clobTokenIds")

    model_config = {"populate_by_name": True}

    @property
    def status(self) -> MarketStatus:
        if self.archived:
            return MarketStatus.ARCHIVED
        if self.closed:
            return MarketStatus.CLOSED
        return MarketStatus.ACTIVE


class GammaPosition(BaseModel):
    """A user position from the Gamma API."""

    id: Optional[str] = None
    proxy_wallet: Optional[str] = Field(default=None, alias="proxyWallet")
    user: Optional[str] = None
    market: Optional[str] = None
    asset: Optional[str] = None
    outcome: Optional[str] = None
    size: float = 0.0
    avg_price: float = Field(default=0.0, alias="avgPrice")
    pnl: float = 0.0
    realized_pnl: float = Field(default=0.0, alias="realizedPnl")
    unrealized_pnl: float = Field(default=0.0, alias="unrealizedPnl")
    cur_price: Optional[float] = Field(default=None, alias="curPrice")
    initial_value: float = Field(default=0.0, alias="initialValue")
    current_value: float = Field(default=0.0, alias="currentValue")

    model_config = {"populate_by_name": True}


# ── CLOB API Models ────────────────────────────────────────────────────────────

class ClobTrade(BaseModel):
    """A single trade from the CLOB API."""

    id: Optional[str] = None
    taker_order_id: Optional[str] = Field(default=None, alias="takerOrderId")
    market: Optional[str] = None
    asset_id: Optional[str] = Field(default=None, alias="asset_id")
    side: Optional[str] = None
    size: Optional[str] = None
    fee_rate_bps: Optional[str] = Field(default=None, alias="fee_rate_bps")
    price: Optional[str] = None
    status: Optional[str] = None
    match_time: Optional[str] = Field(default=None, alias="match_time")
    last_update: Optional[str] = Field(default=None, alias="last_update")
    outcome: Optional[str] = None
    bucket_index: Optional[str] = Field(default=None, alias="bucket_index")
    owner: Optional[str] = None
    maker_address: Optional[str] = Field(default=None, alias="maker_address")
    transaction_hash: Optional[str] = Field(default=None, alias="transaction_hash")
    type: Optional[str] = None

    model_config = {"populate_by_name": True}

    @property
    def price_float(self) -> float:
        try:
            return float(self.price or 0)
        except (ValueError, TypeError):
            return 0.0

    @property
    def size_float(self) -> float:
        try:
            return float(self.size or 0)
        except (ValueError, TypeError):
            return 0.0

    @property
    def match_datetime(self) -> Optional[datetime]:
        if not self.match_time:
            return None
        try:
            ts = float(self.match_time)
            return datetime.utcfromtimestamp(ts)
        except (ValueError, TypeError):
            try:
                return datetime.fromisoformat(self.match_time.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                return None

    @property
    def notional_usd(self) -> float:
        return self.price_float * self.size_float


class ClobOrderBook(BaseModel):
    """Order book snapshot from CLOB API."""

    market: Optional[str] = None
    asset_id: Optional[str] = Field(default=None, alias="asset_id")
    bids: List[Dict[str, str]] = Field(default_factory=list)
    asks: List[Dict[str, str]] = Field(default_factory=list)
    hash: Optional[str] = None
    timestamp: Optional[str] = None

    model_config = {"populate_by_name": True}

    @property
    def best_bid(self) -> Optional[float]:
        if self.bids:
            try:
                return float(self.bids[0].get("price", 0))
            except (ValueError, KeyError):
                return None
        return None

    @property
    def best_ask(self) -> Optional[float]:
        if self.asks:
            try:
                return float(self.asks[0].get("price", 0))
            except (ValueError, KeyError):
                return None
        return None


# ── Internal domain models ─────────────────────────────────────────────────────

class WalletTrade(BaseModel):
    """Normalised trade record stored in our DB."""

    trade_id: str
    wallet_address: str
    market_id: str
    asset_id: Optional[str] = None
    side: str  # BUY / SELL
    price: Decimal
    size: Decimal
    notional_usd: Decimal
    matched_at: datetime
    outcome: Optional[str] = None
    resolved_price: Optional[Decimal] = None  # 1.0 = YES wins, 0.0 = NO wins
    pnl: Optional[Decimal] = None  # populated after resolution

    model_config = {"arbitrary_types_allowed": True}

    @property
    def is_low_price(self) -> bool:
        return Decimal("0.01") <= self.price <= Decimal("0.15")

    @property
    def price_bucket(self) -> str:
        p = float(self.price)
        if p < 0.05:
            return "0.01-0.05"
        if p < 0.10:
            return "0.05-0.10"
        if p < 0.15:
            return "0.10-0.15"
        if p < 0.30:
            return "0.15-0.30"
        return "0.30-0.70"


class WalletScore(BaseModel):
    """Computed performance metrics and composite score for a wallet."""

    wallet_address: str
    total_trades: int = 0
    resolved_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    avg_rr: float = 0.0          # average R:R on winning trades
    low_price_pct: float = 0.0   # % trades in 0.01–0.15 range
    max_drawdown: float = 0.0    # as fraction (e.g. 0.20 = 20%)
    recency_score: float = 0.0   # 0–1 score weighting recent trades
    diversity_score: float = 0.0 # 0–1 how spread across markets
    composite_score: float = 0.0
    total_pnl: Decimal = Decimal("0")
    gross_profit: Decimal = Decimal("0")
    gross_loss: Decimal = Decimal("0")
    active_markets: int = 0
    last_trade_at: Optional[datetime] = None
    first_trade_at: Optional[datetime] = None
    qualifies: bool = False
    disqualify_reason: Optional[str] = None
    scored_at: datetime = Field(default_factory=datetime.utcnow)

    model_config = {"arbitrary_types_allowed": True}


class Signal(BaseModel):
    """A trading signal generated by the signal engine."""

    id: Optional[int] = None
    signal_type: SignalType
    wallet_address: str
    market_id: str
    asset_id: Optional[str] = None
    price: Decimal
    kelly_fraction: float
    recommended_size_usd: Decimal
    wallet_win_rate: float
    wallet_profit_factor: float
    price_bucket: str
    notes: str = ""
    generated_at: datetime = Field(default_factory=datetime.utcnow)

    model_config = {"arbitrary_types_allowed": True}


class PaperTrade(BaseModel):
    """A paper trade record."""

    id: Optional[int] = None
    signal_id: Optional[int] = None
    market_id: str
    asset_id: Optional[str] = None
    wallet_followed: str
    side: str = "BUY"
    entry_price: Decimal
    shares: Decimal
    dollar_amount: Decimal
    kelly_fraction_used: float
    price_bucket: str
    status: PaperTradeStatus = PaperTradeStatus.OPEN
    exit_price: Optional[Decimal] = None
    pnl: Optional[Decimal] = None
    pnl_pct: Optional[float] = None
    opened_at: datetime = Field(default_factory=datetime.utcnow)
    closed_at: Optional[datetime] = None
    notes: str = ""

    model_config = {"arbitrary_types_allowed": True}


class PortfolioSnapshot(BaseModel):
    """Daily snapshot of paper trading portfolio state."""

    id: Optional[int] = None
    snapshot_date: datetime
    bankroll: Decimal
    total_pnl: Decimal
    open_positions_value: Decimal
    closed_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    profit_factor: float
    max_drawdown_to_date: float

    model_config = {"arbitrary_types_allowed": True}


class KellyResult(BaseModel):
    """Output from Kelly criterion calculation."""

    win_rate: float
    price: float
    full_kelly: float
    fractional_kelly: float
    price_scaled_kelly: float
    capped_kelly: float
    recommended_fraction: float
    dollar_amount: Decimal
    bankroll: Decimal
    notes: str = ""

    model_config = {"arbitrary_types_allowed": True}
