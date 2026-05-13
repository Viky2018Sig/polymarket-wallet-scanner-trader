"""
Signal detection engine.

Monitors tracked wallets for new low-price entries and generates
BUY signals with Kelly-sized position recommendations.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Set, Tuple

from loguru import logger

from config import settings
from src.api.data_client import DataApiClient
from src.api.models import Signal, SignalType, WalletScore, WalletTrade
from src.analysis.kelly import calculate_kelly
from src.analysis.metrics import compute_price_bucket_stats
from src.storage.database import Database


class SignalEngine:
    """
    Detects new positions opened by tracked wallets in the low-price range
    and generates trading signals with Kelly-sized recommendations.
    """

    def __init__(
        self,
        clob_client: DataApiClient,
        db: Database,
        bankroll: Optional[Decimal] = None,
    ) -> None:
        self._clob = clob_client
        self._db = db
        self._bankroll = bankroll or Decimal(str(settings.starting_bankroll))
        # Track the most-recent trade timestamp we've seen per wallet
        # to avoid re-emitting signals for old trades.
        self._last_seen: Dict[str, datetime] = {}

    async def run_once(self) -> List[Signal]:
        """
        Single scan cycle: fetch new trades for all tracked wallets,
        emit signals for qualifying entries, return list of new signals.
        """
        tracked = await self._db.get_tracked_wallets()
        if not tracked:
            logger.info("No tracked wallets — run 'scan' first")
            return []

        # On first call, seed _last_seen from DB so we don't replay old signals
        if not self._last_seen:
            await self._seed_last_seen(tracked)

        all_signals: List[Signal] = []

        for wallet_score in tracked:
            try:
                signals = await self._check_wallet(wallet_score)
                all_signals.extend(signals)
            except Exception as exc:
                logger.error(
                    f"Error checking wallet {wallet_score.wallet_address[:10]}…: {exc}"
                )
            await asyncio.sleep(0.3)

        if all_signals:
            logger.info(f"Generated {len(all_signals)} new signals this cycle")

        return all_signals

    async def _seed_last_seen(self, tracked: List[WalletScore]) -> None:
        """
        Seed _last_seen with the most recent signal time per wallet from DB,
        or 'now' if no prior signals exist, so first cycle only catches genuinely new trades.
        """
        for ws in tracked:
            last_signal_dt = await self._db.get_last_signal_time(ws.wallet_address)
            if last_signal_dt:
                self._last_seen[ws.wallet_address] = last_signal_dt
            else:
                # No prior signals — start from now so we don't backfill history
                self._last_seen[ws.wallet_address] = datetime.utcnow()

    async def run_continuous(self) -> None:
        """Run signal detection in a continuous loop."""
        logger.info(
            f"Starting continuous signal monitoring "
            f"(interval: {settings.scan_interval_minutes} min)"
        )
        while True:
            try:
                await self.run_once()
                await _check_unfollow_conditions(self._db)
            except Exception as exc:
                logger.error(f"Signal cycle error: {exc}")

            wait_seconds = settings.scan_interval_minutes * 60
            logger.info(f"Sleeping {settings.scan_interval_minutes} minutes…")
            await asyncio.sleep(wait_seconds)

    async def _check_wallet(self, wallet_score: WalletScore) -> List[Signal]:
        """
        Check a single tracked wallet for new qualifying trades.
        Returns list of generated signals.
        """
        addr = wallet_score.wallet_address
        since_dt = self._last_seen.get(addr)
        lookback_days = 1  # Only look at last 24h for live monitoring

        # Fetch recent CLOB trades
        raw_trades = await self._clob.get_trades_for_wallet(
            maker_address=addr,
            since_days=lookback_days,
        )
        if not raw_trades:
            return []

        # Normalise
        from src.scanner.wallet_discovery import _normalise_data_api_trades
        new_trades = _normalise_data_api_trades(addr, raw_trades)

        # Filter to unseen trades only
        if since_dt:
            new_trades = [t for t in new_trades if t.matched_at > since_dt]

        if not new_trades:
            return []

        # Update last-seen marker
        latest_dt = max(t.matched_at for t in new_trades)
        self._last_seen[addr] = latest_dt

        # Persist new trades to cache
        await self._db.upsert_wallet_trades(new_trades)

        # Only generate signals for BUY trades in the low-price range
        qualifying = [
            t for t in new_trades
            if t.side.upper() == "BUY" and t.is_low_price
        ]
        if not qualifying:
            return []

        # Compute per-bucket win rates from historical data
        historical = await self._db.get_wallet_trades(addr)
        bucket_stats = compute_price_bucket_stats(historical)

        signals: List[Signal] = []
        for trade in qualifying:
            signal = await self._generate_signal(trade, wallet_score, bucket_stats)
            if signal:
                signal_id = await self._db.insert_signal(signal)
                signal.id = signal_id
                signals.append(signal)
                logger.info(
                    f"SIGNAL {signal.signal_type.value}: "
                    f"wallet={addr[:10]}… market={trade.market_id[:12]}… "
                    f"price={float(trade.price):.3f} "
                    f"size=${float(signal.recommended_size_usd):.2f}"
                )

        return signals

    async def _generate_signal(
        self,
        trade: WalletTrade,
        wallet_score: WalletScore,
        bucket_stats: Dict,
    ) -> Optional[Signal]:
        """
        Generate a BUY signal for a qualifying trade.
        Returns None if expected value is negative.
        """
        bucket = trade.price_bucket
        bucket_data = bucket_stats.get(bucket, {})
        win_rate = bucket_data.get("win_rate", wallet_score.win_rate)

        # Fall back to overall win rate if bucket has too few trades
        if bucket_data.get("resolved", 0) < 5:
            win_rate = wallet_score.win_rate
            if win_rate == 0.0:
                win_rate = 0.30  # conservative prior

        price = float(trade.price)

        # Check EV before generating signal
        from src.analysis.kelly import expected_value
        ev = expected_value(win_rate, price)
        if ev <= 0:
            logger.debug(
                f"Skipping trade at {price:.3f}: EV={ev:.4f} (negative EV)"
            )
            return None

        kelly_result = calculate_kelly(
            win_rate=win_rate,
            entry_price=price,
            bankroll=self._bankroll,
        )

        if kelly_result.recommended_fraction <= 0:
            return None

        return Signal(
            signal_type=SignalType.BUY,
            wallet_address=trade.wallet_address,
            market_id=trade.market_id,
            asset_id=trade.asset_id,
            price=trade.price,
            kelly_fraction=kelly_result.recommended_fraction,
            recommended_size_usd=kelly_result.dollar_amount,
            wallet_win_rate=win_rate,
            wallet_profit_factor=wallet_score.profit_factor,
            price_bucket=bucket,
            notes=(
                f"full_kelly={kelly_result.full_kelly:.4f} "
                f"ev={ev:.4f} "
                f"bucket_resolved={bucket_data.get('resolved', 0)}"
            ),
            generated_at=datetime.utcnow(),
        )

    def update_bankroll(self, new_bankroll: Decimal) -> None:
        """Update the bankroll used for Kelly sizing."""
        self._bankroll = new_bankroll


async def _check_unfollow_conditions(db: Database) -> None:
    """
    Check whether any tracked wallet should be unfollowed due to declining
    performance (last-N-trades profit factor below threshold).
    """
    tracked = await db.get_tracked_wallets()
    threshold = settings.unfollow_profit_factor_threshold
    lookback = settings.unfollow_lookback_trades

    for wallet_score in tracked:
        addr = wallet_score.wallet_address
        recent = await db.get_recent_trades_for_wallet(addr, limit=lookback * 2)

        # Only look at resolved BUY trades
        resolved = [
            t for t in recent
            if t.side.upper() == "BUY" and t.pnl is not None
        ][:lookback]

        if len(resolved) < lookback:
            continue

        gross_profit = sum(t.pnl for t in resolved if t.pnl > Decimal("0"))  # type: ignore
        gross_loss = sum(abs(t.pnl) for t in resolved if t.pnl < Decimal("0"))  # type: ignore

        if gross_loss > Decimal("0"):
            recent_pf = float(gross_profit / gross_loss)
        elif gross_profit > Decimal("0"):
            recent_pf = 10.0
        else:
            recent_pf = 0.0

        if recent_pf < threshold:
            logger.warning(
                f"Unfollowing {addr[:10]}…: "
                f"last-{lookback}-trade profit_factor={recent_pf:.2f} "
                f"< threshold {threshold}"
            )
            await db.mark_wallet_tracked(addr, tracked=False)
