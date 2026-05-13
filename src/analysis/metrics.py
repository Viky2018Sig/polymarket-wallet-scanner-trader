"""
Performance metric calculations for wallet scoring.

Computes profit factor, win rate, R:R, drawdown, recency score,
market diversity, and composite score from a list of WalletTrade objects.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, List, Optional, Tuple

from loguru import logger

from config import settings
from src.api.models import WalletScore, WalletTrade


def compute_wallet_metrics(
    wallet_address: str,
    trades: List[WalletTrade],
) -> WalletScore:
    """
    Main metric computation function.

    Takes all trades for a wallet (BUYs and SELLs, resolved and open)
    and returns a fully-populated WalletScore.
    """
    if not trades:
        return WalletScore(
            wallet_address=wallet_address,
            qualifies=False,
            disqualify_reason="no_trades",
        )

    # Sort chronologically
    sorted_trades = sorted(trades, key=lambda t: t.matched_at)

    # ── Basic counts ───────────────────────────────────────────────────────────
    buy_trades = [t for t in sorted_trades if t.side.upper() == "BUY"]
    total_trades = len(buy_trades)

    # ── Low-price percentage ───────────────────────────────────────────────────
    low_price_trades = [t for t in buy_trades if t.is_low_price]
    low_price_pct = len(low_price_trades) / total_trades if total_trades > 0 else 0.0

    # ── Resolved trade analysis ────────────────────────────────────────────────
    resolved_buys = [t for t in buy_trades if t.resolved_price is not None]
    resolved_count = len(resolved_buys)

    winning_trades: List[WalletTrade] = []
    losing_trades: List[WalletTrade] = []
    gross_profit = Decimal("0")
    gross_loss = Decimal("0")

    for t in resolved_buys:
        if t.resolved_price is None or t.pnl is None:
            # Compute PnL from resolved price
            pnl = _compute_trade_pnl(t)
        else:
            pnl = t.pnl

        if pnl > Decimal("0"):
            winning_trades.append(t)
            gross_profit += pnl
        else:
            losing_trades.append(t)
            gross_loss += abs(pnl)

    # Profit factor
    if gross_loss > Decimal("0"):
        profit_factor = float(gross_profit / gross_loss)
    elif gross_profit > Decimal("0"):
        profit_factor = 10.0  # All wins, cap at 10
    else:
        profit_factor = 0.0

    win_rate = len(winning_trades) / resolved_count if resolved_count > 0 else 0.0

    # Average R:R on winning trades
    avg_rr = _compute_avg_rr(winning_trades, losing_trades)

    # ── Drawdown ───────────────────────────────────────────────────────────────
    max_drawdown = _compute_max_drawdown(sorted_trades)

    # ── Recency score ──────────────────────────────────────────────────────────
    recency_score = _compute_recency_score(buy_trades)

    # ── Market diversity ───────────────────────────────────────────────────────
    diversity_score = _compute_diversity_score(buy_trades)

    # ── Composite score ────────────────────────────────────────────────────────
    composite = _compute_composite_score(
        profit_factor=profit_factor,
        win_rate=win_rate,
        low_price_pct=low_price_pct,
        recency_score=recency_score,
        diversity_score=diversity_score,
    )

    # ── Active markets count ───────────────────────────────────────────────────
    active_markets = len({t.market_id for t in buy_trades})

    # ── Dates ─────────────────────────────────────────────────────────────────
    first_trade = sorted_trades[0].matched_at if sorted_trades else None
    last_trade = sorted_trades[-1].matched_at if sorted_trades else None

    # ── Total PnL ──────────────────────────────────────────────────────────────
    total_pnl = gross_profit - gross_loss

    return WalletScore(
        wallet_address=wallet_address,
        total_trades=total_trades,
        resolved_trades=resolved_count,
        winning_trades=len(winning_trades),
        losing_trades=len(losing_trades),
        win_rate=win_rate,
        profit_factor=min(profit_factor, 50.0),  # cap extreme values
        avg_rr=avg_rr,
        low_price_pct=low_price_pct,
        max_drawdown=max_drawdown,
        recency_score=recency_score,
        diversity_score=diversity_score,
        composite_score=composite,
        total_pnl=total_pnl,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        active_markets=active_markets,
        last_trade_at=last_trade,
        first_trade_at=first_trade,
    )


def _compute_trade_pnl(trade: WalletTrade) -> Decimal:
    """
    Estimate PnL for a BUY trade.

    If resolved_price is 1.0 (YES wins) we receive 1.0 per share;
    if 0.0 (NO wins / lose) we receive 0.0.
    PnL = (resolved_price - entry_price) * shares
    """
    if trade.resolved_price is None:
        return Decimal("0")

    pnl = (trade.resolved_price - trade.price) * trade.size
    return pnl.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _compute_avg_rr(
    winners: List[WalletTrade],
    losers: List[WalletTrade],
) -> float:
    """
    Average risk/reward on winning trades.

    For low-price binary bets:
    - Risk per dollar = entry_price (lose it if wrong)
    - Reward per dollar = (1 - entry_price) (gain this if right)
    - R:R = (1 - price) / price

    We take average across winning trades weighted by notional.
    """
    if not winners:
        return 0.0

    total_weight = Decimal("0")
    weighted_rr = Decimal("0")

    for t in winners:
        if t.price <= Decimal("0"):
            continue
        rr = (Decimal("1") - t.price) / t.price
        weight = t.notional_usd
        weighted_rr += rr * weight
        total_weight += weight

    if total_weight == Decimal("0"):
        return 0.0

    return float(weighted_rr / total_weight)


def _compute_max_drawdown(trades: List[WalletTrade]) -> float:
    """
    Estimate max drawdown from a sequence of resolved trades.

    Uses a running equity curve built from resolved buy PnLs.
    Returns drawdown as a positive fraction (e.g. 0.25 = 25% drawdown).
    """
    equity_curve: List[Decimal] = [Decimal("0")]
    running = Decimal("0")

    for t in trades:
        if t.side.upper() != "BUY" or t.resolved_price is None:
            continue
        pnl = t.pnl if t.pnl is not None else _compute_trade_pnl(t)
        running += pnl
        equity_curve.append(running)

    if len(equity_curve) < 2:
        return 0.0

    peak = equity_curve[0]
    max_dd = 0.0

    for value in equity_curve:
        if value > peak:
            peak = value
        if peak != Decimal("0") and peak > Decimal("0"):
            dd = float((peak - value) / peak)
            max_dd = max(max_dd, dd)

    return max_dd


def _compute_recency_score(trades: List[WalletTrade]) -> float:
    """
    Recency score: weight last-30-day trades 2x vs 31-90 day trades.
    Normalises to 0–1 range based on activity density.
    """
    if not trades:
        return 0.0

    now = datetime.utcnow()
    cutoff_recent = now - timedelta(days=30)
    cutoff_old = now - timedelta(days=90)

    recent_count = sum(1 for t in trades if t.matched_at >= cutoff_recent)
    older_count = sum(
        1 for t in trades if cutoff_old <= t.matched_at < cutoff_recent
    )

    # Weighted activity
    weighted = recent_count * 2 + older_count * 1
    # Normalise: 60 weighted trades = full score (arbitrary scale)
    score = min(weighted / 60.0, 1.0)
    return score


def _compute_diversity_score(buy_trades: List[WalletTrade]) -> float:
    """
    Market diversity score: penalise wallets that concentrate in one market.
    Uses normalised entropy across market IDs.

    Score = 1.0 if perfectly diversified, 0.0 if all in one market.
    """
    if not buy_trades:
        return 0.0

    market_counts: Dict[str, int] = defaultdict(int)
    for t in buy_trades:
        market_counts[t.market_id] += 1

    n_markets = len(market_counts)
    if n_markets == 1:
        return 0.0
    if n_markets >= 10:
        return 1.0

    # Herfindahl–Hirschman Index (lower = more diverse)
    total = sum(market_counts.values())
    hhi = sum((c / total) ** 2 for c in market_counts.values())
    # HHI of 1/n_markets is perfectly equal; HHI of 1.0 is monopoly
    # Normalise: diversity = 1 - (HHI - 1/n) / (1 - 1/n)
    min_hhi = 1.0 / n_markets
    if abs(1.0 - min_hhi) < 1e-9:
        return 1.0
    diversity = 1.0 - (hhi - min_hhi) / (1.0 - min_hhi)
    return max(0.0, min(1.0, diversity))


def _compute_composite_score(
    profit_factor: float,
    win_rate: float,
    low_price_pct: float,
    recency_score: float,
    diversity_score: float,
) -> float:
    """
    Weighted composite score normalised to 0–1.

    Weights from settings:
    - profit_factor:  30% (normalised, cap at 5x → 1.0)
    - win_rate:       20%
    - low_price_pct:  20%
    - recency_score:  15%
    - diversity_score:15%
    """
    norm_pf = min(profit_factor / 5.0, 1.0)  # 5x = perfect
    norm_wr = min(win_rate, 1.0)
    norm_lp = min(low_price_pct, 1.0)
    norm_rec = recency_score
    norm_div = diversity_score

    composite = (
        settings.weight_profit_factor * norm_pf
        + settings.weight_win_rate * norm_wr
        + settings.weight_low_price_pct * norm_lp
        + settings.weight_recency * norm_rec
        + settings.weight_diversity * norm_div
    )
    return round(composite, 4)


def compute_price_bucket_stats(
    trades: List[WalletTrade],
) -> Dict[str, Dict[str, float]]:
    """
    Break down trade counts and win rates per price bucket.
    Returns dict keyed by bucket label.
    """
    buckets = settings.price_buckets
    stats: Dict[str, Dict[str, float]] = {}

    for lo, hi in buckets:
        label = f"{lo:.2f}-{hi:.2f}"
        bucket_trades = [
            t for t in trades
            if t.side.upper() == "BUY" and lo <= float(t.price) < hi
        ]
        resolved = [t for t in bucket_trades if t.resolved_price is not None]
        wins = [
            t for t in resolved
            if (t.pnl or _compute_trade_pnl(t)) > Decimal("0")
        ]
        stats[label] = {
            "count": len(bucket_trades),
            "resolved": len(resolved),
            "wins": len(wins),
            "win_rate": len(wins) / len(resolved) if resolved else 0.0,
        }

    return stats
