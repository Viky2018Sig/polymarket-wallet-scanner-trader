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
import os
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

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
    order_id        TEXT,
    peak_price      REAL DEFAULT 0,
    tp_order_id     TEXT,
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

    _POSITION_CHECK_INTERVAL = 5 * 60   # seconds — WIN/LOSS resolution check
    _PROFIT_LOCK_INTERVAL = 10           # seconds — trailing stop price poll
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
        live_mode: bool = False,
        live_env_file: str = "",
        live_max_bet: float = 2.0,
        live_slippage: float = 0.005,
        profit_lock_at: float = 2.0,
        profit_lock_trail: float = 0.50,
    ) -> None:
        self._scanner_db = scanner_db_path
        self._fast_db = fast_db_path
        self._max_entry = max_entry_price
        self._bankroll = Decimal(str(starting_bankroll))
        self._max_pos_pct = max_position_pct
        self._kelly_frac = kelly_fraction
        self._lookback_days = lookback_days
        self._live_mode = live_mode
        self._live_env_file = live_env_file
        self._live_max_bet = live_max_bet
        self._live_slippage = live_slippage
        self._profit_lock_at = profit_lock_at    # activate trailing stop at N× entry
        self._profit_lock_trail = profit_lock_trail  # sell when bid falls to this fraction of peak
        self._clob_client: Any = None

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
        # Additive migrations for existing DBs
        for col_def in (
            "order_id TEXT",
            "peak_price REAL DEFAULT 0",
            "tp_order_id TEXT",
        ):
            try:
                await self._conn.execute(f"ALTER TABLE fast_trades ADD COLUMN {col_def}")
                await self._conn.commit()
            except Exception:
                pass  # column already exists
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

    # ── Live order helpers ────────────────────────────────────────────────────

    def _build_live_client(self) -> Any:
        """Build py-clob-client-v2 ClobClient from live_env_file."""
        try:
            from py_clob_client_v2.client import ClobClient
            from py_clob_client_v2.clob_types import ApiCreds
        except ImportError:
            raise RuntimeError(
                "py-clob-client-v2 not installed. Run: pip install py-clob-client-v2"
            )
        env: Dict[str, str] = {}
        env_path = Path(self._live_env_file)
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line and "=" in line and not line.startswith("#"):
                    k, _, v = line.partition("=")
                    env[k.strip()] = v.strip()
        for key in ("PK", "PROXY_WALLET", "CLOB_API_KEY", "CLOB_SECRET", "CLOB_PASSPHRASE", "SIG_TYPE"):
            val = os.environ.get(key)
            if val:
                env[key] = val
        pk = env.get("PK", "")
        if not pk:
            raise RuntimeError(f"PK not found in {self._live_env_file}")
        sig_type = int(env.get("SIG_TYPE", "0"))
        creds = ApiCreds(
            api_key=env.get("CLOB_API_KEY", ""),
            api_secret=env.get("CLOB_SECRET", ""),
            api_passphrase=env.get("CLOB_PASSPHRASE", ""),
        )
        kwargs: Dict[str, Any] = dict(chain_id=137, key=pk, creds=creds, signature_type=sig_type)
        proxy_wallet = env.get("PROXY_WALLET", "")
        if sig_type in (1, 2, 3) and proxy_wallet:
            kwargs["funder"] = proxy_wallet
        return ClobClient("https://clob.polymarket.com", **kwargs)

    async def _place_live_order(self, asset_id: str, price: float, dollar_amount: float) -> str:
        """
        Place a BUY order on the CLOB via Brazil proxy.
        Strategy: try FOK first (instant fill); if killed due to thin liquidity,
        fall back to GTC limit order (placed on the book at our price).
        Returns order_id or '' on failure.
        """
        size = round(min(dollar_amount, self._live_max_bet), 2)
        if size < 1.0:
            logger.debug(f"Live order skipped: size ${size:.2f} < $1 minimum")
            return ""
        limit_price = round(min(price * (1.0 + self._live_slippage), 0.99), 4)
        shares = round(size / limit_price, 4)

        def _fok_sync() -> Dict[str, Any]:
            if self._clob_client is None:
                self._clob_client = self._build_live_client()
            from py_clob_client_v2.clob_types import MarketOrderArgsV2, OrderType
            order_args = MarketOrderArgsV2(
                token_id=asset_id,
                amount=size,
                side="BUY",
                price=limit_price,
                order_type=OrderType.FOK,
            )
            return self._clob_client.create_and_post_market_order(
                order_args, order_type=OrderType.FOK
            )

        def _gtc_sync() -> Dict[str, Any]:
            if self._clob_client is None:
                self._clob_client = self._build_live_client()
            from py_clob_client_v2.clob_types import OrderArgsV2, OrderType
            order_args = OrderArgsV2(
                token_id=asset_id,
                price=limit_price,
                size=shares,
                side="BUY",
            )
            return self._clob_client.create_and_post_order(order_args, order_type=OrderType.GTC)

        # ── 1. Try FOK (instant fill) ─────────────────────────────────────────
        try:
            resp = await asyncio.to_thread(_fok_sync)
            order_id = resp.get("orderID", resp.get("id", "")) if isinstance(resp, dict) else ""
            logger.info(
                f"LIVE BUY (FOK): asset={asset_id[:16]}… price={limit_price:.4f} "
                f"size=${size:.2f} order_id={order_id[:16] if order_id else '?'}"
            )
            return order_id or ""
        except Exception as fok_exc:
            fok_msg = str(fok_exc).lower()
            if "fok" not in fok_msg and "fully filled" not in fok_msg:
                logger.error(f"Live FOK order error: asset={asset_id[:16]}… {fok_exc}")
                self._clob_client = None
                return ""
            logger.warning(
                f"FOK killed (thin liquidity) → GTC fallback: "
                f"asset={asset_id[:16]}… price={limit_price:.4f} size=${size:.2f}"
            )

        # ── 2. FOK was killed — place GTC limit order on the book ─────────────
        try:
            resp = await asyncio.to_thread(_gtc_sync)
            order_id = resp.get("orderID", resp.get("id", "")) if isinstance(resp, dict) else ""
            logger.info(
                f"LIVE BUY (GTC): asset={asset_id[:16]}… price={limit_price:.4f} "
                f"shares={shares} order_id={order_id[:16] if order_id else '?'}"
            )
            return order_id or ""
        except Exception as gtc_exc:
            logger.error(f"Live GTC order failed: asset={asset_id[:16]}… {gtc_exc}")
            self._clob_client = None
            return ""

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
                self._profit_lock_worker(),
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

            if self._live_mode:
                order_id = await self._place_live_order(asset_id, price, dollar_amount)
                if order_id:
                    await self._conn.execute(
                        "UPDATE fast_trades SET order_id=? "
                        "WHERE market_id=? AND wallet_followed=?",
                        (order_id, market_id, wallet),
                    )
                    await self._conn.commit()

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

    # ── Live sell helper ──────────────────────────────────────────────────────

    async def _place_live_sell(self, asset_id: str, shares: float, bid: float) -> str:
        """Place a GTC SELL order to lock in profit. Returns order_id or '' on failure."""
        limit_price = round(max(bid * (1.0 - self._live_slippage), 0.01), 4)
        shares_r = round(shares, 4)

        def _sell_sync() -> Dict[str, Any]:
            if self._clob_client is None:
                self._clob_client = self._build_live_client()
            from py_clob_client_v2.clob_types import OrderArgsV2, OrderType
            order_args = OrderArgsV2(
                token_id=asset_id,
                price=limit_price,
                size=shares_r,
                side="SELL",
            )
            return self._clob_client.create_and_post_order(order_args, order_type=OrderType.GTC)

        try:
            resp = await asyncio.to_thread(_sell_sync)
            order_id = resp.get("orderID", resp.get("id", "")) if isinstance(resp, dict) else ""
            logger.info(
                f"LIVE SELL (profit-lock): asset={asset_id[:16]}… "
                f"shares={shares_r} price={limit_price:.4f} "
                f"order_id={order_id[:16] if order_id else '?'}"
            )
            return order_id or ""
        except Exception as exc:
            logger.error(f"Live SELL order error: asset={asset_id[:16]}… {exc}")
            self._clob_client = None
            return ""

    # ── Price helper (shared by closer + profit-lock) ─────────────────────────

    async def _fetch_prices(
        self, asset_ids: List[str]
    ) -> Dict[str, Tuple[Optional[float], Optional[float]]]:
        """Fetch (bid, ask) for each asset_id concurrently."""

        async def _one(asset_id: str) -> Tuple[str, Optional[float], Optional[float]]:
            try:
                resp = await self._http.get(
                    f"{_CLOB_BASE}/book", params={"token_id": asset_id}
                )
                if resp.status_code == 200:
                    book = resp.json()
                    asks = book.get("asks", [])
                    bids = book.get("bids", [])
                    best_ask = (
                        float(min(asks, key=lambda x: float(x.get("price", 999)))["price"])
                        if asks else None
                    )
                    best_bid = (
                        float(max(bids, key=lambda x: float(x.get("price", 0)))["price"])
                        if bids else None
                    )
                    if best_ask is not None or best_bid is not None:
                        return asset_id, best_bid, best_ask
                # Fallback: last-trade-price as mid
                resp2 = await self._http.get(
                    f"{_CLOB_BASE}/last-trade-price", params={"token_id": asset_id}
                )
                if resp2.status_code == 200:
                    p = resp2.json().get("price")
                    if p:
                        mid = float(p)
                        return asset_id, mid, mid
            except Exception:
                pass
            return asset_id, None, None

        results = await asyncio.gather(*[_one(a) for a in asset_ids])
        return {r[0]: (r[1], r[2]) for r in results}

    # ── Position closer (WIN / LOSS at resolution) ────────────────────────────

    async def _position_closer(self) -> None:
        """
        Every 5 min: check ask price for all open positions.
          ask ≥ 0.99 → CLOSED_WIN
          ask ≤ 0.01 → CLOSED_LOSS
        """
        logger.info("FastCopier position closer started (5-min interval)")
        while True:
            await asyncio.sleep(self._POSITION_CHECK_INTERVAL)
            try:
                rows = await self._conn.execute_fetchall(
                    "SELECT id, asset_id, entry_price, shares, dollar_amount, "
                    "market_id, wallet_followed "
                    "FROM fast_trades WHERE status='OPEN'"
                )
                if not rows:
                    continue

                unique_assets = list({r[1] for r in rows if r[1]})
                price_map = await self._fetch_prices(unique_assets)

                wins = losses = 0
                for trade_id, asset_id, entry_price, shares, dollar_amount, market_id, wallet in rows:
                    _, ask = price_map.get(asset_id, (None, None))
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

    # ── Trailing profit-lock (10-second poll) ─────────────────────────────────

    async def _profit_lock_worker(self) -> None:
        """
        Every 10 s: track each position's peak bid price.
        When bid peaked at ≥ profit_lock_at × entry AND current bid drops to
        ≤ peak × profit_lock_trail → sell immediately (CLOSED_PROFIT_LOCK).

        Example (defaults): bought at 0.10
          → peak rises to 0.50 (5×) — trailing stop activates at 0.20 (2×)
          → price falls back to 0.25 (50% of 0.50) → SELL, lock in 2.5× profit
        """
        if self._profit_lock_at <= 0:
            logger.info("Profit-lock worker disabled (profit_lock_at=0)")
            return

        logger.info(
            f"Profit-lock worker started (10s poll, "
            f"activate≥{self._profit_lock_at}× entry, "
            f"trail@{self._profit_lock_trail:.0%} of peak)"
        )
        while True:
            await asyncio.sleep(self._PROFIT_LOCK_INTERVAL)
            try:
                rows = await self._conn.execute_fetchall(
                    "SELECT id, asset_id, entry_price, shares, dollar_amount, "
                    "market_id, wallet_followed, COALESCE(peak_price, 0) "
                    "FROM fast_trades WHERE status='OPEN'"
                )
                if not rows:
                    continue

                unique_assets = list({r[1] for r in rows if r[1]})
                price_map = await self._fetch_prices(unique_assets)

                profit_locks = 0
                for (trade_id, asset_id, entry_price, shares,
                     dollar_amount, market_id, wallet, peak_price) in rows:
                    bid, ask = price_map.get(asset_id, (None, None))

                    # Use bid for value; fall back to ask if no bid quoted
                    current = bid if bid is not None else ask
                    if current is None:
                        continue

                    # Skip prices that already indicate resolution (handled by closer)
                    if current >= float(self._WIN_THRESHOLD) or current <= float(self._LOSS_THRESHOLD):
                        continue

                    # Update peak
                    new_peak = max(float(peak_price), current)
                    if new_peak > float(peak_price):
                        await self._conn.execute(
                            "UPDATE fast_trades SET peak_price=? WHERE id=?",
                            (new_peak, trade_id),
                        )

                    # Fire trailing stop
                    if (
                        new_peak >= entry_price * self._profit_lock_at
                        and current <= new_peak * self._profit_lock_trail
                    ):
                        exit_p = current
                        pnl = shares * exit_p - dollar_amount
                        tp_order_id = ""
                        if self._live_mode:
                            tp_order_id = await self._place_live_sell(
                                asset_id, shares, current
                            )
                        now = datetime.utcnow().isoformat()
                        await self._conn.execute(
                            "UPDATE fast_trades SET status='CLOSED_PROFIT_LOCK', "
                            "exit_price=?, pnl=?, closed_at=?, tp_order_id=? WHERE id=?",
                            (exit_p, pnl, now, tp_order_id, trade_id),
                        )
                        self._open_keys.discard((market_id, wallet))
                        profit_locks += 1
                        logger.info(
                            f"PROFIT LOCK #{trade_id}: entry={entry_price:.4f} "
                            f"peak={new_peak:.4f} ({new_peak/entry_price:.1f}x) "
                            f"exit={exit_p:.4f} ({current/new_peak:.0%} of peak) "
                            f"pnl={pnl:+.2f}"
                        )

                if profit_locks:
                    await self._conn.commit()
                    logger.info(f"Profit-lock: {profit_locks} positions exited this cycle")
            except Exception as exc:
                logger.error(f"Profit-lock worker error: {exc}")

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
