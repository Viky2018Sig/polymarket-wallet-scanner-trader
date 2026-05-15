"""
Real-time wallet monitor — WebSocket edition.

Architecture: WebSocket producer → REST resolver → signal consumer.

  ┌──────────────────────┐
  │  _ws_worker          │  ← CLOB WebSocket, last_trade_price events (~50ms latency)
  │  wss://ws-clob…/ws/  │    puts (asset_id, price, side) onto _ws_event_queue
  └────────┬─────────────┘
           │ _ws_event_queue
  ┌────────▼─────────────┐
  │  _trade_fetcher      │  ← targeted REST GET /trades?asset_id=X (one call per event)
  │                      │    filters for tracked wallet maker_address, builds WalletTrade
  └────────┬─────────────┘
           │ asyncio.Queue
  ┌────────▼─────────────┐
  │  _signal_processor   │  ← applies guardrails, writes signals to DB
  │                      │    G0: BUY in 0.01–0.15  G1: dedup  G2: time  G3: ceiling
  └─────────┬────────────┘
            │
  ┌─────────▼────────────┐
  │  _housekeep_worker   │  ← DB prune every 6h
  │  _unfollow_checker   │  ← resubscribes WS every 30min, refreshes open-keys
  └──────────────────────┘

Total detection latency: ~200–300 ms (vs 0–10 s with REST poll).
WebSocket reconnects with exponential backoff; subscriptions refresh every 30 min.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Set, Tuple

from loguru import logger

from config import settings
from src.api.clob_client import ClobClient
from src.api.data_client import DataApiClient
from src.api.models import PaperTradeStatus, Signal, SignalType, WalletScore, WalletTrade
from src.analysis.kelly import calculate_kelly, expected_value
from src.analysis.metrics import compute_price_bucket_stats
from src.scanner.wallet_discovery import _normalise_data_api_trades
from src.storage.database import Database
from src.trader.signals import _check_unfollow_conditions

_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class RealtimeMonitor:
    """
    High-frequency wallet monitor using CLOB WebSocket for sub-second trade detection.

    Detection path: WS event (asset_id, price, side) → REST /trades?asset_id=X
    → filter tracked maker_address → WalletTrade → signal guardrails → DB signal.
    """

    def __init__(
        self,
        data_client: DataApiClient,
        clob_client: ClobClient,
        db: Database,
        bankroll: Optional[Decimal] = None,
    ) -> None:
        self._data = data_client  # kept for interface compat; not used in WS path
        self._clob = clob_client
        self._db = db
        self._bankroll = bankroll or Decimal(str(settings.starting_bankroll))

        # Signal pipeline queue (WalletTrade → signal processor)
        self._queue: asyncio.Queue[WalletTrade] = asyncio.Queue(maxsize=2000)

        # Intermediate queue: (asset_id, price_str, side, tx_hash) before REST resolution
        self._ws_event_queue: asyncio.Queue[Tuple[str, str, str, str]] = asyncio.Queue(maxsize=5000)

        # (market_id, wallet_address) pairs already open in paper_trades
        self._open_keys: Set[Tuple[str, str]] = set()

        # market_id → estimated end datetime
        self._market_end_cache: Dict[str, Optional[datetime]] = {}

        # Lower-cased tracked wallet addresses (fast membership test)
        self._tracked_addrs: Set[str] = set()

        # Trade IDs already processed (dedup across rapid duplicate WS events)
        self._seen_trade_ids: Set[str] = set()

        # Set by _unfollow_checker to trigger WS reconnect with fresh subscriptions
        self._resubscribe_event = asyncio.Event()

    # ── Public entry points ────────────────────────────────────────────────────

    async def run(self) -> None:
        """Start all workers and run until cancelled."""
        logger.info(
            f"RealtimeMonitor starting — WebSocket mode, "
            f"price ceiling: {settings.max_copy_price_multiplier}×, "
            f"min time remaining: {settings.min_market_seconds_remaining}s"
        )
        await self._seed_state()
        await asyncio.gather(
            self._ws_worker(),
            self._trade_fetcher(),
            self._signal_processor(),
            self._housekeep_worker(),
            self._unfollow_checker(),
            self._wallet_activity_poller(),
            self._position_closer_worker(),
        )

    def update_bankroll(self, bankroll: Decimal) -> None:
        self._bankroll = bankroll

    # ── Seeding ────────────────────────────────────────────────────────────────

    async def _seed_state(self) -> None:
        """Populate _tracked_addrs and _open_keys from DB on startup."""
        tracked = await self._db.get_tracked_wallets()
        self._tracked_addrs = {ws.wallet_address.lower() for ws in tracked}

        open_trades = await self._db.get_open_paper_trades()
        self._open_keys = {(t.market_id, t.wallet_followed) for t in open_trades}

        logger.info(
            f"Seeded: {len(tracked)} tracked wallets, "
            f"{len(self._open_keys)} open positions"
        )

    # ── Producer: WebSocket worker ─────────────────────────────────────────────

    async def _ws_worker(self) -> None:
        """
        Maintain a persistent CLOB WebSocket connection.

        Subscribes to all token IDs (asset_ids) from tracked wallets' recent trades.
        Puts (asset_id, price, side) tuples onto _ws_event_queue for each
        last_trade_price event. Reconnects with exponential backoff on any error.
        Breaks out of the receive loop when _resubscribe_event is set (triggered
        by _unfollow_checker every 30 min), then reconnects with a refreshed
        subscription list.
        """
        import websockets  # deferred so the library is only required at runtime

        delay = 1.0
        max_delay = settings.ws_reconnect_max_delay

        while True:
            asset_ids = await self._db.get_tracked_asset_ids(since_days=7)
            if not asset_ids:
                logger.warning("WS: no asset IDs to subscribe to — retrying in 30s")
                await asyncio.sleep(30)
                continue

            sub_msg = json.dumps({"assets_ids": asset_ids, "type": "market"})
            logger.info(f"WS: connecting ({len(asset_ids)} subscriptions)")

            try:
                async with websockets.connect(
                    _WS_URL,
                    ping_interval=20,
                    ping_timeout=10,
                    open_timeout=15,
                ) as ws:
                    await ws.send(sub_msg)
                    self._resubscribe_event.clear()
                    delay = 1.0  # reset backoff after successful connect
                    logger.info(f"WS: connected, receiving events (subscribed {len(asset_ids)} assets)")

                    events_total = 0
                    events_ltp = 0
                    next_log = asyncio.get_event_loop().time() + 60

                    while not self._resubscribe_event.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                        except asyncio.TimeoutError:
                            continue

                        try:
                            data = json.loads(raw)
                        except json.JSONDecodeError:
                            continue

                        events = data if isinstance(data, list) else [data]
                        events_total += len(events)
                        for event in events:
                            et = event.get("event_type")
                            if et == "last_trade_price":
                                events_ltp += 1
                            if et != "last_trade_price":
                                continue
                            asset_id = event.get("asset_id", "")
                            price_str = event.get("price", "0")
                            side = event.get("side", "").upper()
                            tx_hash = event.get("transaction_hash", "")
                            if not asset_id:
                                continue
                            try:
                                self._ws_event_queue.put_nowait(
                                    (asset_id, price_str, side, tx_hash)
                                )
                            except asyncio.QueueFull:
                                logger.warning("WS event queue full — dropping event")

                        now_t = asyncio.get_event_loop().time()
                        if now_t >= next_log:
                            logger.info(
                                f"WS heartbeat: {events_total} events, "
                                f"{events_ltp} last_trade_price in last 60s"
                            )
                            events_total = 0
                            events_ltp = 0
                            next_log = now_t + 60

                    logger.info("WS: resubscribing with updated asset list")

            except Exception as exc:
                logger.warning(f"WS error: {exc!r} — reconnecting in {delay:.0f}s")
                await asyncio.sleep(delay)
                delay = min(delay * 2, max_delay)

    # ── REST resolver: trade fetcher ───────────────────────────────────────────

    async def _trade_fetcher(self) -> None:
        """
        Drain the WS event queue.

        For each BUY event in the low-price range, fire a targeted REST call to
        GET /trades?asset_id=X&limit=5 to retrieve the maker_address. Filter for
        tracked wallets, build WalletTrade objects, upsert to DB cache, and emit
        onto the main signal queue.
        """
        logger.info("Trade fetcher started")
        while True:
            try:
                asset_id, price_str, side, tx_hash = await asyncio.wait_for(
                    self._ws_event_queue.get(), timeout=5.0
                )
            except asyncio.TimeoutError:
                continue

            try:
                price = float(price_str)
            except (ValueError, TypeError):
                continue

            # Pre-filter: only BUY up to signal_price_max (skip expensive REST call otherwise)
            # Uses signal_price_max (default 0.30) not low_price_max (0.15) so tracked wallets
            # that shift to mid-range entries (e.g. 0.19) are still detected.
            if side != "BUY" or not (
                settings.low_price_min <= price <= settings.signal_price_max
            ):
                continue

            # Dedup at event level: same tx can fire multiple WS events
            if tx_hash and tx_hash in self._seen_trade_ids:
                continue

            # Data API /trades?asset=X — public, returns proxyWallet
            try:
                raw_trades = await self._data.get_recent_trades_by_asset(asset_id, limit=5)
            except Exception as exc:
                logger.debug(f"Trade fetcher Data API error [{asset_id[:12]}…]: {exc}")
                continue

            now = datetime.utcnow()
            new_trades: List[WalletTrade] = []

            for dt in raw_trades:
                proxy = dt.proxy_wallet
                if not proxy or proxy.lower() not in self._tracked_addrs:
                    continue

                # Build WalletTrade via the existing normaliser (handles size/notional correctly)
                normalised = _normalise_data_api_trades(proxy, [dt])
                if not normalised:
                    continue
                wt = normalised[0]

                # Use WS tx_hash for dedup if it matches; otherwise use the trade's own id
                tid = wt.trade_id
                if tid in self._seen_trade_ids:
                    continue
                self._seen_trade_ids.add(tid)
                if tx_hash:
                    self._seen_trade_ids.add(tx_hash)  # also mark the WS event as seen

                # Ignore stale trades that slipped through (e.g. during reconnect)
                if wt.matched_at and (now - wt.matched_at).total_seconds() > 120:
                    continue

                new_trades.append(wt)

            if not new_trades:
                continue

            try:
                await self._db.upsert_wallet_trades(new_trades)
            except Exception as exc:
                logger.error(f"DB upsert error in trade fetcher: {exc}")

            for wt in new_trades:
                try:
                    self._queue.put_nowait(wt)
                    logger.debug(
                        f"WS trade: wallet={wt.wallet_address[:10]}… "
                        f"asset={asset_id[:12]}… price={float(wt.price):.4f}"
                    )
                except asyncio.QueueFull:
                    logger.warning("Signal queue full — dropping trade event")

    # ── Consumer: signal processor ─────────────────────────────────────────────

    async def _signal_processor(self) -> None:
        """
        Drain the signal queue, apply guardrails, and write signals to DB.
        """
        logger.info("Signal processor started")
        _counts: Dict[str, int] = {"processed": 0, "signals": 0, "g1": 0, "g2": 0, "g3": 0, "g4": 0, "ev": 0}
        _next_summary = asyncio.get_event_loop().time() + 300  # log summary every 5 min

        while True:
            try:
                trade: WalletTrade = await asyncio.wait_for(
                    self._queue.get(), timeout=5.0
                )
            except asyncio.TimeoutError:
                if asyncio.get_event_loop().time() >= _next_summary and _counts["processed"]:
                    logger.info(
                        f"Signal pipeline (5 min): processed={_counts['processed']} "
                        f"signals={_counts['signals']} "
                        f"blocked: G1={_counts['g1']} G2={_counts['g2']} "
                        f"G3(ask>ceiling)={_counts['g3']} G4(no_score)={_counts['g4']} ev={_counts['ev']}"
                    )
                    for k in _counts:
                        _counts[k] = 0
                    _next_summary = asyncio.get_event_loop().time() + 300
                continue

            try:
                result = await self._process_trade(trade)
                _counts["processed"] += 1
                if result:
                    _counts[result] += 1
            except Exception as exc:
                logger.error(f"Signal processor error on trade {trade.trade_id}: {exc}")
            finally:
                self._queue.task_done()

    async def _process_trade(self, trade: WalletTrade) -> Optional[str]:
        """Apply all guardrails and emit a signal if the trade qualifies. Returns block reason or None."""

        # ── Guardrail 0: only BUY up to signal_price_max ─────────────────────
        price_f = float(trade.price)
        if trade.side.upper() != "BUY" or not (
            settings.low_price_min <= price_f <= settings.signal_price_max
        ):
            return None

        # ── Guardrail 1: dedup — skip if already holding this market ──────────
        key = (trade.market_id, trade.wallet_address)
        if key in self._open_keys:
            return "g1"

        # ── Guardrail 2: market time remaining ────────────────────────────────
        end_time = await self._get_market_end_time(trade.market_id)
        if end_time is not None:
            secs_left = (end_time - datetime.utcnow()).total_seconds()
            if secs_left < settings.min_market_seconds_remaining:
                logger.debug(
                    f"G2 skip {trade.market_id[:12]}…: "
                    f"only {secs_left:.0f}s remaining (min {settings.min_market_seconds_remaining}s)"
                )
                return "g2"

        # ── Guardrail 3: price ceiling ─────────────────────────────────────────
        current_ask: Optional[float] = None
        if trade.asset_id:
            current_ask = await self._clob.get_best_ask(trade.asset_id)

        signal_price = float(trade.price)
        max_copy_price = signal_price * settings.max_copy_price_multiplier

        if current_ask is not None and current_ask > max_copy_price:
            logger.debug(
                f"G3 skip {trade.market_id[:12]}…: "
                f"ask {current_ask:.4f} > ceiling {max_copy_price:.4f} "
                f"({settings.max_copy_price_multiplier}× signal {signal_price:.4f})"
            )
            return "g3"

        # Use current ask as entry price if available
        entry_price = Decimal(str(current_ask)) if current_ask else trade.price

        # ── Generate signal ────────────────────────────────────────────────────
        wallet_score = await self._db.get_wallet_score(trade.wallet_address)
        if wallet_score is None:
            return "g4"

        historical = await self._db.get_wallet_trades(trade.wallet_address)
        bucket_stats = compute_price_bucket_stats(historical)
        bucket = trade.price_bucket
        bucket_data = bucket_stats.get(bucket, {})
        win_rate = bucket_data.get("win_rate", wallet_score.win_rate)
        if bucket_data.get("resolved", 0) < 5:
            win_rate = wallet_score.win_rate or 0.30

        ev = expected_value(win_rate, float(entry_price))
        if ev <= 0:
            logger.debug(f"Skip {trade.market_id[:12]}…: negative EV ({ev:.4f})")
            return "ev"

        kelly = calculate_kelly(
            win_rate=win_rate,
            entry_price=float(entry_price),
            bankroll=self._bankroll,
        )
        if kelly.recommended_fraction <= 0:
            return "ev"

        ask_str = f"{current_ask:.4f}" if current_ask is not None else "?"
        signal = Signal(
            signal_type=SignalType.BUY,
            wallet_address=trade.wallet_address,
            market_id=trade.market_id,
            asset_id=trade.asset_id,
            price=entry_price,
            kelly_fraction=kelly.recommended_fraction,
            recommended_size_usd=kelly.dollar_amount,
            wallet_win_rate=win_rate,
            wallet_profit_factor=wallet_score.profit_factor,
            price_bucket=bucket,
            notes=(
                f"ws ask={ask_str} signal={signal_price:.4f} "
                f"ceiling={max_copy_price:.4f} ev={ev:.4f}"
            ),
            generated_at=datetime.utcnow(),
        )

        signal_id = await self._db.insert_signal(signal)
        signal.id = signal_id
        self._open_keys.add(key)

        logger.info(
            f"SIGNAL #{signal_id}: wallet={trade.wallet_address[:10]}… "
            f"market={trade.market_id[:12]}… "
            f"signal_price={signal_price:.4f} ask={current_ask or '?'} "
            f"ceiling={max_copy_price:.4f} "
            f"size=${float(kelly.dollar_amount):.2f}"
        )
        return "signals"

    # ── Market end-time cache ──────────────────────────────────────────────────

    async def _get_market_end_time(self, market_id: str) -> Optional[datetime]:
        """
        Return cached market end datetime, fetching from Gamma API on first miss.
        Returns None if unavailable (treated as "assume open").
        """
        if market_id in self._market_end_cache:
            return self._market_end_cache[market_id]

        try:
            from src.api.gamma_client import GammaClient
            async with GammaClient() as gamma:
                market = await gamma.get_market_by_condition_id(market_id)
            if market and market.end_date_iso:
                end_dt = datetime.fromisoformat(
                    market.end_date_iso.replace("Z", "+00:00")
                ).replace(tzinfo=None)
                self._market_end_cache[market_id] = end_dt
                return end_dt
        except Exception:
            pass

        self._market_end_cache[market_id] = None
        return None

    # ── Background workers ─────────────────────────────────────────────────────

    async def _housekeep_worker(self) -> None:
        """Prune old DB rows and log disk usage every 6 hours."""
        while True:
            await asyncio.sleep(6 * 3600)
            try:
                deleted = await self._db.prune_old_data(
                    wallet_trades_retention_days=settings.wallet_trades_retention_days,
                    signals_retention_days=settings.signals_retention_days,
                )
                size_mb = await self._db.db_size_bytes() / 1_048_576
                # Also clear the seen-trade-ID set to prevent unbounded growth
                self._seen_trade_ids.clear()
                logger.info(
                    f"Housekeep complete — DB size: {size_mb:.1f} MB | "
                    f"deleted: {deleted}"
                )
            except Exception as exc:
                logger.error(f"Housekeep error: {exc}")

    async def _unfollow_checker(self) -> None:
        """
        Every 30 minutes: check unfollow conditions, refresh open-keys cache,
        refresh tracked-address set, and trigger WS resubscription.
        """
        while True:
            await asyncio.sleep(30 * 60)
            try:
                await _check_unfollow_conditions(self._db)

                open_trades = await self._db.get_open_paper_trades()
                self._open_keys = {(t.market_id, t.wallet_followed) for t in open_trades}

                tracked = await self._db.get_tracked_wallets()
                self._tracked_addrs = {ws.wallet_address.lower() for ws in tracked}

                # Signal the WS worker to reconnect with updated subscriptions
                self._resubscribe_event.set()
                logger.info(
                    f"Unfollow check done — {len(self._tracked_addrs)} wallets tracked, "
                    f"{len(self._open_keys)} open positions, WS resubscribe triggered"
                )
            except Exception as exc:
                logger.error(f"Unfollow checker error: {exc}")

    async def _wallet_activity_poller(self) -> None:
        """
        Poll each tracked wallet's /activity endpoint every 30 seconds.

        This is a fallback for busy markets where the WS + Data API pipeline misses
        trades because the wallet's entry is pushed below the limit=5 window by other
        traders. Direct activity polling guarantees every new trade is caught regardless
        of market volume.
        """
        from src.scanner.wallet_discovery import _normalise_data_api_trades
        POLL_INTERVAL = 3   # seconds between full poll cycles
        LOOKBACK_MINUTES = 1  # fetch only trades newer than this per wallet

        # Track the last trade timestamp seen per wallet to avoid reprocessing
        last_seen: Dict[str, datetime] = {}

        logger.info("Wallet activity poller started (3s interval, parallel)")

        async def _poll_one(addr: str) -> None:
            """Poll a single wallet and forward new trades to the signal queue."""
            cutoff = datetime.utcnow() - timedelta(minutes=LOOKBACK_MINUTES)
            try:
                raw = await self._data.get_wallet_activity(addr, limit=50, offset=0)
            except Exception as exc:
                logger.debug(f"Activity poll error [{addr[:10]}]: {exc}")
                return

            trades = _normalise_data_api_trades(addr, raw)
            wallet_last = last_seen.get(addr, cutoff)

            new_trades = [
                t for t in trades
                if t.matched_at is not None
                and t.matched_at > wallet_last
                and t.side.upper() == "BUY"
                and settings.low_price_min <= float(t.price) <= settings.signal_price_max
            ]

            if not new_trades:
                return

            # Update seen timestamp
            latest = max(t.matched_at for t in new_trades if t.matched_at)
            last_seen[addr] = latest

            # Upsert to DB, then forward all new_trades to signal pipeline
            await self._db.upsert_wallet_trades(new_trades)
            forwarded = 0
            for trade in new_trades:
                key = (trade.market_id, trade.wallet_address)
                if key not in self._open_keys:
                    try:
                        self._queue.put_nowait(trade)
                        forwarded += 1
                    except asyncio.QueueFull:
                        logger.warning("Signal queue full — dropping activity poll trade")

            if forwarded:
                logger.info(
                    f"Activity poll [{addr[:10]}…]: {forwarded} new trades "
                    f"forwarded to signal pipeline"
                )

        while True:
            await asyncio.sleep(POLL_INTERVAL)
            try:
                tracked = await self._db.get_tracked_wallets()
                # Poll all wallets concurrently for minimal latency
                await asyncio.gather(*[_poll_one(ws.wallet_address) for ws in tracked], return_exceptions=True)
            except Exception as exc:
                logger.error(f"Wallet activity poller error: {exc}")

    async def _position_closer_worker(self) -> None:
        """
        Auto-close open paper positions when the market has effectively resolved.

        Checks every 5 minutes. If the CLOB best ask for an open position is:
          >= 0.99  → resolved YES  → close as CLOSED_WIN  at 0.99
          <= 0.01  → resolved NO   → close as CLOSED_LOSS at 0.01

        This avoids waiting for the nightly scan --skip-discovery to detect wallet
        exits, which means positions that resolve intraday get realised the same day.
        """
        CHECK_INTERVAL = 5 * 60  # seconds
        WIN_THRESHOLD = Decimal("0.99")
        LOSS_THRESHOLD = Decimal("0.01")

        logger.info("Position closer worker started (5-min interval)")

        while True:
            await asyncio.sleep(CHECK_INTERVAL)
            try:
                open_trades = await self._db.get_open_paper_trades()
                if not open_trades:
                    continue

                # Batch: resolve unique asset_ids in parallel, then close trades
                unique_assets = list({t.asset_id for t in open_trades if t.asset_id})

                async def _fetch_ask(asset_id: str) -> Tuple[str, Optional[float]]:
                    try:
                        ask = await self._clob.get_best_ask(asset_id)
                        return asset_id, ask
                    except Exception:
                        return asset_id, None

                results = await asyncio.gather(*[_fetch_ask(a) for a in unique_assets], return_exceptions=False)
                asset_asks: Dict[str, Optional[float]] = dict(results)

                wins_closed = 0
                losses_closed = 0

                for trade in open_trades:
                    ask = asset_asks.get(trade.asset_id)
                    if ask is None:
                        continue

                    ask_dec = Decimal(str(ask))
                    if ask_dec >= WIN_THRESHOLD:
                        closed = await self._db.close_paper_trade(
                            trade_id=trade.id,
                            exit_price=WIN_THRESHOLD,
                            status=PaperTradeStatus.CLOSED_WIN,
                        )
                        if closed:
                            wins_closed += 1
                            self._open_keys.discard((trade.market_id, trade.wallet_followed))
                            logger.info(
                                f"Auto-closed WIN: trade#{trade.id} "
                                f"entry={trade.entry_price} exit=0.99 "
                                f"pnl={closed.pnl} market={trade.market_id[:20]}…"
                            )
                    elif ask_dec <= LOSS_THRESHOLD:
                        closed = await self._db.close_paper_trade(
                            trade_id=trade.id,
                            exit_price=LOSS_THRESHOLD,
                            status=PaperTradeStatus.CLOSED_LOSS,
                        )
                        if closed:
                            losses_closed += 1
                            self._open_keys.discard((trade.market_id, trade.wallet_followed))
                            logger.info(
                                f"Auto-closed LOSS: trade#{trade.id} "
                                f"entry={trade.entry_price} exit=0.01 "
                                f"pnl={closed.pnl} market={trade.market_id[:20]}…"
                            )

                if wins_closed or losses_closed:
                    logger.info(
                        f"Position closer: {wins_closed} wins, {losses_closed} losses "
                        f"auto-closed this cycle"
                    )
            except Exception as exc:
                logger.error(f"Position closer error: {exc}")
