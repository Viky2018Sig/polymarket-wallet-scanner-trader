"""
Wallet performance scoring.

Filters wallets by qualification criteria and computes composite scores.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from loguru import logger

from config import settings
from src.api.models import WalletScore, WalletTrade
from src.analysis.metrics import compute_wallet_metrics
from src.storage.database import Database


class PerformanceScorer:
    """
    Scores wallets based on their historical trade performance.

    Qualification criteria:
    1. profit_factor > MIN_PROFIT_FACTOR (default 2.0)
    2. low_price_pct >= LOW_PRICE_MIN_PCT (default 0.50)
    3. resolved_trades >= MIN_TRADES_REQUIRED (default 20)
    4. max_drawdown < MAX_DRAWDOWN_THRESHOLD (default 0.40)
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    async def score_wallet(
        self,
        wallet_address: str,
        trades: Optional[List[WalletTrade]] = None,
    ) -> WalletScore:
        """
        Compute performance metrics for a single wallet.

        If `trades` is None, loads them from the DB cache.
        """
        if trades is None:
            trades = await self._db.get_wallet_trades(wallet_address)

        if not trades:
            return WalletScore(
                wallet_address=wallet_address,
                qualifies=False,
                disqualify_reason="no_trades",
            )

        score = compute_wallet_metrics(wallet_address, trades)
        score = _apply_qualification_rules(score)
        return score

    async def score_all_wallets(
        self,
        addresses: List[str],
        concurrency: int = 5,
    ) -> List[WalletScore]:
        """
        Score all wallets with bounded concurrency.
        Returns all scores (qualified and disqualified).
        """
        semaphore = asyncio.Semaphore(concurrency)
        results: List[WalletScore] = []
        total = len(addresses)

        async def _score_one(addr: str, idx: int) -> WalletScore:
            async with semaphore:
                try:
                    score = await self.score_wallet(addr)
                    if idx % 50 == 0 or score.qualifies:
                        status = "QUALIFIES" if score.qualifies else f"skip({score.disqualify_reason})"
                        logger.debug(
                            f"[{idx}/{total}] {addr[:10]}… → {status} "
                            f"pf={score.profit_factor:.2f} "
                            f"low_pct={score.low_price_pct:.1%}"
                        )
                    return score
                except Exception as exc:
                    logger.error(f"Error scoring {addr[:10]}…: {exc}")
                    return WalletScore(
                        wallet_address=addr,
                        qualifies=False,
                        disqualify_reason=f"error: {exc}",
                    )

        tasks = [_score_one(addr, i) for i, addr in enumerate(addresses, 1)]
        results = await asyncio.gather(*tasks)
        return list(results)

    async def get_qualified_wallets(
        self,
        addresses: Optional[List[str]] = None,
    ) -> List[WalletScore]:
        """
        Return only wallets that pass all qualification criteria,
        sorted by composite_score descending.
        """
        if addresses is None:
            addresses = await self._db.get_all_wallet_addresses()

        all_scores = await self.score_all_wallets(addresses)
        qualified = [s for s in all_scores if s.qualifies]
        qualified.sort(key=lambda s: s.composite_score, reverse=True)

        logger.info(
            f"Qualification results: {len(qualified)}/{len(all_scores)} wallets qualify"
        )
        return qualified

    async def save_scores(self, scores: List[WalletScore]) -> int:
        """Persist scores to the wallets table."""
        return await self._db.upsert_wallet_scores(scores)

    async def get_top_wallets(
        self,
        n: int = 20,
        min_composite_score: float = 0.0,
    ) -> List[WalletScore]:
        """
        Load top-N qualified wallets from DB by composite score.
        """
        return await self._db.get_top_wallets(n=n, min_score=min_composite_score)


def _apply_qualification_rules(score: WalletScore) -> WalletScore:
    """
    Apply hard disqualification rules to a computed score.
    Sets score.qualifies = False and populates disqualify_reason if any rule fails.
    """
    # Rule 1: minimum resolved trades
    if score.resolved_trades < settings.min_trades_required:
        score.qualifies = False
        score.disqualify_reason = (
            f"insufficient_trades({score.resolved_trades}<{settings.min_trades_required})"
        )
        return score

    # Rule 2: profit factor
    if score.profit_factor < settings.min_profit_factor:
        score.qualifies = False
        score.disqualify_reason = (
            f"low_profit_factor({score.profit_factor:.2f}<{settings.min_profit_factor})"
        )
        return score

    # Rule 3: low-price percentage
    if score.low_price_pct < settings.low_price_min_pct:
        score.qualifies = False
        score.disqualify_reason = (
            f"low_price_pct_insufficient({score.low_price_pct:.1%}<{settings.low_price_min_pct:.1%})"
        )
        return score

    # Rule 4: max drawdown
    if score.max_drawdown > settings.max_drawdown_threshold:
        score.qualifies = False
        score.disqualify_reason = (
            f"excessive_drawdown({score.max_drawdown:.1%}>{settings.max_drawdown_threshold:.1%})"
        )
        return score

    score.qualifies = True
    score.disqualify_reason = None
    return score
