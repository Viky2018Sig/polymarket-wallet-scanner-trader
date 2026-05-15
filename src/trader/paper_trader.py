"""
Paper trading engine.

Processes signals, opens paper positions, tracks PnL, and handles market
resolution to close positions. Also takes daily portfolio snapshots.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, List, Optional, Tuple

from loguru import logger

from config import settings
from src.api.gamma_client import GammaClient
from src.api.models import (
    PaperTrade,
    PaperTradeStatus,
    PortfolioSnapshot,
    Signal,
    WalletScore,
)
from src.storage.database import Database


class PaperTrader:
    """
    Paper trading engine that:
    1. Converts signals into paper positions (open trades).
    2. Resolves open positions when markets close.
    3. Updates running bankroll.
    4. Takes daily snapshots for equity curve.
    5. Generates performance reports.
    """

    def __init__(
        self,
        db: Database,
        gamma_client: GammaClient,
        starting_bankroll: Optional[Decimal] = None,
    ) -> None:
        self._db = db
        self._gamma = gamma_client
        self._bankroll = starting_bankroll or Decimal(str(settings.starting_bankroll))
        self._peak_bankroll = self._bankroll
        self._max_drawdown: float = 0.0

    # ── Public interface ───────────────────────────────────────────────────────

    async def initialise(self) -> None:
        """Load current bankroll from latest portfolio snapshot."""
        snapshot = await self._db.get_latest_snapshot()
        if snapshot:
            self._bankroll = snapshot.bankroll
            logger.info(f"Restored bankroll from snapshot: ${self._bankroll:,.2f}")
        else:
            logger.info(f"Starting fresh with bankroll: ${self._bankroll:,.2f}")

    async def process_signals(self) -> List[PaperTrade]:
        """
        Process all unacted signals and open paper trades.
        Returns list of newly opened paper trades.
        """
        unacted = await self._db.get_unacted_signals()
        if not unacted:
            logger.debug("No unacted signals to process")
            return []

        # Build set of (market_id, wallet) already open to avoid duplicates
        open_trades = await self._db.get_open_paper_trades()
        open_keys: set = {(t.market_id, t.wallet_followed) for t in open_trades}

        opened: List[PaperTrade] = []
        skipped_stale = 0
        for signal in unacted:
            valid, reason = await self._validate_signal_freshness(signal)
            if not valid:
                logger.debug(f"Skip signal {signal.id} ({signal.market_id[:12]}…): {reason}")
                await self._db.mark_signal_acted_on(signal.id or 0)
                skipped_stale += 1
                continue
            # Skip if we already have an open position in this market from the same wallet
            key = (signal.market_id, signal.wallet_address)
            if key in open_keys:
                logger.debug(f"Skip signal {signal.id}: already open in {signal.market_id[:12]}…")
                await self._db.mark_signal_acted_on(signal.id or 0)
                continue
            trade = await self._open_trade(signal)
            if trade:
                opened.append(trade)
                open_keys.add(key)
                await self._db.mark_signal_acted_on(signal.id or 0)

        if skipped_stale:
            logger.info(f"Skipped {skipped_stale} stale/invalid signals")
        if opened:
            logger.info(f"Opened {len(opened)} new paper trades")
        return opened

    async def _validate_signal_freshness(self, signal: Signal) -> tuple[bool, str]:
        """
        Validate that a signal is still worth acting on.

        Rules:
        1. Signal must be < 60 minutes old (price may have moved beyond entry).
        2. If DB has a more recent trade for the same asset, current price must
           be within 1% of the signal price.
        """
        age_minutes = (datetime.utcnow() - signal.generated_at).total_seconds() / 60
        if age_minutes > 60:
            return False, f"signal too old ({age_minutes:.0f} min)"

        # Check price drift using latest DB trade for the same asset
        if signal.asset_id:
            last_price = await self._db.get_last_trade_price(signal.asset_id)
            if last_price is not None:
                entry = float(signal.price)
                drift = abs(last_price - entry) / entry if entry > 0 else 0
                if drift > 0.01:
                    return False, f"price drifted {drift:.1%} from signal entry"

        return True, "ok"

    async def resolve_closed_markets(self) -> List[PaperTrade]:
        """
        Check open paper trades for markets that have resolved.

        Primary method: check wallet_trades — if the tracked wallet has SELL trades
        for the market whose FIFO-matched resolved_price is set, use that price.
        This avoids unreliable Gamma API hex-condition-ID lookups entirely.

        Returns list of closed trades.
        """
        open_trades = await self._db.get_open_paper_trades()
        if not open_trades:
            return []

        closed: List[PaperTrade] = []

        for trade in open_trades:
            try:
                exit_price = await self._db.get_wallet_exit_price(
                    trade.market_id, trade.wallet_followed
                )
                if exit_price is None:
                    continue

                closed_trade = await self._close_trade(trade, Decimal(str(exit_price)))
                if closed_trade:
                    closed.append(closed_trade)
                    logger.info(
                        f"Closed trade #{trade.id} via wallet exit: "
                        f"market={trade.market_id[:12]}… exit={exit_price:.4f}"
                    )

            except Exception as exc:
                logger.error(f"Error resolving trade #{trade.id}: {exc}")

        if closed:
            logger.info(f"Resolved {len(closed)} paper trades from closed markets")

        return closed

    async def take_daily_snapshot(self) -> PortfolioSnapshot:
        """Take a portfolio snapshot and persist it."""
        stats = await self._db.get_paper_trade_stats()
        open_trades = await self._db.get_open_paper_trades()

        # Current open position value at entry prices (conservative)
        open_value = sum(t.dollar_amount for t in open_trades)

        wins = stats.get("wins", 0)
        losses = stats.get("losses", 0)
        total_closed = stats.get("total_closed", 0)
        win_rate = stats.get("win_rate", 0.0)
        profit_factor = stats.get("profit_factor", 0.0)
        total_pnl = Decimal(str(stats.get("total_pnl", 0.0)))

        # Reconstruct bankroll: starting + realised PnL
        current_bankroll = Decimal(str(settings.starting_bankroll)) + total_pnl

        # Track peak and drawdown
        if current_bankroll > self._peak_bankroll:
            self._peak_bankroll = current_bankroll
        if self._peak_bankroll > 0:
            dd = float((self._peak_bankroll - current_bankroll) / self._peak_bankroll)
            self._max_drawdown = max(self._max_drawdown, dd)

        self._bankroll = current_bankroll

        snapshot = PortfolioSnapshot(
            snapshot_date=datetime.utcnow(),
            bankroll=current_bankroll,
            total_pnl=total_pnl,
            open_positions_value=open_value,
            closed_trades=total_closed,
            winning_trades=wins,
            losing_trades=losses,
            win_rate=win_rate,
            profit_factor=profit_factor,
            max_drawdown_to_date=self._max_drawdown,
        )

        await self._db.upsert_portfolio_snapshot(snapshot)
        logger.info(
            f"Snapshot: bankroll=${current_bankroll:,.2f} "
            f"pnl=${total_pnl:+,.2f} "
            f"win_rate={win_rate:.1%} "
            f"pf={profit_factor:.2f}"
        )
        return snapshot

    async def run_paper_trade_cycle(self) -> None:
        """
        Single cycle of the paper trader:
        1. Process pending signals → open trades
        2. Resolve closed markets → close trades
        3. Take snapshot
        """
        await self.process_signals()
        await self.resolve_closed_markets()
        await self.take_daily_snapshot()

    async def get_performance_report(self) -> Dict:
        """Generate a comprehensive performance report dict."""
        stats = await self._db.get_paper_trade_stats()
        open_trades = await self._db.get_open_paper_trades()
        closed_trades = await self._db.get_closed_paper_trades(limit=50)
        snapshots = await self._db.get_portfolio_history(days=90)
        tracked = await self._db.get_tracked_wallets()

        # Equity curve (list of [date, bankroll])
        equity_curve = [
            {
                "date": s.snapshot_date.date().isoformat(),
                "bankroll": float(s.bankroll),
                "total_pnl": float(s.total_pnl),
            }
            for s in snapshots
        ]

        # Per-wallet stats on paper trades
        wallet_perf: Dict[str, Dict] = {}
        for t in closed_trades:
            w = t.wallet_followed
            if w not in wallet_perf:
                wallet_perf[w] = {"trades": 0, "wins": 0, "pnl": 0.0}
            wallet_perf[w]["trades"] += 1
            if t.status == PaperTradeStatus.CLOSED_WIN:
                wallet_perf[w]["wins"] += 1
            wallet_perf[w]["pnl"] += float(t.pnl or 0)

        # Price bucket breakdown of signals/trades
        bucket_breakdown: Dict[str, int] = {}
        for t in closed_trades + open_trades:
            b = t.price_bucket
            bucket_breakdown[b] = bucket_breakdown.get(b, 0) + 1

        # Unrealised PnL — fetch live CLOB ask prices for all open positions
        unrealised_pnl = 0.0
        priced_count = 0
        from src.api.clob_client import ClobClient as _ClobClient
        async with _ClobClient() as _clob:
            for t in open_trades:
                if not t.asset_id:
                    continue
                try:
                    live_price = await _clob.get_best_ask(t.asset_id)
                except Exception:
                    live_price = None
                if live_price is None:
                    # fall back to last DB price
                    live_price = await self._db.get_last_trade_price(t.asset_id)
                if live_price is not None:
                    unrealised_pnl += (live_price - float(t.entry_price)) * float(t.shares)
                    priced_count += 1
        logger.debug(f"Unrealised P&L computed from live prices: {priced_count}/{len(open_trades)} positions priced")

        # Top 5 closed trades by PnL
        top_trades = sorted(closed_trades, key=lambda t: float(t.pnl or 0), reverse=True)[:5]
        top_trades_data = [
            {
                "id": t.id,
                "market_id": t.market_id,
                "wallet": t.wallet_followed,
                "pnl": float(t.pnl or 0),
                "entry_price": float(t.entry_price),
                "exit_price": float(t.exit_price or 0),
                "dollar_amount": float(t.dollar_amount),
            }
            for t in top_trades
        ]

        # Always recompute from live DB totals — never trust stale snapshot
        realised_pnl = stats.get("total_pnl", 0.0)
        current_bankroll = settings.starting_bankroll + realised_pnl
        total_portfolio_value = current_bankroll + unrealised_pnl

        return {
            "bankroll": current_bankroll,
            "starting_bankroll": settings.starting_bankroll,
            "total_portfolio_value": total_portfolio_value,
            "total_pnl": stats.get("total_pnl", 0.0),
            "total_pnl_pct": (
                stats.get("total_pnl", 0.0) / settings.starting_bankroll
            ),
            "unrealised_pnl": unrealised_pnl,
            "open_count": stats.get("open_count", 0),
            "closed_count": stats.get("total_closed", 0),
            "wins": stats.get("wins", 0),
            "losses": stats.get("losses", 0),
            "win_rate": stats.get("win_rate", 0.0),
            "profit_factor": stats.get("profit_factor", 0.0),
            "gross_profit": stats.get("gross_profit", 0.0),
            "gross_loss": stats.get("gross_loss", 0.0),
            "max_drawdown": self._max_drawdown,
            "tracked_wallets": len(tracked),
            "equity_curve": equity_curve,
            "wallet_performance": wallet_perf,
            "price_bucket_breakdown": bucket_breakdown,
            "top_trades": top_trades_data,
        }

    # ── Internal helpers ───────────────────────────────────────────────────────

    async def _open_trade(self, signal: Signal) -> Optional[PaperTrade]:
        """
        Open a paper trade from a signal.
        Checks bankroll has sufficient funds before opening.
        """
        # Recalculate Kelly size with current bankroll
        from src.analysis.kelly import calculate_kelly
        kelly = calculate_kelly(
            win_rate=signal.wallet_win_rate,
            entry_price=float(signal.price),
            bankroll=self._bankroll,
        )

        if kelly.dollar_amount <= Decimal("0"):
            logger.debug(
                f"Skip signal {signal.id}: Kelly size is $0 "
                f"(bankroll=${self._bankroll:,.2f})"
            )
            return None

        # Check we have enough free capital
        open_trades = await self._db.get_open_paper_trades()
        committed = sum(t.dollar_amount for t in open_trades)
        available = self._bankroll - committed

        if kelly.dollar_amount > available:
            # Scale down to available
            if available < Decimal("10"):
                logger.warning("Insufficient free capital to open trade")
                return None
            dollar_amount = available.quantize(Decimal("0.01"))
            shares = (dollar_amount / signal.price).quantize(
                Decimal("0.0001"), rounding=ROUND_HALF_UP
            )
        else:
            dollar_amount = kelly.dollar_amount
            shares = (dollar_amount / signal.price).quantize(
                Decimal("0.0001"), rounding=ROUND_HALF_UP
            )

        trade = PaperTrade(
            signal_id=signal.id,
            market_id=signal.market_id,
            asset_id=signal.asset_id,
            wallet_followed=signal.wallet_address,
            side="BUY",
            entry_price=signal.price,
            shares=shares,
            dollar_amount=dollar_amount,
            kelly_fraction_used=kelly.recommended_fraction,
            price_bucket=signal.price_bucket,
            status=PaperTradeStatus.OPEN,
            opened_at=datetime.utcnow(),
            notes=f"signal_id={signal.id} kelly={kelly.recommended_fraction:.4f}",
        )

        trade_id = await self._db.insert_paper_trade(trade)
        trade.id = trade_id

        logger.info(
            f"Opened paper trade #{trade_id}: "
            f"market={signal.market_id[:12]}… "
            f"price={float(signal.price):.4f} "
            f"shares={float(shares):.2f} "
            f"amount=${float(dollar_amount):.2f}"
        )
        return trade

    async def _close_trade(
        self,
        trade: PaperTrade,
        resolved_price: Decimal,
    ) -> Optional[PaperTrade]:
        """Close a paper trade at the resolved price."""
        if trade.id is None:
            return None

        # Determine win/loss
        pnl = (resolved_price - trade.entry_price) * trade.shares
        status = (
            PaperTradeStatus.CLOSED_WIN
            if pnl >= Decimal("0")
            else PaperTradeStatus.CLOSED_LOSS
        )

        closed = await self._db.close_paper_trade(
            trade_id=trade.id,
            exit_price=resolved_price,
            status=status,
            closed_at=datetime.utcnow(),
        )

        outcome_str = "WIN" if status == PaperTradeStatus.CLOSED_WIN else "LOSS"
        logger.info(
            f"Closed paper trade #{trade.id} [{outcome_str}]: "
            f"entry={float(trade.entry_price):.4f} "
            f"exit={float(resolved_price):.4f} "
            f"pnl=${float(pnl):+.2f}"
        )
        return closed

    async def _get_resolved_price(self, market_id: str, market) -> Optional[Decimal]:
        """
        Determine the resolution price for a closed market.
        YES resolution = 1.0, NO resolution = 0.0.
        If last_trade_price is very close to 1.0 or 0.0, use that.
        """
        ltp = market.last_trade_price
        if ltp is not None:
            price = float(ltp)
            if price >= 0.95:
                return Decimal("1.0")
            if price <= 0.05:
                return Decimal("0.0")
            # Ambiguous — try outcome_prices field
            if market.outcome_prices:
                try:
                    prices = [float(p) for p in market.outcome_prices.split(",")]
                    if prices:
                        yes_price = prices[0]
                        if yes_price >= 0.95:
                            return Decimal("1.0")
                        if yes_price <= 0.05:
                            return Decimal("0.0")
                except (ValueError, AttributeError):
                    pass

        # Default: can't determine resolution
        return None


# ── Backtesting ────────────────────────────────────────────────────────────────

class Backtester:
    """
    Replay historical signals against the paper trader logic.
    Uses only signals already in DB, sorted chronologically.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    async def run(self) -> Dict:
        """
        Replay all historical signals in DB against paper trader logic.
        Returns aggregate performance metrics.
        """
        logger.info("Starting backtest — replaying historical signals")

        bankroll = Decimal(str(settings.starting_bankroll))
        peak = bankroll
        max_dd = 0.0
        gross_profit = Decimal("0")
        gross_loss = Decimal("0")
        wins = 0
        losses = 0
        trades_opened: List[Dict] = []

        # Fetch all signals in chronological order
        async with self._db._conn.execute(  # type: ignore
            "SELECT * FROM signals ORDER BY generated_at ASC"
        ) as cur:
            signal_rows = await cur.fetchall()

        # Fetch all paper trades for result lookup
        async with self._db._conn.execute(  # type: ignore
            "SELECT * FROM paper_trades WHERE status != 'OPEN' ORDER BY opened_at ASC"
        ) as cur:
            trade_rows = await cur.fetchall()

        from src.storage.database import _row_to_paper_trade
        closed_trades = [_row_to_paper_trade(r) for r in trade_rows]
        trade_by_signal: Dict[int, PaperTrade] = {
            t.signal_id: t for t in closed_trades if t.signal_id
        }

        for sig_row in signal_rows:
            sig = dict(sig_row)
            sig_id = sig["id"]
            price = float(sig["price"])

            if sig_id not in trade_by_signal:
                continue

            trade = trade_by_signal[sig_id]
            pnl = trade.pnl or Decimal("0")
            bankroll += pnl

            if pnl > 0:
                gross_profit += pnl
                wins += 1
            else:
                gross_loss += abs(pnl)
                losses += 1

            if bankroll > peak:
                peak = bankroll
            if peak > 0:
                dd = float((peak - bankroll) / peak)
                max_dd = max(max_dd, dd)

            trades_opened.append({
                "signal_id": sig_id,
                "price": price,
                "pnl": float(pnl),
                "bankroll": float(bankroll),
            })

        total = wins + losses
        profit_factor = float(gross_profit / gross_loss) if gross_loss > 0 else (10.0 if gross_profit > 0 else 0.0)

        result = {
            "starting_bankroll": float(settings.starting_bankroll),
            "ending_bankroll": float(bankroll),
            "total_pnl": float(bankroll - Decimal(str(settings.starting_bankroll))),
            "total_pnl_pct": float(
                (bankroll - Decimal(str(settings.starting_bankroll)))
                / Decimal(str(settings.starting_bankroll))
            ),
            "total_trades": total,
            "wins": wins,
            "losses": losses,
            "win_rate": wins / total if total > 0 else 0.0,
            "profit_factor": profit_factor,
            "gross_profit": float(gross_profit),
            "gross_loss": float(gross_loss),
            "max_drawdown": max_dd,
            "equity_curve": trades_opened,
        }

        logger.info(
            f"Backtest complete: {total} trades, "
            f"win_rate={result['win_rate']:.1%}, "
            f"pf={profit_factor:.2f}, "
            f"pnl=${result['total_pnl']:+,.2f}, "
            f"max_dd={max_dd:.1%}"
        )
        return result
