"""
Five-Minute Crypto Straddle Paper Trader
========================================

Strategy:
  For each active 5-minute BTC/ETH/crypto market, place paper buy orders on
  BOTH sides (Up/Down or Yes/No) at <= --max-price.

  Fill trigger: last_trade_price for a side drops to <= max_price.

  At resolution (one side → 0.99, other → 0.01):
    If BOTH sides filled at p_win and p_loss:
      Net PnL = bet*(1/p_win - 1) - bet  (always positive when p_win < 0.5)
    If ONE side filled at price p:
      Win:  pnl = bet*(1/p - 1)
      Loss: pnl = -bet

Usage:
  python five_min_paper.py --bankroll 500 --max-price 0.20 --bet-size 5
  python five_min_paper.py --max-price 0.15 --bet-size 2 --log-level DEBUG
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Dict, List, Optional, Set, Tuple

import aiosqlite
import httpx
from loguru import logger

_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
_CLOB_BASE = "https://clob.polymarket.com"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS straddle_markets (
    market_id   TEXT PRIMARY KEY,
    question    TEXT NOT NULL,
    asset_yes   TEXT NOT NULL,
    asset_no    TEXT NOT NULL,
    end_time    TEXT NOT NULL,
    discovered_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS straddle_trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id     TEXT NOT NULL,
    question      TEXT NOT NULL,
    side          TEXT NOT NULL,           -- YES or NO
    asset_id      TEXT NOT NULL,
    fill_price    REAL NOT NULL,
    shares        REAL NOT NULL,
    bet_usd       REAL NOT NULL,
    status        TEXT NOT NULL DEFAULT 'OPEN',   -- OPEN | WIN | LOSS
    pnl           REAL,
    filled_at     TEXT NOT NULL,
    closed_at     TEXT,
    notes         TEXT
);

CREATE TABLE IF NOT EXISTS straddle_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    total_markets   INTEGER,
    open_positions  INTEGER,
    both_filled     INTEGER,
    single_filled   INTEGER,
    closed_wins     INTEGER,
    closed_losses   INTEGER,
    realized_pnl    REAL,
    snapped_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_WIN_THRESHOLD  = 0.99
_LOSS_THRESHOLD = 0.01
_EXIT_MULTIPLIER       = 5.0   # take profit when price reaches N× entry
_MARKET_POLL_INTERVAL  = 60    # seconds between Data API polls for new markets
_SNAPSHOT_INTERVAL     = 30 * 60
_TELEGRAM_INTERVAL     = 60 * 60  # 1 hour
_POSITION_CHECK_SECS   = 60    # how often to verify resolution via CLOB REST
_BOOK_CHECK_BATCH      = 20    # positions to check per REST cycle


import re


def _parse_end_time_from_slug(slug: str) -> str:
    """
    Extract market end time from slugs like 'eth-updown-5m-1778937300'.
    Returns ISO UTC string or empty string if unparseable.
    """
    m = re.search(r"-(\d+)m-(\d+)$", slug)
    if m:
        duration_min = int(m.group(1))
        start_ts    = int(m.group(2))
        end_ts      = start_ts + duration_min * 60
        return datetime.fromtimestamp(end_ts, tz=timezone.utc).isoformat()
    return ""


def _is_five_min_up_down(question: str, slug: str = "") -> bool:
    """
    Returns True for 5-minute BTC/ETH/SOL/XRP/DOGE Up-or-Down markets.
    Uses slug ('-5m-') as the primary signal; falls back to question text.
    """
    crypto = any(
        t in question.lower()
        for t in ("btc", "eth", "sol", "xrp", "bnb", "doge",
                  "bitcoin", "ethereum", "solana", "dogecoin", "ripple")
    )
    if not crypto:
        return False
    if slug:
        return bool(re.search(r"-5m-\d+$", slug))
    return "up or down" in question.lower()


def _load_telegram_creds() -> Tuple[str, str]:
    """Read TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from the project .env."""
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    token, chat_id = "", ""
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("TELEGRAM_BOT_TOKEN="):
                    token = line.split("=", 1)[1].strip()
                elif line.startswith("TELEGRAM_CHAT_ID="):
                    chat_id = line.split("=", 1)[1].strip()
    except FileNotFoundError:
        pass
    return token, chat_id


def _now_utc() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def _is_five_min_crypto(question: str) -> bool:
    """Heuristic: question mentions a crypto ticker and a 5-minute window."""
    q = question.lower()
    crypto = any(t in q for t in ("btc", "eth", "sol", "xrp", "bnb", "doge", "bitcoin", "ethereum"))
    five_min = any(t in q for t in ("5 min", "5-min", "5m ", " 5m", "five min"))
    return crypto and five_min


class StraddlePaperTrader:
    """
    Paper-trades a bilateral straddle on 5-minute crypto markets.

    Per market:
      • Subscribes to both YES and NO CLOB token IDs via WebSocket.
      • Fills the YES paper order when YES last_trade_price <= max_price.
      • Fills the NO  paper order when NO  last_trade_price <= max_price.
      • At resolution, records WIN/LOSS for each filled side.
    """

    def __init__(
        self,
        db_path: str,
        max_price: float,
        bet_usd: float,
        bankroll: float = 500.0,
        tg_token: str = "",
        tg_chat_id: str = "",
    ) -> None:
        self._db_path    = db_path
        self._max_price  = max_price
        self._bet_usd    = Decimal(str(bet_usd))
        self._bankroll   = bankroll
        self._tg_token   = tg_token
        self._tg_chat_id = tg_chat_id

        # market_id → {asset_yes, asset_no, end_time, question}
        self._markets: Dict[str, dict] = {}

        # asset_id → (market_id, side)   side = "YES" | "NO"
        self._asset_to_side: Dict[str, Tuple[str, str]] = {}

        # (market_id, side) → trade row id  — filled positions
        self._filled: Dict[Tuple[str, str], int] = {}

        # asset_id → (trade_id, entry_price, shares, bet_usd) — for real-time 2x exit
        self._open_pos: Dict[str, Tuple[int, float, float, float]] = {}

        # (market_id, side) already inserted to DB (don't double-fill)
        self._fill_keys: Set[Tuple[str, str]] = set()

        # markets already resolved (don't process again)
        self._resolved: Set[str] = set()

        self._resubscribe = asyncio.Event()
        self._conn: Optional[aiosqlite.Connection] = None
        self._http: Optional[httpx.AsyncClient] = None

    # ── Database ──────────────────────────────────────────────────────────────

    async def _init_db(self) -> None:
        self._conn = await aiosqlite.connect(self._db_path, timeout=30)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA busy_timeout=30000")
        for stmt in _SCHEMA.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                await self._conn.execute(stmt)
        await self._conn.commit()

        # Re-hydrate open positions from previous run
        async with self._conn.execute(
            "SELECT id, market_id, side, asset_id, fill_price, shares, bet_usd "
            "FROM straddle_trades WHERE status='OPEN'"
        ) as cur:
            async for row in cur:
                tid, mid, side, asset_id, fill_price, shares, bet_usd = row
                self._filled[(mid, side)] = tid
                self._fill_keys.add((mid, side))
                self._open_pos[asset_id] = (tid, fill_price, shares, bet_usd)

        async with self._conn.execute(
            "SELECT market_id, asset_yes, asset_no, end_time, question FROM straddle_markets"
        ) as cur:
            async for row in cur:
                mid, ay, an, et, q = row
                self._markets[mid] = {
                    "asset_yes": ay, "asset_no": an,
                    "end_time": et, "question": q,
                }
                self._asset_to_side[ay] = (mid, "YES")
                self._asset_to_side[an] = (mid, "NO")

        logger.info(
            f"Resumed: {len(self._markets)} markets, "
            f"{len(self._filled)} open positions"
        )

    async def _record_fill(
        self, market_id: str, side: str, asset_id: str, price: float, question: str
    ) -> int:
        shares = float(self._bet_usd / Decimal(str(price)))
        async with self._conn.execute(
            """INSERT INTO straddle_trades
               (market_id, question, side, asset_id, fill_price, shares, bet_usd, filled_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (market_id, question, side, asset_id, price, shares,
             float(self._bet_usd), _now_utc()),
        ) as cur:
            trade_id = cur.lastrowid
        await self._conn.commit()
        self._open_pos[asset_id] = (trade_id, price, shares, float(self._bet_usd))
        return trade_id

    async def _close_trade(self, trade_id: int, status: str, exit_price: float) -> None:
        bet   = float(self._bet_usd)
        async with self._conn.execute(
            "SELECT fill_price, shares FROM straddle_trades WHERE id=?", (trade_id,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return
        fill_price, shares = row
        pnl = (shares * exit_price - bet) if status == "WIN" else -bet
        await self._conn.execute(
            """UPDATE straddle_trades
               SET status=?, pnl=?, closed_at=?, notes=?
               WHERE id=?""",
            (status, round(pnl, 4), _now_utc(),
             f"exit_price={exit_price:.4f}", trade_id),
        )
        await self._conn.commit()

    async def _save_market(self, mid: str, info: dict) -> None:
        await self._conn.execute(
            """INSERT OR IGNORE INTO straddle_markets
               (market_id, question, asset_yes, asset_no, end_time)
               VALUES (?,?,?,?,?)""",
            (mid, info["question"], info["asset_yes"], info["asset_no"], info["end_time"]),
        )
        await self._conn.commit()

    async def _snapshot(self) -> None:
        async with self._conn.execute(
            """SELECT
                 COUNT(DISTINCT market_id),
                 SUM(CASE WHEN status='OPEN'   THEN 1 ELSE 0 END),
                 SUM(CASE WHEN status IN ('WIN','WIN_2X') THEN 1 ELSE 0 END),
                 SUM(CASE WHEN status='WIN_2X' THEN 1 ELSE 0 END),
                 SUM(CASE WHEN status='LOSS'   THEN 1 ELSE 0 END),
                 ROUND(SUM(COALESCE(pnl,0)),2)
               FROM straddle_trades"""
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return
        total_markets, open_pos, wins, wins_2x, losses, realized = row
        total_markets = total_markets or 0
        open_pos      = open_pos      or 0
        wins          = wins          or 0
        wins_2x       = wins_2x       or 0
        losses        = losses        or 0
        realized      = realized      or 0.0

        async with self._conn.execute(
            """SELECT COUNT(*) FROM (
                 SELECT market_id FROM straddle_trades
                 WHERE status != 'OPEN'
                 GROUP BY market_id HAVING COUNT(DISTINCT side) = 2
               )"""
        ) as cur:
            both = (await cur.fetchone())[0] or 0
        logger.info(
            f"[Snapshot] markets={total_markets} open={open_pos} "
            f"both_filled={both} wins={wins}(2x={wins_2x}) losses={losses} "
            f"realized_pnl={realized:+.2f}"
        )
        await self._conn.execute(
            """INSERT INTO straddle_snapshots
               (total_markets,open_positions,both_filled,single_filled,
                closed_wins,closed_losses,realized_pnl)
               VALUES (?,?,?,?,?,?,?)""",
            (total_markets, open_pos, both, 0, wins, losses, realized),
        )
        await self._conn.commit()

    # ── Market discovery ──────────────────────────────────────────────────────

    async def _market_poller(self) -> None:
        """Poll Gamma API every 30s for new active 5-min markets."""
        while True:
            try:
                await self._discover_markets()
            except Exception as exc:
                logger.error(f"Market poller error: {exc}")
            await asyncio.sleep(_MARKET_POLL_INTERVAL)

    async def _discover_markets(self) -> None:
        """
        Finds active 5-min Up/Down markets from the Data API global trade stream.
        Polls the 200 most-recent trades, filters for '-5m-' slugs, then calls
        the CLOB API to get both token IDs for each new market found.
        No dependency on the scanner DB or wallet tracking.
        """
        try:
            r = await self._http.get(
                "https://data-api.polymarket.com/trades",
                params={"limit": 200},
                timeout=15,
            )
            if r.status_code != 200:
                logger.warning(f"Data API returned {r.status_code}")
                return
            trades = r.json()
            if not isinstance(trades, list):
                trades = trades.get("data", [])
        except Exception as exc:
            logger.warning(f"Data API discovery failed: {exc}")
            return

        seen: set = set()
        new_count = 0
        candidates = 0
        for t in trades:
            slug         = t.get("slug", "") or ""
            condition_id = t.get("conditionId", "") or ""
            title        = t.get("title", "") or ""
            if not re.search(r"-5m-", slug):
                continue
            if condition_id in seen:
                continue
            seen.add(condition_id)
            candidates += 1
            if not condition_id or condition_id in self._markets or condition_id in self._resolved:
                continue
            added = await self._clob_lookup(condition_id)
            if added:
                new_count += 1
                logger.info(f"New market: {title}")

        if new_count:
            logger.info(f"Discovered {new_count} new 5-min market(s) → resubscribing WS")
            self._resubscribe.set()
        else:
            logger.debug(f"Market poll: {candidates} 5-min candidates, 0 new")

    async def _clob_lookup(self, condition_id: str) -> bool:
        """
        Calls CLOB /markets/{condition_id}, validates it is a 5-min Up/Down
        market, extracts both token IDs, and registers the market.
        Returns True if a new market was added.
        """
        try:
            r = await self._http.get(
                f"{_CLOB_BASE}/markets/{condition_id}", timeout=8
            )
            if r.status_code != 200:
                return False
            data = r.json()
        except Exception as exc:
            logger.debug(f"CLOB lookup failed for {condition_id}: {exc}")
            return False

        if data.get("closed") or not data.get("accepting_orders", True):
            return False

        question = data.get("question", "")
        slug     = data.get("market_slug", "")

        if not _is_five_min_up_down(question, slug):
            return False

        tokens = data.get("tokens", [])
        asset_yes = asset_no = ""
        for t in tokens:
            if t.get("outcome") == "Up":
                asset_yes = str(t["token_id"])
            elif t.get("outcome") == "Down":
                asset_no = str(t["token_id"])

        if not asset_yes or not asset_no:
            return False

        end_time = _parse_end_time_from_slug(slug)

        info = {
            "asset_yes": asset_yes,
            "asset_no":  asset_no,
            "end_time":  end_time,
            "question":  question,
        }
        self._markets[condition_id] = info
        self._asset_to_side[asset_yes] = (condition_id, "YES")
        self._asset_to_side[asset_no]  = (condition_id, "NO")
        await self._save_market(condition_id, info)
        logger.info(f"Added: {question} | ends {end_time or 'unknown'}")
        return True

    # ── WebSocket ─────────────────────────────────────────────────────────────

    async def _ws_worker(self) -> None:
        """
        Subscribes to all tracked 5-min market token IDs.
        Fills paper orders when price <= max_price.
        Resolves markets when price reaches WIN/LOSS threshold.
        """
        import websockets  # deferred import

        delay = 1.0
        while True:
            asset_ids = list(self._asset_to_side.keys())
            if not asset_ids:
                logger.warning("WS: no asset IDs yet — waiting 30s for market discovery")
                await asyncio.sleep(30)
                continue

            sub_msg = json.dumps({"assets_ids": asset_ids, "type": "market"})
            logger.info(f"WS: connecting ({len(asset_ids)} subscriptions)")

            try:
                async with websockets.connect(
                    _WS_URL, ping_interval=20, ping_timeout=10, open_timeout=15
                ) as ws:
                    await ws.send(sub_msg)
                    self._resubscribe.clear()
                    delay = 1.0
                    logger.info("WS: connected")

                    while not self._resubscribe.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                        except asyncio.TimeoutError:
                            continue

                        try:
                            data = json.loads(raw)
                        except json.JSONDecodeError:
                            continue

                        events = data if isinstance(data, list) else [data]
                        for event in events:
                            await self._handle_event(event)

            except Exception as exc:
                logger.warning(f"WS error ({type(exc).__name__}: {exc}) — reconnecting in {delay}s")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    async def _handle_event(self, event: dict) -> None:
        if event.get("event_type") != "last_trade_price":
            return

        asset_id  = event.get("asset_id", "")
        price_str = event.get("price", "0")

        if asset_id not in self._asset_to_side:
            return

        try:
            price = float(price_str)
        except (ValueError, TypeError):
            return

        market_id, side = self._asset_to_side[asset_id]
        if market_id in self._resolved:
            return

        info = self._markets.get(market_id)
        if not info:
            return

        # ── Multiplier exit (first priority) ──────────────────────────────
        pos = self._open_pos.get(asset_id)
        if pos and price >= pos[1] * _EXIT_MULTIPLIER:
            trade_id, entry_price, shares, bet_usd = pos
            await self._exit_2x(
                trade_id, asset_id, market_id,
                entry_price, shares, bet_usd, price
            )
            return

        # ── Resolution detection ───────────────────────────────────────────
        if price >= _WIN_THRESHOLD:
            await self._resolve_market(market_id, winning_side=side)
            return
        if price <= _LOSS_THRESHOLD:
            other = "NO" if side == "YES" else "YES"
            await self._resolve_market(market_id, winning_side=other)
            return

        # ── Fill detection ─────────────────────────────────────────────────
        key = (market_id, side)
        if key in self._fill_keys:
            return  # already filled this side

        if price <= self._max_price:
            trade_id = await self._record_fill(
                market_id, side, asset_id, price, info["question"]
            )
            self._filled[key] = trade_id
            self._fill_keys.add(key)

            other_filled = (market_id, "NO" if side == "YES" else "YES") in self._fill_keys
            tag = "BOTH SIDES FILLED — guaranteed profit" if other_filled else "single side"
            logger.info(
                f"FILL {side} @ {price:.4f} | {info['question'][:60]} | {tag}"
            )

    async def _exit_2x(
        self,
        trade_id: int,
        asset_id: str,
        market_id: str,
        entry_price: float,
        shares: float,
        bet_usd: float,
        exit_price: float,
    ) -> None:
        """Close a position immediately when price reaches _EXIT_MULTIPLIER × entry."""
        pnl = round(shares * exit_price - bet_usd, 4)
        mult = round(exit_price / entry_price, 1)
        await self._conn.execute(
            """UPDATE straddle_trades
               SET status='WIN_2X', pnl=?, closed_at=?, notes=?
               WHERE id=?""",
            (pnl, _now_utc(), f"{mult}x_exit price={exit_price:.4f}", trade_id),
        )
        await self._conn.commit()
        self._open_pos.pop(asset_id, None)
        for key, tid in list(self._filled.items()):
            if tid == trade_id:
                del self._filled[key]
                break
        logger.info(
            f"{mult}X EXIT @ {exit_price:.4f} (entry={entry_price:.4f}) "
            f"pnl={pnl:+.2f} | {market_id[:16]}…"
        )

    async def _resolve_market(self, market_id: str, winning_side: str) -> None:
        if market_id in self._resolved:
            return
        self._resolved.add(market_id)

        info = self._markets.get(market_id, {})
        q = info.get("question", market_id[:20])
        losing_side = "NO" if winning_side == "YES" else "YES"

        win_key  = (market_id, winning_side)
        loss_key = (market_id, losing_side)

        win_id  = self._filled.get(win_key)
        loss_id = self._filled.get(loss_key)

        pnl_log = []

        if win_id:
            await self._close_trade(win_id, "WIN", 1.0)
            async with self._conn.execute(
                "SELECT pnl FROM straddle_trades WHERE id=?", (win_id,)
            ) as cur:
                row = await cur.fetchone()
            pnl_log.append(f"WIN {winning_side} pnl={row[0]:+.2f}" if row else "WIN")

        if loss_id:
            await self._close_trade(loss_id, "LOSS", 0.0)
            pnl_log.append(f"LOSS {losing_side} pnl={-float(self._bet_usd):+.2f}")

        if not win_id and not loss_id:
            logger.debug(f"Resolved (no fills): {q[:60]}")
            return

        result = "BOTH" if (win_id and loss_id) else ("WIN_ONLY" if win_id else "LOSS_ONLY")
        logger.info(f"RESOLVED [{result}] {q[:60]} | {' | '.join(pnl_log)}")

    # ── Position watchdog ─────────────────────────────────────────────────────

    async def _position_checker(self) -> None:
        """
        Every 60s, REST-checks open positions in expired markets to catch
        any resolutions the WS missed (reconnects, race conditions).
        """
        while True:
            await asyncio.sleep(_POSITION_CHECK_SECS)
            try:
                await self._check_expired()
            except Exception as exc:
                logger.error(f"Position checker error: {exc}")

    async def _check_expired(self) -> None:
        now = datetime.now(timezone.utc)
        for mid, info in list(self._markets.items()):
            if mid in self._resolved:
                continue
            end_str = info.get("end_time", "")
            if end_str:
                try:
                    end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                    if end_dt > now:
                        continue  # still active
                except ValueError:
                    pass  # unknown end time — check anyway

            # Market expired — use CLOB /markets/{id} which has winner field
            try:
                r = await self._http.get(
                    f"{_CLOB_BASE}/markets/{mid}", timeout=8
                )
                if r.status_code != 200:
                    continue
                data = r.json()
            except Exception:
                continue

            # Find the winning outcome from tokens[].winner
            tokens = data.get("tokens", [])
            for t in tokens:
                if not t.get("winner"):
                    continue
                outcome = t.get("outcome", "")
                # "Up" maps to asset_yes → "YES"; "Down" maps to asset_no → "NO"
                winning_side = "YES" if outcome == "Up" else "NO"
                await self._resolve_market(mid, winning_side=winning_side)
                break

    # ── Snapshot ──────────────────────────────────────────────────────────────

    async def _snapshot_worker(self) -> None:
        while True:
            await asyncio.sleep(_SNAPSHOT_INTERVAL)
            try:
                await self._snapshot()
            except Exception as exc:
                logger.error(f"Snapshot error: {exc}")

    # ── Telegram ──────────────────────────────────────────────────────────────

    async def _telegram_worker(self) -> None:
        """Sends an hourly portfolio report to Telegram."""
        await asyncio.sleep(_TELEGRAM_INTERVAL)  # wait 1h before first send
        while True:
            try:
                await self._send_telegram_report()
            except Exception as exc:
                logger.error(f"Telegram worker error: {exc}")
            await asyncio.sleep(_TELEGRAM_INTERVAL)

    async def _send_telegram_report(self) -> None:
        if not self._tg_token or not self._tg_chat_id:
            logger.warning("Telegram not configured — skipping report")
            return

        async with self._conn.execute(
            """SELECT
                 COUNT(DISTINCT market_id)                                          as markets,
                 SUM(CASE WHEN status='OPEN'   THEN 1 ELSE 0 END)                  as open_pos,
                 SUM(CASE WHEN status IN ('WIN','WIN_2X') THEN 1 ELSE 0 END)       as wins,
                 SUM(CASE WHEN status='LOSS'   THEN 1 ELSE 0 END)                  as losses,
                 ROUND(SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END), 2)             as gross_profit,
                 ROUND(SUM(CASE WHEN pnl < 0 THEN pnl ELSE 0 END), 2)             as gross_loss,
                 ROUND(SUM(COALESCE(pnl, 0)), 2)                                   as net_pnl
               FROM straddle_trades"""
        ) as cur:
            row = await cur.fetchone()

        if not row:
            return

        markets, open_pos, wins, losses, gross_profit, gross_loss, net_pnl = row
        gross_profit = gross_profit or 0.0
        gross_loss   = gross_loss   or 0.0
        net_pnl      = net_pnl      or 0.0
        portfolio    = self._bankroll + net_pnl

        # count markets where both sides filled
        async with self._conn.execute(
            """SELECT COUNT(*) FROM (
                 SELECT market_id FROM straddle_trades
                 GROUP BY market_id HAVING COUNT(DISTINCT side) = 2
               )"""
        ) as cur:
            both_filled = (await cur.fetchone())[0]

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        wins_total = wins or 0
        losses_total = losses or 0
        closed  = wins_total + losses_total
        win_pct = f"{100*wins_total/closed:.1f}%" if closed else "N/A"

        # count 2x exits from the telegram query (wins already includes WIN_2X)
        async with self._conn.execute(
            "SELECT COUNT(*) FROM straddle_trades WHERE status='WIN_2X'"
        ) as cur:
            wins_2x = (await cur.fetchone())[0] or 0

        msg = (
            f"<b>⚡ 5-Min Straddle Paper Trader</b>\n"
            f"<i>{now_str}</i>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 <b>Starting Balance:</b>  ${self._bankroll:.2f}\n"
            f"📈 <b>Realised Profit:</b>   ${gross_profit:+.2f}\n"
            f"📉 <b>Realised Loss:</b>     ${gross_loss:.2f}\n"
            f"🏦 <b>Portfolio:</b>         ${portfolio:.2f}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 Markets tracked:   {markets or 0}\n"
            f"🔁 Both sides filled: {both_filled}\n"
            f"✅ Wins / ❌ Losses:  {wins_total} / {losses_total}  ({win_pct})\n"
            f"   └ 2x exits:        {wins_2x}\n"
            f"📂 Open positions:    {open_pos or 0}\n"
            f"Max fill price: ${self._max_price:.2f} | Bet/side: ${float(self._bet_usd):.2f}"
        )

        url = f"https://api.telegram.org/bot{self._tg_token}/sendMessage"
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(url, json={
                    "chat_id": self._tg_chat_id,
                    "text": msg,
                    "parse_mode": "HTML",
                })
                r.raise_for_status()
            logger.info("Telegram report sent")
        except Exception as exc:
            logger.error(f"Telegram send failed: {exc}")

    # ── Main run ──────────────────────────────────────────────────────────────

    async def run(self) -> None:
        await self._init_db()
        self._http = httpx.AsyncClient(
            headers={"User-Agent": "five-min-straddle-paper/1.0"},
            follow_redirects=True,
        )

        # Initial discovery before WS connects
        try:
            await self._discover_markets()
        except Exception as exc:
            logger.warning(f"Initial market discovery error: {exc}")

        await self._snapshot()
        logger.info(
            f"Starting straddle paper trader | max_price={self._max_price} "
            f"bet_usd={float(self._bet_usd)}"
        )

        try:
            await asyncio.gather(
                self._ws_worker(),
                self._market_poller(),
                self._position_checker(),
                self._snapshot_worker(),
                self._telegram_worker(),
            )
        finally:
            if self._conn:
                await self._conn.close()
            if self._http:
                await self._http.aclose()


# ── CLI ───────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="5-min crypto straddle paper trader")
    p.add_argument("--db",        default="five_min_paper.db", help="SQLite DB path")
    p.add_argument("--bankroll",  type=float, default=500.0,
                   help="Starting balance in USD for portfolio tracking (default $500)")
    p.add_argument("--max-price", type=float, default=0.20,
                   help="Fill if price drops to or below this (default 0.20)")
    p.add_argument("--bet-size",  type=float, default=3.0,
                   help="USD per side per market (default $3)")
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    p.add_argument("--log-file",  default="logs/five_min_paper.log")
    return p.parse_args()


def main() -> None:
    args = _parse_args()

    logger.remove()
    logger.add(sys.stderr, level=args.log_level,
               format="<green>{time:HH:mm:ss}</green> | <level>{level:8}</level> | {message}")
    if args.log_file:
        from pathlib import Path
        Path(args.log_file).parent.mkdir(parents=True, exist_ok=True)
        logger.add(args.log_file, level="DEBUG", rotation="50 MB")

    tg_token, tg_chat_id = _load_telegram_creds()
    if tg_token and tg_chat_id:
        logger.info("Telegram configured — hourly reports enabled")
    else:
        logger.warning("Telegram not configured — set TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in .env")

    trader = StraddlePaperTrader(
        db_path    = args.db,
        max_price  = args.max_price,
        bet_usd    = args.bet_size,
        bankroll   = args.bankroll,
        tg_token   = tg_token,
        tg_chat_id = tg_chat_id,
    )

    try:
        asyncio.run(trader.run())
    except KeyboardInterrupt:
        logger.info("Stopped by user")


if __name__ == "__main__":
    main()
