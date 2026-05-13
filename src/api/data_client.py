"""
Polymarket Data API client.
Endpoint: https://data-api.polymarket.com

This is the correct public API for:
- Global trade stream discovery (GET /trades)
- Per-wallet activity (GET /activity?user=<address>)

No authentication required. Used by the working copybot-live project.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set

import httpx
from loguru import logger
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from config import DATA_API_BASE, settings
from src.api.models import DataApiTrade


class DataApiClient:
    """Async HTTP client for the Polymarket Data API (public, no auth)."""

    def __init__(self) -> None:
        self._client: Optional[httpx.AsyncClient] = None
        self._semaphore = asyncio.Semaphore(max(1, int(settings.max_requests_per_second)))

    async def __aenter__(self) -> "DataApiClient":
        self._client = httpx.AsyncClient(
            base_url=DATA_API_BASE,
            timeout=settings.http_timeout_seconds,
            headers={
                "Accept": "application/json",
                "User-Agent": "polymarket-wallet-scanner/1.0",
            },
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """Rate-limited GET with exponential backoff retry."""
        assert self._client is not None, "Client not initialised — use async context manager"

        async with self._semaphore:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(settings.max_retries),
                wait=wait_exponential(multiplier=1, min=1, max=30),
                retry=retry_if_exception_type(
                    (httpx.TimeoutException, httpx.NetworkError)
                ),
                reraise=True,
            ):
                with attempt:
                    resp = await self._client.get(path, params=params)
                    if resp.status_code == 429:
                        retry_after = int(resp.headers.get("Retry-After", "5"))
                        logger.warning(f"Data API rate limited, sleeping {retry_after}s")
                        await asyncio.sleep(retry_after)
                        resp.raise_for_status()
                    if resp.status_code == 404:
                        return []
                    resp.raise_for_status()
                    return resp.json()

    # ── Global trade stream (wallet discovery) ─────────────────────────────────

    async def get_recent_trades(
        self,
        limit: int = 100,
        before: Optional[int] = None,
    ) -> List[DataApiTrade]:
        """
        Fetch recent global trades from the Data API.
        Returns trades with proxyWallet field for address discovery.

        Args:
            limit: Max results per page (max 100).
            before: Unix timestamp — return trades before this time.
        """
        params: Dict[str, Any] = {"limit": limit}
        if before:
            params["before"] = before

        try:
            data = await self._get("/trades", params=params)
            raw_list = data if isinstance(data, list) else data.get("data", [])
            return [DataApiTrade.model_validate(t) for t in raw_list if t]
        except Exception as exc:
            logger.error(f"Data API /trades error: {exc}")
            return []

    async def get_wallet_activity(
        self,
        user: str,
        limit: int = 500,
        offset: int = 0,
    ) -> List[DataApiTrade]:
        """
        Fetch trade activity for a specific wallet address.

        Args:
            user: Wallet address (proxyWallet / EOA).
            limit: Page size.
            offset: Pagination offset.
        """
        params: Dict[str, Any] = {"user": user, "limit": limit, "offset": offset}

        try:
            data = await self._get("/activity", params=params)
            raw_list = data if isinstance(data, list) else data.get("data", [])
            return [DataApiTrade.model_validate(t) for t in raw_list if t]
        except Exception as exc:
            logger.error(f"Data API /activity error for {user[:10]}…: {exc}")
            return []

    # ── Composed helpers ───────────────────────────────────────────────────────

    async def get_all_wallet_activity(
        self,
        user: str,
        since_days: int = 90,
    ) -> List[DataApiTrade]:
        """
        Paginate all activity for a wallet over the last `since_days` days.
        """
        all_trades: List[DataApiTrade] = []
        cutoff = datetime.utcnow() - timedelta(days=since_days)
        offset = 0
        limit = 500
        page = 0

        while True:
            batch = await self.get_wallet_activity(user=user, limit=limit, offset=offset)
            if not batch:
                break

            stopped = False
            for t in batch:
                dt = t.matched_at
                if dt and dt < cutoff:
                    stopped = True
                    break
                all_trades.append(t)

            page += 1
            logger.debug(
                f"Wallet {user[:8]}… activity page {page}: "
                f"{len(batch)} fetched, total {len(all_trades)}"
            )

            if stopped or len(batch) < limit:
                break

            offset += limit
            await asyncio.sleep(0.3)

        return all_trades

    async def discover_wallet_addresses(
        self,
        since_days: int = 90,
        max_pages: int = 200,
    ) -> Set[str]:
        """
        Stream global trades to collect unique proxyWallet addresses.
        Uses timestamp-based cursor pagination via the `before` param.
        """
        addresses: Set[str] = set()
        cutoff_ts = int((datetime.utcnow() - timedelta(days=since_days)).timestamp())
        before: Optional[int] = None
        page = 0

        while page < max_pages:
            batch = await self.get_recent_trades(limit=100, before=before)
            if not batch:
                break

            stopped = False
            oldest_ts: Optional[int] = None

            for t in batch:
                ts = t.timestamp_unix
                if ts and ts < cutoff_ts:
                    stopped = True
                    break
                if t.proxy_wallet:
                    addresses.add(t.proxy_wallet.lower())
                if ts:
                    oldest_ts = ts

            page += 1
            if page % 20 == 0:
                logger.info(
                    f"Discovery page {page}: {len(addresses)} unique wallets so far"
                )

            if stopped or oldest_ts is None or len(batch) < 100:
                break

            # Paginate backwards in time
            before = oldest_ts - 1
            await asyncio.sleep(0.5)

        logger.info(
            f"Data API discovery: {len(addresses)} unique wallet addresses "
            f"({page} pages scanned)"
        )
        return addresses

    async def get_trades_for_wallet(
        self,
        maker_address: str,
        since_days: int = 90,
    ) -> List[DataApiTrade]:
        """Convenience alias used by wallet_discovery and signals."""
        return await self.get_all_wallet_activity(
            user=maker_address,
            since_days=since_days,
        )
