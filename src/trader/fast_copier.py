"""
Fast Copier — sub-second trade copying without G3 price-ceiling guard.

Architecture
────────────
Startup: build asset_id → (wallet, market_id) map from scanner DB wallet_trades.

  ┌──────────────────────────────────────────────────────────────────────────┐
  │  _ws_worker          ← CLOB WebSocket, last_trade_price events           │
  │  subscribed to all     price ≤ max_entry AND asset in map → instant copy  │
  │  known asset_ids       NO REST call; wallet known from pre-built map      │
  │       │                                                                   │
  │  _trade_processor    ← dedup, Kelly size, write to fast_trades            │
  │       │                                                                   │
  │  _activity_poller    ← fallback for brand-new markets (5-s REST poll)     │
  │  _position_closer    ← every 5 min, close at 0.99 (WIN) / 0.01 (LOSS)    │
  │  _asset_refresher    ← every 5 min, reload map + trigger WS resubscribe   │
  │  _snapshot_worker    ← log portfolio stats every 30 min                   │
  └──────────────────────────────────────────────────────────────────────────┘

Key differences from RealtimeMonitor
─────────────────────────────────────
  • No G3 block  — copies at the WS trade price directly (no current-ask check).
  • No signals table — goes straight to fast_trades.
  • No REST wallet lookup — wallet resolved from asset_map in O(1).
  • Activity poller catches brand-new markets the WS hasn't seen yet.
  • Separate SQLite database: fast_copier.db (independent from main scanner).

Usage
─────
  python fast_copier.py                          # defaults
  python fast_copier.py --max-price 0.15         # tighter filter
  python fast_copier.py --scanner-db /other/path/polymarket_scanner.db
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import aiosqlite
import httpx
from loguru import logger

_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
_CLOB_BASE = "https://clob.polymarket.com"
_DATA_BASE = "https://data-api.polymarket.com"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fast_trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id       TEXT NOT NULL,
    asset_id        TEXT NOT NULL,
    wallet_followed TEXT NOT NULL,
    entry_price     REAL NOT NULL,
    shares          REAL NOT NULL,
    dollar_amount   REAL NOT NULL,
    kelly_fraction  REAL,
    profit_factor   REAL,
    win_rate        REAL,
    status          TEXT NOT NULL DEFAULT 'OPEN',
    exit_price      REAL,
    pnl             REAL,
    opened_at       TEXT NOT NULL,
    closed_at       TEXT,
    notes           TEXT,
    UNIQUE(market_id, wallet_followed)
);

CREATE TABLE IF NOT EXISTS fast_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    bankroll      REAL,
    realized_pnl  REAL,
    open_count    INTEGER,
    closed_wins   INTEGER,
    closed_losses INTEGER,
    snapped_at    TEXT NOT NULL
);
"""


class FastCopier:
    """
    Sub-second paper trade copier.

    Tracks: asset_id → (wallet_address, market_id) map.
    On each WS last_trade_price BUY event at ≤ max_entry_price for a tracked
    asset, opens a paper trade immediately — no REST call required.
    """

    _WIN_THRESHOLD = Decimal("0.99")
    _LOSS_THRESHOLD = Decimal("0.01")

    _POSITION_CHECK_INTERVAL = 5 * 60   # seconds
    _ASSET_REFRESH_INTERVAL = 5 * 60
    _ACTIVITY_POLL_INTERVAL = 5          # seconds
    _SNAPSHOT_INTERVAL = 30 * 60

    def __init__(
        self,
        scanner_db_path: str,
        fast_db_path: str,
        max_entry_price: float = 0.20,
        starting_bankroll: float = 2000.0,
        max_position_pct: float = 0.0025,
        kelly_fraction: float = 0.25,
        lookback_days: int = 14,
    ) -> None:
        self._scanner_db = scanner_db_path
        self._fast_db = fast_db_path
        self._max_entry = max_entry_price
        self._bankroll = Decimal(str(starting_bankroll))
        self._max_pos_pct = max_position_pct
        self._kelly_frac = kelly_fraction
        self._lookback_days = lookback_days

        # asset_id → (wallet_address_lower, market_id)
        self._asset_map: Dict[str, Tuple[str, str]] = {}

        # Open position dedup: (market_id, wallet_address)
        self._open_keys: Set[Tuple[str, str]] = set()

        # tx_hash / trade_id dedup
        self._seen_ids: Set[str] = set()

        # wallet_address_lower → (profit_factor, win_rate)
        self._wallet_scores: Dict[str, Tuple[float, float]] = {}

        # WS event queue: (asset_id, price_str, tx_hash)
        self._event_queue: asyncio.Queue[Tuple[str, str, str]] = asyncio.Queue(maxsize=5000)

        # Set to trigger WS reconnect with refreshed subscription list
        self._resubscribe = asyncio.Event()

        self._conn: Optional[aiosqlite.Connection] = None
        self._http: Optional[httpx.AsyncClient] = None

    # ── Startup ───────────────────────────────────────────────────────────────

    async def _init_db(self) -> None:
        self._conn = await aiosqlite.connect(self._fast_db, timeout=30)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA busy_timeout=30000")
        for stmt in _SCHEMA.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                await self._conn.execute(stmt)
        await self._conn.commit()
        logger.info(f"Fast DB ready: {self._fast_db}")

    async def _load_scanner_data(self) -> None:
        """Load wallet scores and build asset_id map from the main scanner DB."""
        cutoff = (datetime.utcnow() - timedelta(days=self._lookback_days)).isoformat()

        async with aiosqlite.connect(self._scanner_db, timeout=30) as db:
            # Tracked wallet scores
            rows = await db.execute_fetchall(
                "SELECT wallet_address, profit_factor, win_rate "
                "FROM wallets WHERE is_tracked=1"
            )
            if not rows:
                logger.warning("No tracked wallets found in scanner DB — check is_tracked column")
                return

            self._wallet_scores = {
                r[0].lower(): (float(r[1] or 1.0), float(r[2] or 0.5))
                for r in rows
            }

            # Build asset_id → (wallet, market_id) from recent wallet_trades
            addrs = list(self._wallet_scores.keys())
            placeholders = ",".join("?" * len(addrs))
            rows = await db.execute_fetchall(
                f"""
                SELECT DISTINCT asset_id, market_id, wallet_address
                FROM wallet_trades
                WHERE wallet_address IN ({placeholders})
                  AND matched_at > ?
                  AND asset_id IS NOT NULL
                  AND market_id IS NOT NULL
                """,
                addrs + [cutoff],
            )

            new_map: Dict[str, Tuple[str, str]] = {}
            for asset_id, market_id, wallet in rows:
                if asset_id and market_id:
                    new_map[asset_id] = (wallet.lower(), market_id)

            self._asset_map = new_map

        logger.info(
            f"Scanner data loaded: {len(self._wallet_scores)} tracked wallets, "
            f"{len(self._asset_map)} asset IDs (last {self._lookback_days}d)"
        )

    async def _load_open_keys(self) -> None:
        rows = await self._conn.execute_fetchall(
            "SELECT market_id, wallet_followed FROM fast_trades WHERE status='OPEN'"
        )
        self._open_keys = {(r[0], r[1]) for r in rows}
        logger.info(f"{len(self._open_keys)} open positions loaded from fast DB")

    # ── Entry point ───────────────────────────────────────────────────────────

    async def run(self) -> None:
        logger.info(
            f"FastCopier starting — max_entry={self._max_entry} "
            f"bankroll=${float(self._bankroll):,.2f} "
            f"kelly={self._kelly_frac} max_pos={self._max_pos_pct:.2%}"
        )
        await self._init_db()
        await self._load_scanner_data()
        await self._load_open_keys()

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=5.0),
            headers={"User-Agent": "fast-copier/1.0"},
        ) as http:
            self._http = http
            await asyncio.gather(
                self._ws_worker(),
                self._trade_processor(),
                self._activity_poller(),
                self._position_closer(),
                self._asset_refresher(),
                self._snapshot_worker(),
            )

    # ── WebSocket worker ──────────────────────────────────────────────────────

    async def _ws_worker(self) -> None:
        """
        Subscribe to all known tracked asset_ids on the CLOB WebSocket.
        On each BUY last_trade_price event at ≤ max_entry for a tracked
        asset_id, push to the event queue with no REST call.
        """
        import websockets  # deferred import

        delay = 1.0
        events_60s = 0
        matches_60s = 0
        next_log = asyncio.get_event_loop().time() + 60

        while True:
            asset_ids = list(self._asset_map.keys())
            if not asset_ids:
                logger.warning("FastCopier WS: no asset IDs — waiting 30s")
                await asyncio.sleep(30)
                continue

            sub_msg = json.dumps({"assets_ids": asset_ids, "type": "market"})
            logger.info(f"FastCopier WS: connecting ({len(asset_ids)} subscriptions)")

            try:
                async with websockets.connect(
                    _WS_URL,
                    ping_interval=20,
                    ping_timeout=10,
                    open_timeout=15,
                ) as ws:
                    await ws.send(sub_msg)
                    self._resubscribe.clear()
                    delay = 1.0
                    logger.info("FastCopier WS: connected")

                    while not self._resubscribe.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                        except asyncio.TimeoutError:
                            now = asyncio.get_event_loop().time()
                            if now >= next_log:
                                logger.info(
                                    f"FastCopier WS: {events_60s} events, "
                                    f"{matches_60s} tracked BUYs ≤{self._max_entry} in 60s"
                                )
                                events_60s = matches_60s = 0
                                next_log = now + 60
                            continue

                        try:
                            data = json.loads(raw)
                        except json.JSONDecodeError:
                            continue

                        events = data if isinstance(data, list) else [data]
                        events_60s += len(events)

                        for event in events:
                            if event.get("event_type") != "last_trade_price":
                                continue
                            if event.get("side", "").upper() != "BUY":
                                continue

                            asset_id = event.get("asset_id", "")
                            price_str = event.get("price", "0")
                            tx_hash = event.get("transaction_hash", "")

                            if not asset_id or asset_id not in self._asset_map:
                                continue

                            try:
                                price = float(price_str)
                            except (ValueError, TypeError):
                                continue

                            if price <= 0 or price > self._max_entry:
                                continue

                            matches_60s += 1
                            try:
                                self._event_queue.put_nowait((asset_id, price_str, tx_hash))
                            except asyncio.QueueFull:
                                logger.warning("FastCopier: event queue full — dropping")

                    logger.info("FastCopier WS: resubscribing with updated asset list")

            except Exception as exc:
                logger.warning(f"FastCopier WS error: {exc!r} — reconnect in {delay:.0f}s")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)

    # ── Trade processor ───────────────────────────────────────────────────────

    async def _trade_processor(self) -> None:
        logger.info("FastCopier trade processor started")
        while True:
            try:
                asset_id, price_str, tx_hash = await asyncio.wait_for(
                    self._event_queue.get(), timeout=5.0
                )
            except asyncio.TimeoutError:
                continue
            try:
                await self._open_trade(asset_id, price_str, tx_hash, source="ws")
            except Exception as exc:
                logger.error(f"FastCopier processor error: {exc}")
            finally:
                self._event_queue.task_done()

    async def _open_trade(
        self,
        asset_id: str,
        price_str: str,
        tx_hash: str,
        source: str = "ws",
    ) -> bool:
        """
        Open a paper trade for a qualifying event.
        Returns True if a trade was opened, False if skipped.
        """
        # Dedup by tx_hash
        if tx_hash and tx_hash in self._seen_ids:
            return False

        lookup = self._asset_map.get(asset_id)
        if not lookup:
            return False
        wallet, market_id = lookup

        # Dedup: already open for this (market, wallet)
        key = (market_id, wallet)
        if key in self._open_keys:
            return False

        try:
            price = float(price_str)
        except (ValueError, TypeError):
            return False

        if price <= 0 or price > self._max_entry:
            return False

        if tx_hash:
            self._seen_ids.add(tx_hash)

        pf, win_rate = self._wallet_scores.get(wallet, (1.0, 0.5))

        # Quarter Kelly: f* = (w*(1-p) - (1-w)*p) / (1-p), then × kelly_frac
        full_kelly = (win_rate * (1.0 - price) - (1.0 - win_rate) * price) / (1.0 - price)
        kelly = max(0.0, full_kelly) * self._kelly_frac

        bankroll_f = float(self._bankroll)
        dollar_amount = min(bankroll_f * kelly, bankroll_f * self._max_pos_pct)
        dollar_amount = max(dollar_amount, 1.0)
        shares = dollar_amount / price

        opened_at = datetime.utcnow().isoformat()
        notes = f"src={source} ws_price={price:.4f} pf={pf:.2f} wr={win_rate:.2%} kelly={kelly:.4f}"

        try:
            cur = await self._conn.execute(
                """
                INSERT OR IGNORE INTO fast_trades
                  (market_id, asset_id, wallet_followed, entry_price, shares,
                   dollar_amount, kelly_fraction, profit_factor, win_rate,
                   status, opened_at, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?)
                """,
                (
                    market_id, asset_id, wallet, price, shares,
                    dollar_amount, kelly, pf, win_rate, opened_at, notes,
                ),
            )
            await self._conn.commit()

            if cur.rowcount == 0:
                return False  # UNIQUE constraint — already exists

            self._open_keys.add(key)
            logger.info(
                f"FAST COPY [{source}]: wallet={wallet[:10]}… "
                f"market={market_id[:16]}… "
                f"price={price:.4f} size=${dollar_amount:.2f} "
                f"pf={pf:.1f}x wr={win_rate:.1%}"
            )
            return True

        except Exception as exc:
            logger.error(f"FastCopier DB insert error: {exc}")
            return False

    # ── Activity poller (fallback for brand-new markets) ──────────────────────

    async def _activity_poller(self) -> None:
        """
        Poll each tracked wallet's /activity endpoint every 5 seconds.

        Catches trades in markets the WS hasn't subscribed to yet (new market,
        asset_id not yet in the map). When a new BUY is found at ≤ max_entry:
          1. Adds asset_id to the map and triggers WS resubscription.
          2. Opens a paper trade immediately via _open_trade().

        Latency: ~5-10s for brand-new markets vs ~50ms for known markets via WS.
        """
        logger.info("FastCopier activity poller started (5s interval, parallel)")
        last_seen: Dict[str, datetime] = {}
        LOOKBACK = timedelta(minutes=2)

        async def _poll_one(wallet: str) -> None:
            cutoff = last_seen.get(wallet, datetime.utcnow() - LOOKBACK)
            try:
                resp = await self._http.get(
                    f"{_DATA_BASE}/activity",
                    params={"user": wallet, "limit": 20},
                )
                if resp.status_code != 200:
                    return
                raw = resp.json()
                trades = raw if isinstance(raw, list) else raw.get("data", [])
            except Exception:
                return

            new_latest = cutoff
            for t in trades:
                # Parse timestamp (unix int)
                try:
                    ts = datetime.utcfromtimestamp(int(float(str(t.get("timestamp", 0)))))
                except (ValueError, TypeError, OSError):
                    continue

                if ts <= cutoff:
                    continue
                if ts > new_latest:
                    new_latest = ts

                side = str(t.get("side") or "").upper()
                if side not in ("BUY", "LONG"):
                    continue

                try:
                    price = float(t.get("price") or 0)
                except (ValueError, TypeError):
                    continue

                if price <= 0 or price > self._max_entry:
                    continue

                asset_id = str(t.get("asset") or "")
                market_id = str(t.get("conditionId") or "")
                tx_hash = str(t.get("transactionHash") or "")

                if not asset_id or not market_id:
                    continue

                # If this is a new asset_id, add to map and trigger resubscription
                if asset_id not in self._asset_map:
                    self._asset_map[asset_id] = (wallet, market_id)
                    self._resubscribe.set()
                    logger.info(
                        f"Activity poller: new asset {asset_id[:16]}… "
                        f"added for {wallet[:10]}… — WS resubscribe triggered"
                    )

                await self._open_trade(asset_id, str(price), tx_hash, source="poll")

            if new_latest > cutoff:
                last_seen[wallet] = new_latest

        while True:
            await asyncio.sleep(self._ACTIVITY_POLL_INTERVAL)
            try:
                wallets = list(self._wallet_scores.keys())
                await asyncio.gather(
                    *[_poll_one(w) for w in wallets],
                    return_exceptions=True,
                )
            except Exception as exc:
                logger.error(f"Activity poller error: {exc}")

    # ── Position closer ───────────────────────────────────────────────────────

    async def _position_closer(self) -> None:
        """
        Every 5 min: fetch CLOB asks for all open positions.
        ask ≥ 0.99 → CLOSED_WIN, ask ≤ 0.01 → CLOSED_LOSS.
        """
        logger.info("FastCopier position closer started (5-min interval)")

        async def _best_ask(asset_id: str) -> Tuple[str, Optional[float]]:
            try:
                resp = await self._http.get(
                    f"{_CLOB_BASE}/book",
                    params={"token_id": asset_id},
                )
                if resp.status_code == 200:
                    asks = resp.json().get("asks", [])
                    if asks:
                        best = min(asks, key=lambda x: float(x.get("price", 999)))
                        return asset_id, float(best["price"])
                # Fallback: last-trade-price
                resp2 = await self._http.get(
                    f"{_CLOB_BASE}/last-trade-price",
                    params={"token_id": asset_id},
                )
                if resp2.status_code == 200:
                    p = resp2.json().get("price")
                    if p:
                        return asset_id, float(p)
            except Exception:
                pass
            return asset_id, None

        while True:
            await asyncio.sleep(self._POSITION_CHECK_INTERVAL)
            try:
                rows = await self._conn.execute_fetchall(
                    "SELECT id, asset_id, entry_price, shares, dollar_amount, "
                    "market_id, wallet_followed FROM fast_trades WHERE status='OPEN'"
                )
                if not rows:
                    continue

                unique_assets = list({r[1] for r in rows if r[1]})
                results = await asyncio.gather(*[_best_ask(a) for a in unique_assets])
                ask_map: Dict[str, Optional[float]] = dict(results)

                wins = losses = 0
                for trade_id, asset_id, entry_price, shares, dollar_amount, market_id, wallet in rows:
                    ask = ask_map.get(asset_id)
                    if ask is None:
                        continue

                    ask_d = Decimal(str(ask))
                    now = datetime.utcnow().isoformat()

                    if ask_d >= self._WIN_THRESHOLD:
                        exit_p = float(self._WIN_THRESHOLD)
                        pnl = shares * exit_p - dollar_amount
                        await self._conn.execute(
                            "UPDATE fast_trades SET status='CLOSED_WIN', "
                            "exit_price=?, pnl=?, closed_at=? WHERE id=?",
                            (exit_p, pnl, now, trade_id),
                        )
                        self._open_keys.discard((market_id, wallet))
                        wins += 1
                        logger.info(
                            f"FAST WIN #{trade_id}: entry={entry_price:.4f} "
                            f"exit=0.99 pnl=+${pnl:.2f}"
                        )
                    elif ask_d <= self._LOSS_THRESHOLD:
                        exit_p = float(self._LOSS_THRESHOLD)
                        pnl = shares * exit_p - dollar_amount
                        await self._conn.execute(
                            "UPDATE fast_trades SET status='CLOSED_LOSS', "
                            "exit_price=?, pnl=?, closed_at=? WHERE id=?",
                            (exit_p, pnl, now, trade_id),
                        )
                        self._open_keys.discard((market_id, wallet))
                        losses += 1
                        logger.info(
                            f"FAST LOSS #{trade_id}: entry={entry_price:.4f} "
                            f"exit=0.01 pnl=${pnl:.2f}"
                        )

                if wins or losses:
                    await self._conn.commit()
                    logger.info(
                        f"Position closer: {wins} wins, {losses} losses closed this cycle"
                    )
            except Exception as exc:
                logger.error(f"Position closer error: {exc}")

    # ── Asset refresher ───────────────────────────────────────────────────────

    async def _asset_refresher(self) -> None:
        """
        Every 5 min: reload the asset_id map and wallet scores from scanner DB.
        Triggers WS resubscription whenever the map grows.
        """
        logger.info("FastCopier asset refresher started (5-min interval)")
        while True:
            await asyncio.sleep(self._ASSET_REFRESH_INTERVAL)
            try:
                old_count = len(self._asset_map)
                await self._load_scanner_data()
                new_count = len(self._asset_map)
                if new_count != old_count:
                    logger.info(
                        f"Asset map updated: {old_count} → {new_count} IDs — resubscribing WS"
                    )
                    self._resubscribe.set()
                # Reload open keys to stay in sync
                await self._load_open_keys()
                # Trim seen_ids to prevent unbounded growth
                if len(self._seen_ids) > 50_000:
                    self._seen_ids.clear()
            except Exception as exc:
                logger.error(f"Asset refresher error: {exc}")

    # ── Snapshot / reporting ──────────────────────────────────────────────────

    async def _snapshot_worker(self) -> None:
        """Log portfolio stats and write snapshot every 30 min."""
        while True:
            await asyncio.sleep(self._SNAPSHOT_INTERVAL)
            try:
                rows = await self._conn.execute_fetchall(
                    """
                    SELECT
                        SUM(CASE WHEN status='OPEN'        THEN 1 ELSE 0 END),
                        SUM(CASE WHEN status='CLOSED_WIN'  THEN 1 ELSE 0 END),
                        SUM(CASE WHEN status='CLOSED_LOSS' THEN 1 ELSE 0 END),
                        COALESCE(SUM(CASE WHEN status IN ('CLOSED_WIN','CLOSED_LOSS')
                                         THEN pnl ELSE 0 END), 0)
                    FROM fast_trades
                    """
                )
                if rows:
                    open_count, wins, losses, realized = rows[0]
                    open_count = open_count or 0
                    wins = wins or 0
                    losses = losses or 0
                    realized = float(realized or 0)
                    bankroll = float(self._bankroll) + realized
                    total = wins + losses
                    wr = wins / total if total else 0.0

                    logger.info(
                        f"FastCopier snapshot — bankroll=${bankroll:.2f} "
                        f"realized=${realized:+.2f} "
                        f"open={open_count} wins={wins} losses={losses} "
                        f"win_rate={wr:.1%}"
                    )
                    await self._conn.execute(
                        "INSERT INTO fast_snapshots "
                        "(bankroll, realized_pnl, open_count, closed_wins, closed_losses, snapped_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (bankroll, realized, open_count, wins, losses, datetime.utcnow().isoformat()),
                    )
                    await self._conn.commit()
            except Exception as exc:
                logger.error(f"Snapshot error: {exc}")
