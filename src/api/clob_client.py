"""
CLOB API client for Polymarket.
Endpoint: https://clob.polymarket.com
Handles trade history, order books, and maker address lookups.
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

from config import CLOB_API_BASE, settings
from src.api.models import ClobOrderBook, ClobTrade


class ClobClient:
    """Async HTTP client for the Polymarket CLOB API."""

    def __init__(self) -> None:
        self._client: Optional[httpx.AsyncClient] = None
        self._semaphore = asyncio.Semaphore(max(1, int(settings.max_requests_per_second)))

    async def __aenter__(self) -> "ClobClient":
        headers: dict = {
            "Accept": "application/json",
            "User-Agent": "polymarket-wallet-scanner/1.0",
        }
        if settings.polymarket_api_key:
            headers["Authorization"] = f"Bearer {settings.polymarket_api_key}"
        self._client = httpx.AsyncClient(
            base_url=CLOB_API_BASE,
            timeout=settings.http_timeout_seconds,
            headers=headers,
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
                    (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError)
                ),
                reraise=True,
            ):
                with attempt:
                    resp = await self._client.get(path, params=params)
                    if resp.status_code == 429:
                        retry_after = int(resp.headers.get("Retry-After", "5"))
                        logger.warning(f"CLOB API rate limited, sleeping {retry_after}s")
                        await asyncio.sleep(retry_after)
                        raise httpx.HTTPStatusError(
                            "Rate limited", request=resp.request, response=resp
                        )
                    resp.raise_for_status()
                    return resp.json()

    # ── Trades ─────────────────────────────────────────────────────────────────

    async def get_trades(
        self,
        maker_address: Optional[str] = None,
        market: Optional[str] = None,
        asset_id: Optional[str] = None,
        before: Optional[int] = None,
        after: Optional[int] = None,
        limit: int = 500,
        offset: int = 0,
    ) -> List[ClobTrade]:
        """
        Fetch trades from CLOB API.

        Args:
            maker_address: Filter by maker (wallet) address.
            market: Filter by market condition ID.
            asset_id: Filter by token asset ID.
            before: Unix timestamp upper bound.
            after: Unix timestamp lower bound.
            limit: Page size.
            offset: Pagination offset (some endpoints use cursor/next_cursor).
        """
        params: Dict[str, Any] = {"limit": limit}
        if maker_address:
            params["maker_address"] = maker_address
        if market:
            params["market"] = market
        if asset_id:
            params["asset_id"] = asset_id
        if before:
            params["before"] = before
        if after:
            params["after"] = after
        if offset:
            params["offset"] = offset

        try:
            data = await self._get("/trades", params=params)
            if isinstance(data, list):
                return [ClobTrade.model_validate(t) for t in data]
            if isinstance(data, dict):
                raw = data.get("data", data.get("trades", []))
                return [ClobTrade.model_validate(t) for t in raw]
            return []
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return []
            logger.error(f"CLOB trades HTTP error: {exc}")
            return []
        except Exception as exc:
            logger.error(f"Failed to fetch CLOB trades: {exc}")
            return []

    async def get_trades_for_wallet(
        self,
        maker_address: str,
        since_days: int = 90,
    ) -> List[ClobTrade]:
        """
        Fetch all trades for a wallet over the last `since_days` days,
        handling pagination automatically.
        """
        all_trades: List[ClobTrade] = []
        cutoff_ts = int(
            (datetime.utcnow() - timedelta(days=since_days)).timestamp()
        )
        offset = 0
        limit = 500
        page = 0

        while True:
            batch = await self.get_trades(
                maker_address=maker_address,
                after=cutoff_ts,
                limit=limit,
                offset=offset,
            )
            if not batch:
                break

            all_trades.extend(batch)
            page += 1
            logger.debug(
                f"Wallet {maker_address[:8]}…: page {page}, "
                f"{len(all_trades)} trades fetched"
            )

            if len(batch) < limit:
                break

            offset += limit
            await asyncio.sleep(0.2)

        return all_trades

    async def get_all_trades_paginated(
        self,
        since_days: int = 90,
        max_pages: int = 200,
    ) -> List[ClobTrade]:
        """
        Fetch global trade stream for wallet discovery.
        Iterates through offset pages until cutoff or max_pages reached.
        """
        all_trades: List[ClobTrade] = []
        cutoff_ts = int(
            (datetime.utcnow() - timedelta(days=since_days)).timestamp()
        )
        offset = 0
        limit = 500
        page = 0
        stop = False

        while page < max_pages and not stop:
            batch = await self.get_trades(
                after=cutoff_ts,
                limit=limit,
                offset=offset,
            )
            if not batch:
                break

            for trade in batch:
                dt = trade.match_datetime
                if dt and dt.timestamp() < cutoff_ts:
                    stop = True
                    break
                all_trades.append(trade)

            page += 1
            logger.debug(
                f"Global trades page {page}: {len(batch)} fetched, "
                f"total {len(all_trades)}"
            )

            if len(batch) < limit:
                break

            offset += limit
            await asyncio.sleep(0.5)

        logger.info(f"Global trade discovery: {len(all_trades)} total trades fetched")
        return all_trades

    # ── Maker address discovery ────────────────────────────────────────────────

    async def discover_wallet_addresses(
        self,
        since_days: int = 90,
        max_pages: int = 200,
    ) -> Set[str]:
        """
        Extract unique maker_address values from the global trade stream.
        Returns a set of wallet addresses active in the last `since_days` days.
        """
        trades = await self.get_all_trades_paginated(
            since_days=since_days,
            max_pages=max_pages,
        )
        addresses: Set[str] = set()
        for trade in trades:
            if trade.maker_address:
                addresses.add(trade.maker_address.lower())

        logger.info(f"Discovered {len(addresses)} unique wallet addresses")
        return addresses

    # ── Order book ─────────────────────────────────────────────────────────────

    async def get_order_book(self, token_id: str) -> Optional[ClobOrderBook]:
        """Fetch current order book for a token (YES/NO share)."""
        try:
            data = await self._get("/book", params={"token_id": token_id})
            if isinstance(data, dict):
                return ClobOrderBook.model_validate(data)
            return None
        except Exception as exc:
            logger.error(f"Failed to fetch order book for {token_id}: {exc}")
            return None

    async def get_last_trade_price(self, token_id: str) -> Optional[float]:
        """Get the most recent trade price for a token."""
        try:
            data = await self._get("/last-trade-price", params={"token_id": token_id})
            if isinstance(data, dict):
                return float(data.get("price", 0) or 0)
            return None
        except Exception as exc:
            logger.error(f"Failed to fetch last trade price for {token_id}: {exc}")
            return None

    async def get_best_ask(self, token_id: str) -> Optional[float]:
        """
        Return the current best ask for a token, falling back to last trade price.
        Used by the realtime monitor's price-ceiling guardrail.
        """
        book = await self.get_order_book(token_id)
        if book is not None and book.best_ask is not None:
            return book.best_ask
        return await self.get_last_trade_price(token_id)

    async def get_midpoint_price(self, token_id: str) -> Optional[float]:
        """Get the mid-point of bid/ask for a token."""
        book = await self.get_order_book(token_id)
        if book is None:
            return None
        bid = book.best_bid
        ask = book.best_ask
        if bid is not None and ask is not None:
            return (bid + ask) / 2.0
        return bid or ask

    # ── Markets / tokens ───────────────────────────────────────────────────────

    async def get_markets(
        self,
        next_cursor: Optional[str] = None,
        limit: int = 100,
    ) -> Dict[str, Any]:
        """
        Fetch CLOB market listing.
        Returns dict with 'data' (list of markets) and 'next_cursor'.
        """
        params: Dict[str, Any] = {"limit": limit}
        if next_cursor:
            params["next_cursor"] = next_cursor

        try:
            data = await self._get("/markets", params=params)
            if isinstance(data, dict):
                return data
            return {"data": data, "next_cursor": None}
        except Exception as exc:
            logger.error(f"Failed to fetch CLOB markets: {exc}")
            return {"data": [], "next_cursor": None}

    async def get_market(self, condition_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a single market by condition ID from CLOB."""
        try:
            data = await self._get(f"/markets/{condition_id}")
            return data if isinstance(data, dict) else None
        except Exception as exc:
            logger.error(f"Failed to fetch CLOB market {condition_id}: {exc}")
            return None

    async def get_sampling_simplified_markets(
        self, next_cursor: Optional[str] = None
    ) -> Dict[str, Any]:
        """Fetch simplified market data optimised for scanning."""
        params: Dict[str, Any] = {}
        if next_cursor:
            params["next_cursor"] = next_cursor

        try:
            data = await self._get("/sampling-simplified-markets", params=params)
            if isinstance(data, dict):
                return data
            return {"data": data, "next_cursor": None}
        except Exception as exc:
            logger.error(f"Failed to fetch sampling markets: {exc}")
            return {"data": [], "next_cursor": None}
