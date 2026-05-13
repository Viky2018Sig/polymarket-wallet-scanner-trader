"""
Gamma API client for Polymarket.
Endpoint: https://gamma-api.polymarket.com
Handles markets, positions, and market resolution data.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import httpx
from loguru import logger
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from config import GAMMA_API_BASE, settings
from src.api.models import GammaMarket, GammaPosition


class GammaClient:
    """Async HTTP client for the Polymarket Gamma API."""

    def __init__(self) -> None:
        self._client: Optional[httpx.AsyncClient] = None
        self._semaphore = asyncio.Semaphore(int(settings.max_requests_per_second))

    async def __aenter__(self) -> "GammaClient":
        self._client = httpx.AsyncClient(
            base_url=GAMMA_API_BASE,
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
                    (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError)
                ),
                reraise=True,
            ):
                with attempt:
                    resp = await self._client.get(path, params=params)
                    if resp.status_code == 429:
                        retry_after = int(resp.headers.get("Retry-After", "5"))
                        logger.warning(f"Gamma API rate limited, sleeping {retry_after}s")
                        await asyncio.sleep(retry_after)
                        raise httpx.HTTPStatusError(
                            "Rate limited", request=resp.request, response=resp
                        )
                    resp.raise_for_status()
                    return resp.json()

    # ── Markets ────────────────────────────────────────────────────────────────

    async def get_markets(
        self,
        active: Optional[bool] = None,
        closed: Optional[bool] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[GammaMarket]:
        """Fetch a page of markets from Gamma."""
        params: Dict[str, Any] = {"limit": limit, "offset": offset}
        if active is not None:
            params["active"] = str(active).lower()
        if closed is not None:
            params["closed"] = str(closed).lower()

        try:
            data = await self._get("/markets", params=params)
            if isinstance(data, list):
                return [GammaMarket.model_validate(m) for m in data]
            if isinstance(data, dict) and "data" in data:
                return [GammaMarket.model_validate(m) for m in data["data"]]
            return []
        except Exception as exc:
            logger.error(f"Failed to fetch Gamma markets (offset={offset}): {exc}")
            return []

    async def get_all_active_markets(self) -> List[GammaMarket]:
        """Paginate through all active markets."""
        all_markets: List[GammaMarket] = []
        offset = 0
        limit = 100

        while True:
            batch = await self.get_markets(active=True, limit=limit, offset=offset)
            if not batch:
                break
            all_markets.extend(batch)
            logger.debug(f"Fetched {len(all_markets)} Gamma markets so far")
            if len(batch) < limit:
                break
            offset += limit
            await asyncio.sleep(1.0 / settings.max_requests_per_second)

        logger.info(f"Total active Gamma markets: {len(all_markets)}")
        return all_markets

    async def get_market(self, market_id: str) -> Optional[GammaMarket]:
        """Fetch a single market by ID."""
        try:
            data = await self._get(f"/markets/{market_id}")
            if isinstance(data, dict):
                return GammaMarket.model_validate(data)
            return None
        except Exception as exc:
            logger.error(f"Failed to fetch market {market_id}: {exc}")
            return None

    async def get_market_by_condition_id(self, condition_id: str) -> Optional[GammaMarket]:
        """Fetch market by condition ID. Tries multiple param name variants."""
        for param_name in ("conditionId", "condition_id", "condition_ids"):
            try:
                data = await self._get("/markets", params={param_name: condition_id})
                raw = data if isinstance(data, list) else data.get("data", [])
                if raw:
                    return GammaMarket.model_validate(raw[0])
            except Exception:
                continue
        return None

    async def get_recently_resolved_markets(self, days: int = 7) -> List[GammaMarket]:
        """Fetch markets resolved within the last `days` days."""
        cutoff = datetime.utcnow() - timedelta(days=days)
        try:
            data = await self._get(
                "/markets",
                params={
                    "closed": "true",
                    "limit": 200,
                    "order": "end_date_iso",
                    "ascending": "false",
                },
            )
            markets: List[GammaMarket] = []
            raw_list = data if isinstance(data, list) else data.get("data", [])
            for m in raw_list:
                market = GammaMarket.model_validate(m)
                if market.end_date_iso:
                    try:
                        end_dt = datetime.fromisoformat(
                            market.end_date_iso.replace("Z", "+00:00")
                        ).replace(tzinfo=None)
                        if end_dt >= cutoff:
                            markets.append(market)
                    except ValueError:
                        pass
            return markets
        except Exception as exc:
            logger.error(f"Failed to fetch recently resolved markets: {exc}")
            return []

    # ── Positions ──────────────────────────────────────────────────────────────

    async def get_positions(
        self,
        user: Optional[str] = None,
        market: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[GammaPosition]:
        """Fetch positions filtered by user or market."""
        params: Dict[str, Any] = {"limit": limit, "offset": offset}
        if user:
            params["user"] = user
        if market:
            params["market"] = market

        try:
            data = await self._get("/positions", params=params)
            raw_list = data if isinstance(data, list) else data.get("data", [])
            return [GammaPosition.model_validate(p) for p in raw_list]
        except Exception as exc:
            logger.error(f"Failed to fetch Gamma positions: {exc}")
            return []

    async def get_all_positions_for_user(self, user: str) -> List[GammaPosition]:
        """Paginate all positions for a given user address."""
        all_positions: List[GammaPosition] = []
        offset = 0
        limit = 100

        while True:
            batch = await self.get_positions(user=user, limit=limit, offset=offset)
            if not batch:
                break
            all_positions.extend(batch)
            if len(batch) < limit:
                break
            offset += limit
            await asyncio.sleep(1.0 / settings.max_requests_per_second)

        return all_positions

    async def get_events(
        self,
        active: Optional[bool] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Fetch Gamma events (collections of related markets)."""
        params: Dict[str, Any] = {"limit": limit, "offset": offset}
        if active is not None:
            params["active"] = str(active).lower()

        try:
            data = await self._get("/events", params=params)
            if isinstance(data, list):
                return data
            return data.get("data", [])
        except Exception as exc:
            logger.error(f"Failed to fetch Gamma events: {exc}")
            return []
