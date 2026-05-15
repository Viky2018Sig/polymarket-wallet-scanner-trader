"""
SQLite persistence layer using aiosqlite with WAL mode.

Tables:
- wallets          — scored wallets with all metrics
- paper_trades     — open and closed paper positions
- wallet_trades    — raw trade data cached from API
- signals          — generated signals with timestamps
- portfolio_snapshots — daily bankroll snapshots for equity curve
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiosqlite
from loguru import logger

from config import settings
from src.api.models import (
    PaperTrade,
    PaperTradeStatus,
    PortfolioSnapshot,
    Signal,
    SignalType,
    WalletScore,
    WalletTrade,
)


class Database:
    """Async SQLite database with WAL mode and type-safe helpers."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._path = db_path or settings.database_path
        self._conn: Optional[aiosqlite.Connection] = None

    async def __aenter__(self) -> "Database":
        await self.connect()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def connect(self) -> None:
        """Open connection and run schema migration."""
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path, timeout=30)
        self._conn.row_factory = aiosqlite.Row
        # Enable WAL mode for concurrent reads
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.execute("PRAGMA cache_size=-32768")  # 32 MB
        await self._conn.execute("PRAGMA busy_timeout=30000")  # 30s wait on lock
        await self._migrate()
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    # ── Schema migration ───────────────────────────────────────────────────────

    async def _migrate(self) -> None:
        """Create tables if they don't exist (idempotent)."""
        assert self._conn is not None

        await self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS wallets (
                wallet_address      TEXT PRIMARY KEY,
                total_trades        INTEGER NOT NULL DEFAULT 0,
                resolved_trades     INTEGER NOT NULL DEFAULT 0,
                winning_trades      INTEGER NOT NULL DEFAULT 0,
                losing_trades       INTEGER NOT NULL DEFAULT 0,
                win_rate            REAL NOT NULL DEFAULT 0,
                profit_factor       REAL NOT NULL DEFAULT 0,
                avg_rr              REAL NOT NULL DEFAULT 0,
                low_price_pct       REAL NOT NULL DEFAULT 0,
                max_drawdown        REAL NOT NULL DEFAULT 0,
                recency_score       REAL NOT NULL DEFAULT 0,
                diversity_score     REAL NOT NULL DEFAULT 0,
                composite_score     REAL NOT NULL DEFAULT 0,
                total_pnl           TEXT NOT NULL DEFAULT '0',
                gross_profit        TEXT NOT NULL DEFAULT '0',
                gross_loss          TEXT NOT NULL DEFAULT '0',
                active_markets      INTEGER NOT NULL DEFAULT 0,
                last_trade_at       TEXT,
                first_trade_at      TEXT,
                qualifies           INTEGER NOT NULL DEFAULT 0,
                disqualify_reason   TEXT,
                scored_at           TEXT NOT NULL,
                is_tracked          INTEGER NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_wallets_composite
                ON wallets (qualifies, composite_score DESC);

            CREATE TABLE IF NOT EXISTS wallet_trades (
                trade_id            TEXT PRIMARY KEY,
                wallet_address      TEXT NOT NULL,
                market_id           TEXT NOT NULL,
                asset_id            TEXT,
                side                TEXT NOT NULL,
                price               TEXT NOT NULL,
                size                TEXT NOT NULL,
                notional_usd        TEXT NOT NULL,
                matched_at          TEXT NOT NULL,
                outcome             TEXT,
                resolved_price      TEXT,
                pnl                 TEXT,
                created_at          TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_wt_wallet
                ON wallet_trades (wallet_address, matched_at DESC);

            CREATE INDEX IF NOT EXISTS idx_wt_market
                ON wallet_trades (market_id);

            CREATE TABLE IF NOT EXISTS signals (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_type         TEXT NOT NULL,
                wallet_address      TEXT NOT NULL,
                market_id           TEXT NOT NULL,
                asset_id            TEXT,
                price               TEXT NOT NULL,
                kelly_fraction      REAL NOT NULL,
                recommended_size_usd TEXT NOT NULL,
                wallet_win_rate     REAL NOT NULL,
                wallet_profit_factor REAL NOT NULL,
                price_bucket        TEXT NOT NULL,
                notes               TEXT NOT NULL DEFAULT '',
                generated_at        TEXT NOT NULL,
                acted_on            INTEGER NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_signals_wallet
                ON signals (wallet_address, generated_at DESC);

            CREATE TABLE IF NOT EXISTS paper_trades (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id           INTEGER REFERENCES signals(id),
                market_id           TEXT NOT NULL,
                asset_id            TEXT,
                wallet_followed     TEXT NOT NULL,
                side                TEXT NOT NULL DEFAULT 'BUY',
                entry_price         TEXT NOT NULL,
                shares              TEXT NOT NULL,
                dollar_amount       TEXT NOT NULL,
                kelly_fraction_used REAL NOT NULL,
                price_bucket        TEXT NOT NULL,
                status              TEXT NOT NULL DEFAULT 'OPEN',
                exit_price          TEXT,
                pnl                 TEXT,
                pnl_pct             REAL,
                opened_at           TEXT NOT NULL,
                closed_at           TEXT,
                notes               TEXT NOT NULL DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_pt_status
                ON paper_trades (status, opened_at DESC);

            CREATE INDEX IF NOT EXISTS idx_pt_market
                ON paper_trades (market_id, status);

            CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_date           TEXT NOT NULL UNIQUE,
                bankroll                TEXT NOT NULL,
                total_pnl               TEXT NOT NULL,
                open_positions_value    TEXT NOT NULL,
                closed_trades           INTEGER NOT NULL DEFAULT 0,
                winning_trades          INTEGER NOT NULL DEFAULT 0,
                losing_trades           INTEGER NOT NULL DEFAULT 0,
                win_rate                REAL NOT NULL DEFAULT 0,
                profit_factor           REAL NOT NULL DEFAULT 0,
                max_drawdown_to_date    REAL NOT NULL DEFAULT 0
            );
        """)

    # ── Wallet scores ──────────────────────────────────────────────────────────

    async def upsert_wallet_scores(self, scores: List[WalletScore]) -> int:
        """Upsert wallet scores. Returns count of rows affected."""
        assert self._conn is not None
        rows_affected = 0

        for s in scores:
            await self._conn.execute(
                """
                INSERT INTO wallets (
                    wallet_address, total_trades, resolved_trades, winning_trades,
                    losing_trades, win_rate, profit_factor, avg_rr, low_price_pct,
                    max_drawdown, recency_score, diversity_score, composite_score,
                    total_pnl, gross_profit, gross_loss, active_markets,
                    last_trade_at, first_trade_at, qualifies, disqualify_reason,
                    scored_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                ON CONFLICT(wallet_address) DO UPDATE SET
                    total_trades        = excluded.total_trades,
                    resolved_trades     = excluded.resolved_trades,
                    winning_trades      = excluded.winning_trades,
                    losing_trades       = excluded.losing_trades,
                    win_rate            = excluded.win_rate,
                    profit_factor       = excluded.profit_factor,
                    avg_rr              = excluded.avg_rr,
                    low_price_pct       = excluded.low_price_pct,
                    max_drawdown        = excluded.max_drawdown,
                    recency_score       = excluded.recency_score,
                    diversity_score     = excluded.diversity_score,
                    composite_score     = excluded.composite_score,
                    total_pnl           = excluded.total_pnl,
                    gross_profit        = excluded.gross_profit,
                    gross_loss          = excluded.gross_loss,
                    active_markets      = excluded.active_markets,
                    last_trade_at       = excluded.last_trade_at,
                    first_trade_at      = excluded.first_trade_at,
                    qualifies           = excluded.qualifies,
                    disqualify_reason   = excluded.disqualify_reason,
                    scored_at           = excluded.scored_at
                """,
                (
                    s.wallet_address,
                    s.total_trades,
                    s.resolved_trades,
                    s.winning_trades,
                    s.losing_trades,
                    s.win_rate,
                    s.profit_factor,
                    s.avg_rr,
                    s.low_price_pct,
                    s.max_drawdown,
                    s.recency_score,
                    s.diversity_score,
                    s.composite_score,
                    str(s.total_pnl),
                    str(s.gross_profit),
                    str(s.gross_loss),
                    s.active_markets,
                    _dt_str(s.last_trade_at),
                    _dt_str(s.first_trade_at),
                    1 if s.qualifies else 0,
                    s.disqualify_reason,
                    _dt_str(s.scored_at),
                ),
            )
            rows_affected += 1

        await self._conn.commit()
        return rows_affected

    async def get_wallet_score(self, wallet_address: str) -> Optional[WalletScore]:
        """Fetch a wallet score by address."""
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT * FROM wallets WHERE wallet_address = ?",
            (wallet_address.lower(),),
        ) as cur:
            row = await cur.fetchone()
            if row is None:
                return None
            return _row_to_wallet_score(row)

    async def get_top_wallets(
        self,
        n: int = 20,
        min_score: float = 0.0,
        qualified_only: bool = True,
    ) -> List[WalletScore]:
        """Fetch top-N wallets ordered by composite score."""
        assert self._conn is not None
        qual_filter = "qualifies = 1 AND " if qualified_only else ""
        async with self._conn.execute(
            f"""
            SELECT * FROM wallets
            WHERE {qual_filter} composite_score >= ?
            ORDER BY composite_score DESC
            LIMIT ?
            """,
            (min_score, n),
        ) as cur:
            rows = await cur.fetchall()
            return [_row_to_wallet_score(r) for r in rows]

    async def get_all_wallet_addresses(self) -> List[str]:
        """Return all wallet addresses stored in DB."""
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT wallet_address FROM wallets"
        ) as cur:
            rows = await cur.fetchall()
            return [r[0] for r in rows]

    async def mark_wallet_tracked(self, wallet_address: str, tracked: bool = True) -> None:
        """Mark a wallet as actively tracked for signal generation."""
        assert self._conn is not None
        await self._conn.execute(
            "UPDATE wallets SET is_tracked = ? WHERE wallet_address = ?",
            (1 if tracked else 0, wallet_address.lower()),
        )
        await self._conn.commit()

    async def get_tracked_wallets(self) -> List[WalletScore]:
        """Return all wallets marked as tracked."""
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT * FROM wallets WHERE is_tracked = 1 ORDER BY composite_score DESC"
        ) as cur:
            rows = await cur.fetchall()
            return [_row_to_wallet_score(r) for r in rows]

    # ── Wallet trades cache ────────────────────────────────────────────────────

    async def upsert_wallet_trades(self, trades: List[WalletTrade]) -> int:
        """Upsert trade records. Returns count inserted/updated."""
        assert self._conn is not None
        count = 0
        for t in trades:
            await self._conn.execute(
                """
                INSERT INTO wallet_trades (
                    trade_id, wallet_address, market_id, asset_id, side,
                    price, size, notional_usd, matched_at, outcome,
                    resolved_price, pnl
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trade_id) DO UPDATE SET
                    resolved_price = excluded.resolved_price,
                    pnl            = excluded.pnl,
                    outcome        = excluded.outcome
                """,
                (
                    t.trade_id,
                    t.wallet_address.lower(),
                    t.market_id,
                    t.asset_id,
                    t.side,
                    str(t.price),
                    str(t.size),
                    str(t.notional_usd),
                    t.matched_at.isoformat(),
                    t.outcome,
                    str(t.resolved_price) if t.resolved_price is not None else None,
                    str(t.pnl) if t.pnl is not None else None,
                ),
            )
            count += 1
        await self._conn.commit()
        return count

    async def get_wallet_trades(
        self,
        wallet_address: str,
        since_days: Optional[int] = None,
    ) -> List[WalletTrade]:
        """Fetch cached trades for a wallet, optionally filtered by recency."""
        assert self._conn is not None
        if since_days is not None:
            cutoff = (datetime.utcnow().replace(microsecond=0)
                      - __import__("datetime").timedelta(days=since_days)).isoformat()
            async with self._conn.execute(
                """
                SELECT * FROM wallet_trades
                WHERE wallet_address = ? AND matched_at >= ?
                ORDER BY matched_at ASC
                """,
                (wallet_address.lower(), cutoff),
            ) as cur:
                rows = await cur.fetchall()
        else:
            async with self._conn.execute(
                """
                SELECT * FROM wallet_trades
                WHERE wallet_address = ?
                ORDER BY matched_at ASC
                """,
                (wallet_address.lower(),),
            ) as cur:
                rows = await cur.fetchall()

        return [_row_to_wallet_trade(r) for r in rows]

    async def get_recent_trades_for_wallet(
        self,
        wallet_address: str,
        limit: int = 50,
    ) -> List[WalletTrade]:
        """Return the most recent N trades for a wallet."""
        assert self._conn is not None
        async with self._conn.execute(
            """
            SELECT * FROM wallet_trades
            WHERE wallet_address = ?
            ORDER BY matched_at DESC
            LIMIT ?
            """,
            (wallet_address.lower(), limit),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_wallet_trade(r) for r in rows]

    async def update_trade_resolution(
        self,
        market_id: str,
        resolved_price: Decimal,
    ) -> int:
        """
        Update all wallet_trades for a market with resolved price and PnL.
        Returns number of trades updated.
        """
        assert self._conn is not None

        # Fetch all BUY trades for this market
        async with self._conn.execute(
            """
            SELECT trade_id, price, size
            FROM wallet_trades
            WHERE market_id = ? AND side = 'BUY' AND resolved_price IS NULL
            """,
            (market_id,),
        ) as cur:
            rows = await cur.fetchall()

        count = 0
        for row in rows:
            trade_id, price_str, size_str = row[0], row[1], row[2]
            price = Decimal(price_str)
            size = Decimal(size_str)
            pnl = (resolved_price - price) * size
            await self._conn.execute(
                """
                UPDATE wallet_trades
                SET resolved_price = ?, pnl = ?
                WHERE trade_id = ?
                """,
                (str(resolved_price), str(pnl), trade_id),
            )
            count += 1

        await self._conn.commit()
        return count

    async def get_unresolved_market_ids(self) -> list:
        """Return distinct market_ids that have BUY trades with no resolved_price."""
        assert self._conn is not None
        async with self._conn.execute(
            """
            SELECT DISTINCT market_id FROM wallet_trades
            WHERE side = 'BUY' AND resolved_price IS NULL AND market_id != ''
            """
        ) as cur:
            rows = await cur.fetchall()
        return [r[0] for r in rows]

    async def update_trade_resolution_by_outcome(
        self,
        market_id: str,
        yes_resolved_price: Decimal,
        no_resolved_price: Decimal,
    ) -> int:
        """
        Update wallet_trades for a resolved market, applying the correct
        resolved_price per outcome:
          - Trades with outcome='Yes'  → yes_resolved_price
          - Trades with outcome='No'   → no_resolved_price
          - Trades with NULL outcome   → yes_resolved_price (assume YES token)
        Returns number of rows updated.
        """
        assert self._conn is not None

        async with self._conn.execute(
            """
            SELECT trade_id, price, size, outcome
            FROM wallet_trades
            WHERE market_id = ? AND side = 'BUY' AND resolved_price IS NULL
            """,
            (market_id,),
        ) as cur:
            rows = await cur.fetchall()

        count = 0
        for row in rows:
            trade_id, price_str, size_str, outcome = row
            price = Decimal(price_str)
            size = Decimal(size_str)

            # Determine which resolved price applies to this trade's token
            if outcome and outcome.lower() in ("no", "n"):
                resolved_price = no_resolved_price
            else:
                resolved_price = yes_resolved_price

            pnl = (resolved_price - price) * size
            await self._conn.execute(
                "UPDATE wallet_trades SET resolved_price = ?, pnl = ? WHERE trade_id = ?",
                (str(resolved_price), str(pnl), trade_id),
            )
            count += 1

        await self._conn.commit()
        return count

    # ── Signals ────────────────────────────────────────────────────────────────

    async def insert_signal(self, signal: Signal) -> int:
        """Insert a new signal. Returns the new row ID."""
        assert self._conn is not None
        cur = await self._conn.execute(
            """
            INSERT INTO signals (
                signal_type, wallet_address, market_id, asset_id, price,
                kelly_fraction, recommended_size_usd, wallet_win_rate,
                wallet_profit_factor, price_bucket, notes, generated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                signal.signal_type.value,
                signal.wallet_address,
                signal.market_id,
                signal.asset_id,
                str(signal.price),
                signal.kelly_fraction,
                str(signal.recommended_size_usd),
                signal.wallet_win_rate,
                signal.wallet_profit_factor,
                signal.price_bucket,
                signal.notes,
                signal.generated_at.isoformat(),
            ),
        )
        await self._conn.commit()
        return cur.lastrowid or 0

    async def get_recent_signals(self, limit: int = 20) -> List[Signal]:
        """Fetch the most recent signals."""
        assert self._conn is not None
        async with self._conn.execute(
            """
            SELECT * FROM signals
            ORDER BY generated_at DESC
            LIMIT ?
            """,
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_signal(r) for r in rows]

    async def mark_signal_acted_on(self, signal_id: int) -> None:
        """Mark a signal as having been acted upon (paper trade opened)."""
        assert self._conn is not None
        await self._conn.execute(
            "UPDATE signals SET acted_on = 1 WHERE id = ?",
            (signal_id,),
        )
        await self._conn.commit()

    async def get_unacted_signals(self) -> List[Signal]:
        """Fetch signals not yet turned into paper trades."""
        assert self._conn is not None
        async with self._conn.execute(
            """
            SELECT * FROM signals
            WHERE acted_on = 0
            ORDER BY generated_at ASC
            """,
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_signal(r) for r in rows]

    async def get_wallet_exit_price(self, market_id: str, wallet_address: str) -> Optional[float]:
        """
        Return the FIFO-resolved exit price for a (market, wallet) pair if the
        tracked wallet has fully exited — i.e. all BUY trades have a resolved_price.
        Returns None if the wallet still holds an open position.
        """
        assert self._conn is not None
        async with self._conn.execute(
            """
            SELECT
                COUNT(*) as total_buys,
                SUM(CASE WHEN resolved_price IS NOT NULL THEN 1 ELSE 0 END) as resolved_buys,
                AVG(CAST(resolved_price AS REAL)) as avg_exit
            FROM wallet_trades
            WHERE market_id = ? AND wallet_address = ? AND side = 'BUY'
            """,
            (market_id, wallet_address),
        ) as cur:
            row = await cur.fetchone()
        if not row or not row[0]:
            return None
        total, resolved, avg_exit = row
        # Only consider fully exited (all BUYs resolved)
        if resolved < total or avg_exit is None:
            return None
        return float(avg_exit)

    async def get_last_trade_price(self, asset_id: str) -> Optional[float]:
        """Return the most recent trade price for a given asset/token ID."""
        assert self._conn is not None
        async with self._conn.execute(
            """
            SELECT CAST(price AS REAL) FROM wallet_trades
            WHERE asset_id = ? ORDER BY matched_at DESC LIMIT 1
            """,
            (asset_id,),
        ) as cur:
            row = await cur.fetchone()
        return float(row[0]) if row and row[0] is not None else None

    async def get_last_signal_time(self, wallet_address: str) -> Optional[datetime]:
        """Return the most recent signal generation time for a wallet, or None."""
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT MAX(generated_at) FROM signals WHERE wallet_address = ?",
            (wallet_address,),
        ) as cur:
            row = await cur.fetchone()
        if row and row[0]:
            try:
                return datetime.fromisoformat(row[0])
            except ValueError:
                return None
        return None

    async def get_tracked_asset_ids(self, since_days: int = 1, limit: int = 500) -> List[str]:
        """
        Return the most recently traded asset_ids of tracked wallets (for WS subscriptions).

        Ordered by most-recent trade so that the `limit` cap keeps the freshest markets.
        Use since_days=1 by default so 5-minute recurring markets are always included.
        """
        assert self._conn is not None
        cutoff = (datetime.utcnow() - __import__("datetime").timedelta(days=since_days)).isoformat()
        async with self._conn.execute(
            """
            SELECT wt.asset_id, MAX(wt.matched_at) AS last_seen
            FROM wallet_trades wt
            JOIN wallets w ON w.wallet_address = wt.wallet_address
            WHERE w.is_tracked = 1
              AND wt.asset_id IS NOT NULL
              AND wt.matched_at >= ?
            GROUP BY wt.asset_id
            ORDER BY last_seen DESC
            LIMIT ?
            """,
            (cutoff, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [r[0] for r in rows if r[0]]

    # ── Paper trades ───────────────────────────────────────────────────────────

    async def insert_paper_trade(self, trade: PaperTrade) -> int:
        """Insert a new paper trade. Returns the new row ID."""
        assert self._conn is not None
        cur = await self._conn.execute(
            """
            INSERT INTO paper_trades (
                signal_id, market_id, asset_id, wallet_followed, side,
                entry_price, shares, dollar_amount, kelly_fraction_used,
                price_bucket, status, exit_price, pnl, pnl_pct,
                opened_at, closed_at, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trade.signal_id,
                trade.market_id,
                trade.asset_id,
                trade.wallet_followed,
                trade.side,
                str(trade.entry_price),
                str(trade.shares),
                str(trade.dollar_amount),
                trade.kelly_fraction_used,
                trade.price_bucket,
                trade.status.value,
                str(trade.exit_price) if trade.exit_price is not None else None,
                str(trade.pnl) if trade.pnl is not None else None,
                trade.pnl_pct,
                trade.opened_at.isoformat(),
                trade.closed_at.isoformat() if trade.closed_at else None,
                trade.notes,
            ),
        )
        await self._conn.commit()
        return cur.lastrowid or 0

    async def close_paper_trade(
        self,
        trade_id: int,
        exit_price: Decimal,
        status: PaperTradeStatus,
        closed_at: Optional[datetime] = None,
    ) -> Optional[PaperTrade]:
        """
        Close an open paper trade with its resolution price.
        Calculates PnL automatically.
        """
        assert self._conn is not None

        # Fetch the trade first
        async with self._conn.execute(
            "SELECT * FROM paper_trades WHERE id = ?",
            (trade_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None

        trade = _row_to_paper_trade(row)
        entry = trade.entry_price
        shares = trade.shares

        # PnL = (exit_price - entry_price) * shares
        pnl = (exit_price - entry) * shares
        pnl_pct = float(pnl / trade.dollar_amount) if trade.dollar_amount != 0 else 0.0
        closed_dt = closed_at or datetime.utcnow()

        await self._conn.execute(
            """
            UPDATE paper_trades
            SET status     = ?,
                exit_price = ?,
                pnl        = ?,
                pnl_pct    = ?,
                closed_at  = ?
            WHERE id = ?
            """,
            (
                status.value,
                str(exit_price),
                str(pnl),
                pnl_pct,
                closed_dt.isoformat(),
                trade_id,
            ),
        )
        await self._conn.commit()

        # Return updated record
        async with self._conn.execute(
            "SELECT * FROM paper_trades WHERE id = ?", (trade_id,)
        ) as cur:
            row = await cur.fetchone()
        return _row_to_paper_trade(row) if row else None

    async def get_open_paper_trades(self) -> List[PaperTrade]:
        """Return all currently open paper trades."""
        assert self._conn is not None
        async with self._conn.execute(
            """
            SELECT * FROM paper_trades
            WHERE status = 'OPEN'
            ORDER BY opened_at DESC
            """,
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_paper_trade(r) for r in rows]

    async def get_closed_paper_trades(self, limit: int = 100) -> List[PaperTrade]:
        """Return recently closed paper trades."""
        assert self._conn is not None
        async with self._conn.execute(
            """
            SELECT * FROM paper_trades
            WHERE status != 'OPEN'
            ORDER BY closed_at DESC
            LIMIT ?
            """,
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_paper_trade(r) for r in rows]

    async def get_open_trades_for_market(self, market_id: str) -> List[PaperTrade]:
        """Return open paper trades for a specific market."""
        assert self._conn is not None
        async with self._conn.execute(
            """
            SELECT * FROM paper_trades
            WHERE market_id = ? AND status = 'OPEN'
            """,
            (market_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_paper_trade(r) for r in rows]

    async def get_paper_trade_stats(self) -> Dict[str, Any]:
        """Aggregate statistics across all closed paper trades."""
        assert self._conn is not None
        async with self._conn.execute(
            """
            SELECT
                COUNT(CASE WHEN status != 'OPEN' THEN 1 END) as total_closed,
                SUM(CASE WHEN status = 'CLOSED_WIN' THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN status = 'CLOSED_LOSS' THEN 1 ELSE 0 END) as losses,
                SUM(CASE WHEN pnl > 0 THEN CAST(pnl AS REAL) ELSE 0 END) as gross_profit,
                SUM(CASE WHEN pnl < 0 THEN ABS(CAST(pnl AS REAL)) ELSE 0 END) as gross_loss,
                SUM(CASE WHEN status != 'OPEN' THEN CAST(pnl AS REAL) ELSE 0 END) as total_pnl,
                COUNT(CASE WHEN status = 'OPEN' THEN 1 END) as open_count
            FROM paper_trades
            """
        ) as cur:
            row = await cur.fetchone()

        if row is None:
            return {}

        total = row["total_closed"] or 0
        wins = row["wins"] or 0
        losses = row["losses"] or 0
        gross_profit = row["gross_profit"] or 0.0
        gross_loss = row["gross_loss"] or 0.0
        total_pnl = row["total_pnl"] or 0.0
        open_count = row["open_count"] or 0

        return {
            "total_closed": total,
            "open_count": open_count,
            "wins": wins,
            "losses": losses,
            "win_rate": wins / total if total > 0 else 0.0,
            "gross_profit": gross_profit,
            "gross_loss": gross_loss,
            "total_pnl": total_pnl,
            "profit_factor": gross_profit / gross_loss if gross_loss > 0 else (10.0 if gross_profit > 0 else 0.0),
        }

    # ── Portfolio snapshots ────────────────────────────────────────────────────

    async def upsert_portfolio_snapshot(self, snapshot: PortfolioSnapshot) -> int:
        """Upsert a daily portfolio snapshot."""
        assert self._conn is not None
        cur = await self._conn.execute(
            """
            INSERT INTO portfolio_snapshots (
                snapshot_date, bankroll, total_pnl, open_positions_value,
                closed_trades, winning_trades, losing_trades, win_rate,
                profit_factor, max_drawdown_to_date
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(snapshot_date) DO UPDATE SET
                bankroll              = excluded.bankroll,
                total_pnl             = excluded.total_pnl,
                open_positions_value  = excluded.open_positions_value,
                closed_trades         = excluded.closed_trades,
                winning_trades        = excluded.winning_trades,
                losing_trades         = excluded.losing_trades,
                win_rate              = excluded.win_rate,
                profit_factor         = excluded.profit_factor,
                max_drawdown_to_date  = excluded.max_drawdown_to_date
            """,
            (
                snapshot.snapshot_date.date().isoformat(),
                str(snapshot.bankroll),
                str(snapshot.total_pnl),
                str(snapshot.open_positions_value),
                snapshot.closed_trades,
                snapshot.winning_trades,
                snapshot.losing_trades,
                snapshot.win_rate,
                snapshot.profit_factor,
                snapshot.max_drawdown_to_date,
            ),
        )
        await self._conn.commit()
        return cur.lastrowid or 0

    async def get_portfolio_history(
        self, days: int = 90
    ) -> List[PortfolioSnapshot]:
        """Return portfolio snapshots for the last N days."""
        assert self._conn is not None
        from datetime import timedelta
        cutoff = (datetime.utcnow() - timedelta(days=days)).date().isoformat()
        async with self._conn.execute(
            """
            SELECT * FROM portfolio_snapshots
            WHERE snapshot_date >= ?
            ORDER BY snapshot_date ASC
            """,
            (cutoff,),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_snapshot(r) for r in rows]

    async def get_latest_snapshot(self) -> Optional[PortfolioSnapshot]:
        """Get the most recent portfolio snapshot."""
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT * FROM portfolio_snapshots ORDER BY snapshot_date DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
        return _row_to_snapshot(row) if row else None

    # ── Housekeeping / pruning ─────────────────────────────────────────────────

    async def prune_old_data(
        self,
        wallet_trades_retention_days: int = 90,
        signals_retention_days: int = 7,
    ) -> Dict[str, int]:
        """
        Delete old rows to keep disk usage bounded.

        wallet_trades: prune resolved rows older than retention_days.
                       Unresolved BUY rows (resolved_price IS NULL) are never pruned
                       because the FIFO calculator still needs them.
        signals:       prune acted-on signals older than retention_days.
        snapshots:     keep one per day; drop sub-daily duplicates beyond 7 days.

        Returns dict of {table: rows_deleted}.
        """
        assert self._conn is not None
        from datetime import timedelta
        wt_cutoff = (datetime.utcnow() - timedelta(days=wallet_trades_retention_days)).isoformat()
        sig_cutoff = (datetime.utcnow() - timedelta(days=signals_retention_days)).isoformat()

        # Prune resolved wallet_trades beyond retention window
        cur = await self._conn.execute(
            """DELETE FROM wallet_trades
               WHERE resolved_price IS NOT NULL
                 AND matched_at < ?""",
            (wt_cutoff,),
        )
        wt_deleted = cur.rowcount

        # Prune acted-on signals beyond retention window
        cur = await self._conn.execute(
            """DELETE FROM signals
               WHERE acted_on = 1
                 AND generated_at < ?""",
            (sig_cutoff,),
        )
        sig_deleted = cur.rowcount

        # Prune duplicate snapshots: beyond 7 days keep only the latest per day
        snap_cutoff = (datetime.utcnow() - timedelta(days=7)).isoformat()
        cur = await self._conn.execute(
            """DELETE FROM portfolio_snapshots
               WHERE snapshot_date < ?
                 AND id NOT IN (
                     SELECT MAX(id) FROM portfolio_snapshots
                     WHERE snapshot_date < ?
                     GROUP BY DATE(snapshot_date)
                 )""",
            (snap_cutoff, snap_cutoff),
        )
        snap_deleted = cur.rowcount

        await self._conn.commit()
        await self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        # VACUUM cannot run on an active aiosqlite connection; use a dedicated sync call
        import sqlite3, asyncio
        def _vacuum(path: str) -> None:
            con = sqlite3.connect(path, isolation_level=None)
            con.execute("VACUUM")
            con.close()
        await asyncio.to_thread(_vacuum, str(self._path))

        logger.info(
            f"Pruned: {wt_deleted} wallet_trades, "
            f"{sig_deleted} signals, "
            f"{snap_deleted} snapshots — VACUUM complete"
        )
        return {
            "wallet_trades": wt_deleted,
            "signals": sig_deleted,
            "snapshots": snap_deleted,
        }

    async def db_size_bytes(self) -> int:
        """Return current database file size in bytes."""
        import os
        try:
            return os.path.getsize(self._path)
        except OSError:
            return 0


# ── Row converters ─────────────────────────────────────────────────────────────

def _dt_str(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _dec(s: Optional[str]) -> Decimal:
    if s is None:
        return Decimal("0")
    try:
        return Decimal(s)
    except Exception:
        return Decimal("0")


def _row_to_wallet_score(row: aiosqlite.Row) -> WalletScore:
    r = dict(row)
    return WalletScore(
        wallet_address=r["wallet_address"],
        total_trades=r["total_trades"],
        resolved_trades=r["resolved_trades"],
        winning_trades=r["winning_trades"],
        losing_trades=r["losing_trades"],
        win_rate=r["win_rate"],
        profit_factor=r["profit_factor"],
        avg_rr=r["avg_rr"],
        low_price_pct=r["low_price_pct"],
        max_drawdown=r["max_drawdown"],
        recency_score=r["recency_score"],
        diversity_score=r["diversity_score"],
        composite_score=r["composite_score"],
        total_pnl=_dec(r["total_pnl"]),
        gross_profit=_dec(r["gross_profit"]),
        gross_loss=_dec(r["gross_loss"]),
        active_markets=r["active_markets"],
        last_trade_at=_parse_dt(r["last_trade_at"]),
        first_trade_at=_parse_dt(r["first_trade_at"]),
        qualifies=bool(r["qualifies"]),
        disqualify_reason=r.get("disqualify_reason"),
        scored_at=_parse_dt(r["scored_at"]) or datetime.utcnow(),
    )


def _row_to_wallet_trade(row: aiosqlite.Row) -> WalletTrade:
    r = dict(row)
    return WalletTrade(
        trade_id=r["trade_id"],
        wallet_address=r["wallet_address"],
        market_id=r["market_id"],
        asset_id=r.get("asset_id"),
        side=r["side"],
        price=_dec(r["price"]),
        size=_dec(r["size"]),
        notional_usd=_dec(r["notional_usd"]),
        matched_at=_parse_dt(r["matched_at"]) or datetime.utcnow(),
        outcome=r.get("outcome"),
        resolved_price=_dec(r["resolved_price"]) if r.get("resolved_price") else None,
        pnl=_dec(r["pnl"]) if r.get("pnl") else None,
    )


def _row_to_signal(row: aiosqlite.Row) -> Signal:
    r = dict(row)
    return Signal(
        id=r["id"],
        signal_type=SignalType(r["signal_type"]),
        wallet_address=r["wallet_address"],
        market_id=r["market_id"],
        asset_id=r.get("asset_id"),
        price=_dec(r["price"]),
        kelly_fraction=r["kelly_fraction"],
        recommended_size_usd=_dec(r["recommended_size_usd"]),
        wallet_win_rate=r["wallet_win_rate"],
        wallet_profit_factor=r["wallet_profit_factor"],
        price_bucket=r["price_bucket"],
        notes=r.get("notes", ""),
        generated_at=_parse_dt(r["generated_at"]) or datetime.utcnow(),
    )


def _row_to_paper_trade(row: aiosqlite.Row) -> PaperTrade:
    r = dict(row)
    return PaperTrade(
        id=r["id"],
        signal_id=r.get("signal_id"),
        market_id=r["market_id"],
        asset_id=r.get("asset_id"),
        wallet_followed=r["wallet_followed"],
        side=r["side"],
        entry_price=_dec(r["entry_price"]),
        shares=_dec(r["shares"]),
        dollar_amount=_dec(r["dollar_amount"]),
        kelly_fraction_used=r["kelly_fraction_used"],
        price_bucket=r["price_bucket"],
        status=PaperTradeStatus(r["status"]),
        exit_price=_dec(r["exit_price"]) if r.get("exit_price") else None,
        pnl=_dec(r["pnl"]) if r.get("pnl") else None,
        pnl_pct=r.get("pnl_pct"),
        opened_at=_parse_dt(r["opened_at"]) or datetime.utcnow(),
        closed_at=_parse_dt(r.get("closed_at")),
        notes=r.get("notes", ""),
    )


def _row_to_snapshot(row: aiosqlite.Row) -> PortfolioSnapshot:
    r = dict(row)
    return PortfolioSnapshot(
        id=r["id"],
        snapshot_date=_parse_dt(r["snapshot_date"]) or datetime.utcnow(),
        bankroll=_dec(r["bankroll"]),
        total_pnl=_dec(r["total_pnl"]),
        open_positions_value=_dec(r["open_positions_value"]),
        closed_trades=r["closed_trades"],
        winning_trades=r["winning_trades"],
        losing_trades=r["losing_trades"],
        win_rate=r["win_rate"],
        profit_factor=r["profit_factor"],
        max_drawdown_to_date=r["max_drawdown_to_date"],
    )
