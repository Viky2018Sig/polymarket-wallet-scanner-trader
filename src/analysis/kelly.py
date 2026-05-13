"""
Kelly criterion position sizing for binary Polymarket bets.

For a binary bet at price `p` (probability implied by market):
  - Win probability: w  (wallet's historical win rate for this price bucket)
  - If win: gain (1 - p) per dollar risked (we bought at p, resolves to 1.0)
  - If lose: lose p per dollar risked (position expires worthless)

Full Kelly formula for binary:
  f* = (w * (1 - p) - (1 - w) * p) / (1 - p)
     = w - (1 - w) * p / (1 - p)

We then apply:
  1. Fractional Kelly safety factor (default: 0.25)
  2. Price-scaled multiplier: lower entry price → higher R:R → allow slightly
     larger fraction (smoothly scales from 1.0 at 0.15 down to 0.5 at 0.01)
  3. Hard cap at MAX_POSITION_PCT (default 3%) of bankroll
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from loguru import logger

from config import settings
from src.api.models import KellyResult


def calculate_kelly(
    win_rate: float,
    entry_price: float,
    bankroll: Decimal,
    kelly_fraction: Optional[float] = None,
    max_position_pct: Optional[float] = None,
) -> KellyResult:
    """
    Calculate Kelly-optimal position size for a binary Polymarket bet.

    Args:
        win_rate: Wallet's historical win rate for this price bucket (0–1).
        entry_price: Entry price of the position (0.01–0.99).
        bankroll: Current paper trading bankroll in USD.
        kelly_fraction: Safety multiplier (defaults to settings.kelly_fraction).
        max_position_pct: Hard position cap as fraction (defaults to settings.max_position_pct).

    Returns:
        KellyResult with recommended_fraction and dollar_amount.
    """
    kf = kelly_fraction if kelly_fraction is not None else settings.kelly_fraction
    max_pct = max_position_pct if max_position_pct is not None else settings.max_position_pct

    # Clamp inputs to valid ranges
    win_rate = max(0.0, min(1.0, win_rate))
    price = max(0.001, min(0.999, entry_price))

    # ── Full Kelly calculation ─────────────────────────────────────────────────
    # For a binary bet:
    #   b = (1 - p) / p  (net odds per unit risked)
    #   f* = (w * b - (1 - w)) / b  = w - (1 - w) / b  [classic form]
    # Simplified:
    #   f* = (w * (1 - p) - (1 - w) * p) / (1 - p)
    numerator = win_rate * (1.0 - price) - (1.0 - win_rate) * price
    denominator = 1.0 - price

    if denominator <= 0:
        full_kelly = 0.0
    else:
        full_kelly = numerator / denominator

    # Negative Kelly = negative expected value → don't bet
    full_kelly = max(0.0, full_kelly)

    # ── Fractional Kelly ───────────────────────────────────────────────────────
    fractional_kelly = full_kelly * kf

    # ── Price-scaled multiplier ────────────────────────────────────────────────
    # Lower entry price = higher R:R potential.
    # Scale: price=0.01 → multiplier=1.5, price=0.15 → multiplier=1.0
    # Linear interpolation capped at [1.0, 1.5]
    lo, hi = settings.low_price_min, settings.low_price_max
    if price <= lo:
        price_multiplier = 1.5
    elif price >= hi:
        price_multiplier = 1.0
    else:
        # Linear: goes from 1.5 at lo to 1.0 at hi
        t = (price - lo) / (hi - lo)
        price_multiplier = 1.5 - 0.5 * t

    price_scaled_kelly = fractional_kelly * price_multiplier

    # Hard cap at max_position_pct of bankroll
    capped_kelly = min(price_scaled_kelly, max_pct)
    recommended_fraction = round(capped_kelly, 6)

    # ── Dollar amount ──────────────────────────────────────────────────────────
    dollar_amount = (Decimal(str(recommended_fraction)) * bankroll).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )

    # Build notes
    notes_parts = []
    if win_rate <= 0.5:
        notes_parts.append(f"low_win_rate({win_rate:.1%})")
    if full_kelly <= 0:
        notes_parts.append("negative_ev")
    if price_scaled_kelly > max_pct:
        notes_parts.append(f"capped_from_{price_scaled_kelly:.3f}")
    notes = "; ".join(notes_parts) if notes_parts else "standard"

    result = KellyResult(
        win_rate=win_rate,
        price=price,
        full_kelly=round(full_kelly, 6),
        fractional_kelly=round(fractional_kelly, 6),
        price_scaled_kelly=round(price_scaled_kelly, 6),
        capped_kelly=round(capped_kelly, 6),
        recommended_fraction=recommended_fraction,
        dollar_amount=dollar_amount,
        bankroll=bankroll,
        notes=notes,
    )

    logger.debug(
        f"Kelly: price={price:.3f} win_rate={win_rate:.1%} "
        f"full_k={full_kelly:.4f} frac_k={fractional_kelly:.4f} "
        f"scaled={price_scaled_kelly:.4f} final={recommended_fraction:.4f} "
        f"${dollar_amount}"
    )
    return result


def kelly_for_price_bucket(
    price_bucket: str,
    wallet_win_rates: dict,
    entry_price: float,
    bankroll: Decimal,
) -> KellyResult:
    """
    Look up win rate for the specific price bucket and compute Kelly size.

    Args:
        price_bucket: Label like "0.01-0.05", "0.05-0.10", etc.
        wallet_win_rates: Dict mapping bucket label → win rate.
        entry_price: Actual entry price.
        bankroll: Current bankroll.
    """
    win_rate = wallet_win_rates.get(price_bucket, 0.0)

    # If we don't have bucket-level data, use a conservative default
    if win_rate == 0.0:
        # Low-price bets tend to have low win rates but high R:R
        # Use a conservative 30% as prior when no data
        win_rate = 0.30
        logger.debug(
            f"No win rate data for bucket {price_bucket}, using prior 0.30"
        )

    return calculate_kelly(
        win_rate=win_rate,
        entry_price=entry_price,
        bankroll=bankroll,
    )


def expected_value(win_rate: float, price: float) -> float:
    """
    Compute expected value of a binary bet.
    EV = w * (1 - p) - (1 - w) * p
    Positive EV required for Kelly to recommend a non-zero bet.
    """
    return win_rate * (1.0 - price) - (1.0 - win_rate) * price


def optimal_bet_price(win_rate: float) -> float:
    """
    Find the maximum price at which a bet has positive EV given a win rate.
    EV = 0  →  w*(1-p) = (1-w)*p  →  w - w*p = p - w*p  →  p = w
    (At break-even, market price equals our estimated probability.)
    Returns the break-even price; buy only at price < win_rate.
    """
    return win_rate
