"""
Market resolution enrichment.

For every market_id in wallet_trades that has no resolved_price yet,
fetches the Gamma API to check if the market is closed and which
outcome won. Then writes resolved_price (1.0 = token paid out, 0.0 =
expired worthless) and PnL back to wallet_trades.

Binary Polymarket logic:
  - YES token: pays $1.00 if YES wins, $0.00 if NO wins
  - NO token:  pays $1.00 if NO wins,  $0.00 if YES wins

The `outcome` column in wallet_trades stores "Yes" / "No" from the
Data API, telling us which token the wallet held.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from typing import Optional, Tuple

from loguru import logger

from src.api.gamma_client import GammaClient
from src.storage.database import Database


class MarketResolver:
    """
    Resolves closed Polymarket markets and enriches wallet_trades with
    resolved_price and PnL values needed for wallet scoring.
    """

    def __init__(self, gamma_client: GammaClient, db: Database) -> None:
        self._gamma = gamma_client
        self._db = db

    async def resolve_all(self) -> int:
        """
        Main entry point. Resolves all markets that have unresolved BUY trades.
        Returns total number of trade rows updated.
        """
        market_ids = await self._db.get_unresolved_market_ids()
        if not market_ids:
            logger.info("No markets with unresolved trades found")
            return 0

        logger.info(f"Resolving {len(market_ids)} markets with unresolved trades…")
        total_updated = 0
        resolved_count = 0
        skipped_count = 0

        for i, market_id in enumerate(market_ids, 1):
            try:
                updated = await self._resolve_market(market_id)
                if updated > 0:
                    total_updated += updated
                    resolved_count += 1
                else:
                    skipped_count += 1
            except Exception as exc:
                logger.error(f"Error resolving market {market_id[:14]}…: {exc}")
                skipped_count += 1

            if i % 50 == 0:
                logger.info(
                    f"Resolution progress: {i}/{len(market_ids)} markets checked, "
                    f"{resolved_count} resolved, {total_updated} trades updated"
                )

            # Throttle to avoid hammering Gamma API
            await asyncio.sleep(0.2)

        logger.info(
            f"Resolution complete: {resolved_count} markets resolved, "
            f"{skipped_count} skipped (still open or unresolvable), "
            f"{total_updated} trade rows updated"
        )
        return total_updated

    async def _resolve_market(self, market_id: str) -> int:
        """
        Resolve a single market. Returns number of trade rows updated (0 if
        market is still open or outcome cannot be determined).
        """
        # Hex condition IDs (0x...) must use the condition_id query param.
        # The /markets/{id} endpoint expects an integer ID, not a hex string.
        if market_id.startswith("0x"):
            market = await self._gamma.get_market_by_condition_id(market_id)
        else:
            market = await self._gamma.get_market(market_id)
            if market is None:
                market = await self._gamma.get_market_by_condition_id(market_id)

        if market is None:
            return 0

        # Only resolve closed/archived markets
        if not (market.closed or market.archived):
            return 0

        yes_wins = _determine_yes_wins(market)
        if yes_wins is None:
            # Outcome unclear — skip
            return 0

        # Yes token: resolves to 1.0 if yes_wins, else 0.0
        # No  token: resolves to 1.0 if not yes_wins, else 0.0
        yes_resolved = Decimal("1.0") if yes_wins else Decimal("0.0")
        no_resolved = Decimal("0.0") if yes_wins else Decimal("1.0")

        return await self._db.update_trade_resolution_by_outcome(
            market_id=market_id,
            yes_resolved_price=yes_resolved,
            no_resolved_price=no_resolved,
        )


def _determine_yes_wins(market) -> Optional[bool]:
    """
    Inspect a closed GammaMarket and return True if the YES outcome won,
    False if NO won, or None if we cannot determine.

    Checks outcome_prices first (most reliable), falls back to last_trade_price.
    """
    # outcome_prices is a JSON string like "[1, 0]" or "[0, 1]"
    if market.outcome_prices:
        try:
            prices = json.loads(market.outcome_prices)
            if isinstance(prices, list) and len(prices) >= 2:
                yes_price = float(prices[0])
                if yes_price >= 0.95:
                    return True
                if yes_price <= 0.05:
                    return False
        except (json.JSONDecodeError, ValueError, IndexError):
            pass

    # Fall back to last_trade_price of the YES token
    if market.last_trade_price is not None:
        ltp = float(market.last_trade_price)
        if ltp >= 0.95:
            return True
        if ltp <= 0.05:
            return False

    return None
