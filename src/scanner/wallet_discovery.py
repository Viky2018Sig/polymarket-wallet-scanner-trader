"""
Wallet discovery module.

Uses data-api.polymarket.com (public, no auth) to:
1. Stream global trades and extract unique proxyWallet addresses.
2. Fetch per-wallet trade history via /activity?user=<address>.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional, Set

from loguru import logger

from config import settings
from src.api.data_client import DataApiClient
from src.api.models import DataApiTrade, WalletTrade
from src.storage.database import Database


class WalletDiscovery:
    """
    Discovers active Polymarket wallets via the public Data API.

    Usage:
        async with DataApiClient() as data, Database() as db:
            discovery = WalletDiscovery(data, db)
            addresses = await discovery.discover(seed_addresses=[...])
    """

    def __init__(self, data_client: DataApiClient, db: Database) -> None:
        self._data = data_client
        self._db = db

    async def discover(
        self,
        seed_addresses: Optional[List[str]] = None,
        since_days: Optional[int] = None,
        max_pages: int = 200,
    ) -> List[str]:
        """
        Main entry point for wallet discovery.

        1. Pull global Data API trade stream for the lookback window.
        2. Extract unique proxyWallet values.
        3. Merge with seed list (if any).
        4. Merge with addresses already in DB from previous scans.
        5. Return deduplicated, lowercase-sorted list.
        """
        lookback = since_days or settings.lookback_days
        logger.info(
            f"Starting wallet discovery: last {lookback} days, "
            f"max_pages={max_pages}"
        )

        discovered: Set[str] = await self._data.discover_wallet_addresses(
            since_days=lookback,
            max_pages=max_pages,
        )

        if seed_addresses:
            normalised_seeds = {addr.lower() for addr in seed_addresses if addr}
            before = len(discovered)
            discovered |= normalised_seeds
            logger.info(
                f"Seed list added {len(discovered) - before} new addresses "
                f"({len(normalised_seeds)} seeds provided)"
            )

        existing = await self._db.get_all_wallet_addresses()
        discovered |= {addr.lower() for addr in existing}

        result = sorted(discovered)
        logger.info(f"Total unique wallets for scoring: {len(result)}")
        return result

    async def fetch_and_cache_wallet_trades(
        self,
        wallet_address: str,
        since_days: Optional[int] = None,
    ) -> List[WalletTrade]:
        """
        Fetch trades for one wallet via Data API /activity, normalise them,
        and upsert into the DB cache.
        """
        lookback = since_days or settings.lookback_days
        raw_trades = await self._data.get_trades_for_wallet(
            maker_address=wallet_address,
            since_days=lookback,
        )

        if not raw_trades:
            logger.debug(f"No trades found for {wallet_address[:10]}…")
            return []

        wallet_trades = _normalise_data_api_trades(wallet_address, raw_trades)

        saved = await self._db.upsert_wallet_trades(wallet_trades)
        logger.debug(
            f"{wallet_address[:10]}…: {len(raw_trades)} raw → "
            f"{len(wallet_trades)} normalised, {saved} upserted"
        )
        return wallet_trades

    async def refresh_all_tracked_wallets(self) -> Dict[str, int]:
        """Refresh trade cache for all wallets in DB. Returns address→count map."""
        addresses = await self._db.get_all_wallet_addresses()
        results: Dict[str, int] = {}

        for addr in addresses:
            try:
                trades = await self.fetch_and_cache_wallet_trades(addr)
                results[addr] = len(trades)
            except Exception as exc:
                logger.error(f"Failed to refresh trades for {addr[:10]}…: {exc}")
                results[addr] = 0
            await asyncio.sleep(0.5)

        return results


def _normalise_data_api_trades(
    wallet_address: str,
    trades: List[DataApiTrade],
) -> List[WalletTrade]:
    """
    Convert DataApiTrade objects (from data-api.polymarket.com) into
    normalised WalletTrade objects for DB storage and scoring.
    """
    result: List[WalletTrade] = []

    for t in trades:
        if not t.condition_id and not t.asset:
            continue
        if t.price is None:
            continue

        matched_at = t.matched_at
        if matched_at is None:
            continue

        try:
            price = Decimal(str(t.price)).quantize(Decimal("0.0001"))
            size = Decimal(str(t.size or 0)).quantize(Decimal("0.0001"))
            notional = price * size
        except InvalidOperation:
            continue

        # Unique ID: tx hash preferred, fallback to wallet+ts+asset
        trade_id = (
            t.transaction_hash
            or f"{wallet_address}_{t.timestamp_unix}_{t.asset or t.condition_id}"
        )

        side = "BUY"
        if t.side and t.side.lower() in ("sell", "ask"):
            side = "SELL"

        result.append(
            WalletTrade(
                trade_id=trade_id,
                wallet_address=wallet_address.lower(),
                market_id=t.condition_id or t.asset or "",
                asset_id=t.asset,
                side=side,
                price=price,
                size=size,
                notional_usd=notional,
                matched_at=matched_at,
                outcome=t.outcome,
            )
        )

    return result


# Keep the old name as an alias used by signals.py
_normalise_clob_trades = _normalise_data_api_trades
