"""
PnL calculator — FIFO matching of SELL trades against BUY trades.

No external API calls needed. For each (wallet, market_id, outcome)
group we sort trades chronologically and match SELLs against the
earliest BUYs first. The SELL price becomes the resolved_price on
the matched BUY trade.

Why FIFO from sells:
  - BUY at 0.08, SELL at 0.99 → resolved_price=0.99, pnl = +0.91 × shares  (WIN)
  - BUY at 0.12, SELL at 0.02 → resolved_price=0.02, pnl = -0.10 × shares  (LOSS)
  - Unmatched BUYs remain open (resolved_price stays NULL)
"""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, List, NamedTuple, Optional, Tuple

from loguru import logger

from src.storage.database import Database


class _TradeRow(NamedTuple):
    trade_id: str
    wallet_address: str
    market_id: str
    outcome: str
    side: str
    price: float
    size: float
    matched_at: str   # ISO string for sorting


class PnlCalculator:
    """
    Calculates realised PnL by FIFO-matching SELL trades against BUY trades
    for every (wallet, market_id, outcome) combo in the database.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    async def run(self) -> int:
        """
        Main entry point. Returns number of BUY trade rows updated with PnL.
        """
        logger.info("Loading all trades for PnL calculation…")
        rows = await self._load_all_trades()
        logger.info(f"Loaded {len(rows):,} trade rows")

        # Group by (wallet, market_id, outcome)
        groups: Dict[Tuple[str, str, str], List[_TradeRow]] = defaultdict(list)
        for row in rows:
            key = (row.wallet_address, row.market_id, row.outcome.lower())
            groups[key].append(row)

        logger.info(f"Processing {len(groups):,} wallet×market×outcome groups…")

        updates: List[Tuple[str, str, str]] = []  # (trade_id, resolved_price, pnl)

        for (wallet, market_id, outcome), trades in groups.items():
            buys = sorted(
                [t for t in trades if t.side == "BUY"],
                key=lambda t: t.matched_at,
            )
            sells = sorted(
                [t for t in trades if t.side == "SELL"],
                key=lambda t: t.matched_at,
            )

            if not buys or not sells:
                continue

            group_updates = _fifo_match(buys, sells)
            updates.extend(group_updates)

        logger.info(f"FIFO matching produced {len(updates):,} BUY trade resolutions")

        if updates:
            await self._bulk_update(updates)

        return len(updates)

    async def _load_all_trades(self) -> List[_TradeRow]:
        """Load all trades needed for PnL matching."""
        assert self._db._conn is not None
        # Load unresolved BUYs and all SELLs
        async with self._db._conn.execute(
            """
            SELECT trade_id, wallet_address, market_id,
                   COALESCE(outcome, '') as outcome,
                   side,
                   CAST(price AS REAL) as price,
                   CAST(size  AS REAL) as size,
                   matched_at
            FROM wallet_trades
            WHERE side = 'SELL'
               OR (side = 'BUY' AND resolved_price IS NULL)
            ORDER BY wallet_address, market_id, outcome, matched_at
            """
        ) as cur:
            rows = await cur.fetchall()
        return [_TradeRow(*r) for r in rows]

    async def _bulk_update(self, updates: List[Tuple[str, str, str]]) -> None:
        """Batch-write resolved_price and pnl to wallet_trades."""
        assert self._db._conn is not None
        BATCH = 500
        total = len(updates)
        for i in range(0, total, BATCH):
            chunk = updates[i : i + BATCH]
            await self._db._conn.executemany(
                "UPDATE wallet_trades SET resolved_price = ?, pnl = ? WHERE trade_id = ?",
                [(rp, pnl, tid) for tid, rp, pnl in chunk],
            )
            await self._db._conn.commit()
            if (i + BATCH) % 5000 == 0 or i + BATCH >= total:
                logger.info(f"  Updated {min(i + BATCH, total):,}/{total:,} trade rows")


def _fifo_match(
    buys: List[_TradeRow],
    sells: List[_TradeRow],
) -> List[Tuple[str, str, str]]:
    """
    FIFO-match a list of SELL trades against BUY trades.
    Returns list of (trade_id, resolved_price_str, pnl_str) tuples for BUYs.
    """
    # Each buy becomes (trade_id, price, remaining_size)
    buy_queue: deque = deque(
        [{"id": b.trade_id, "price": b.price, "remaining": b.size} for b in buys]
    )
    results: List[Tuple[str, str, str]] = []

    for sell in sells:
        sell_remaining = sell.size
        sell_price = sell.price

        while sell_remaining > 0.0001 and buy_queue:
            buy = buy_queue[0]
            matched = min(buy["remaining"], sell_remaining)

            # Accumulate partial sells against this buy
            if "sell_proceeds" not in buy:
                buy["sell_proceeds"] = 0.0
                buy["sold_size"] = 0.0

            buy["sell_proceeds"] += sell_price * matched
            buy["sold_size"] += matched
            buy["remaining"] -= matched
            sell_remaining -= matched

            if buy["remaining"] <= 0.0001:
                # BUY fully matched — compute weighted average sell price
                avg_sell_price = buy["sell_proceeds"] / buy["sold_size"]
                pnl = (avg_sell_price - buy["price"]) * buy["sold_size"]
                results.append((
                    buy["id"],
                    f"{avg_sell_price:.6f}",
                    f"{pnl:.6f}",
                ))
                buy_queue.popleft()

    # Any buy that was partially matched (sold_size > 0 but remaining > 0)
    # gets a partial resolution at the weighted avg sell price
    for buy in buy_queue:
        if buy.get("sold_size", 0) > 0.0001:
            sold = buy["sold_size"]
            avg_sell_price = buy["sell_proceeds"] / sold
            pnl = (avg_sell_price - buy["price"]) * sold
            results.append((
                buy["id"],
                f"{avg_sell_price:.6f}",
                f"{pnl:.6f}",
            ))

    return results
