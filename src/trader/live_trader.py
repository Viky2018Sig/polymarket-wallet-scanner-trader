"""
Live Trading Engine — places real CLOB orders from scanner signals.

Reads unacted signals from the signals DB table, places FOK market orders
via py-clob-client-v2 (POLY_1271 / SIG_TYPE=3 deposit-wallet signing), and
manages positions with stop-loss / take-profit / resolution detection.

Credentials are loaded from settings.live_env_file (default: copybot .env).
State is persisted to settings.live_state_file (default: data/live_state.json).

Usage:
    python main.py live-trade          # continuous loop
    python main.py live-trade --once   # single cycle
    python main.py live-trade --status # print portfolio summary
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

from config import settings
from src.api.models import Signal
from src.storage.database import Database

CLOB_HOST = "https://clob.polymarket.com"
POLYGON_CHAIN_ID = 137

_BLOCKED_MARKET_KEYWORDS = [
    "bitcoin up or down",
    "btc up or down",
]

# Token-ID cache: (market_id, outcome_lower) → token_id
_token_cache: Dict[str, str] = {}


# ── Credential loading ─────────────────────────────────────────────────────────

def _load_live_env() -> Dict[str, str]:
    env: Dict[str, str] = {}
    env_path = Path(settings.live_env_file)
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    for key in ("PK", "WALLET", "PROXY_WALLET", "CLOB_API_KEY", "CLOB_SECRET",
                "CLOB_PASSPHRASE", "SIG_TYPE"):
        val = os.environ.get(key)
        if val:
            env[key] = val
    return env


def _build_clob_client():
    """Build a py-clob-client-v2 ClobClient with L2 credentials."""
    try:
        from py_clob_client_v2.client import ClobClient
        from py_clob_client_v2.clob_types import ApiCreds
    except ImportError:
        raise RuntimeError(
            "py-clob-client-v2 not installed. Run: pip install py-clob-client-v2"
        )

    env = _load_live_env()
    pk = env.get("PK", "")
    if not pk:
        raise RuntimeError(
            f"PK (private key) not found in {settings.live_env_file}. "
            "Ensure the live credentials file is configured correctly."
        )

    sig_type = int(env.get("SIG_TYPE", "0"))
    api_key = env.get("CLOB_API_KEY", "")
    api_secret = env.get("CLOB_SECRET", "")
    api_passphrase = env.get("CLOB_PASSPHRASE", "")
    proxy_wallet = env.get("PROXY_WALLET", "")

    if not all([api_key, api_secret, api_passphrase]):
        raise RuntimeError(
            "L2 credentials (CLOB_API_KEY, CLOB_SECRET, CLOB_PASSPHRASE) not found. "
            f"Check {settings.live_env_file}"
        )

    creds = ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase)
    kwargs: Dict[str, Any] = dict(
        chain_id=POLYGON_CHAIN_ID,
        key=pk,
        creds=creds,
        signature_type=sig_type,
    )
    if sig_type in (1, 2, 3) and proxy_wallet:
        kwargs["funder"] = proxy_wallet

    return ClobClient(CLOB_HOST, **kwargs)


# ── Token ID resolution ────────────────────────────────────────────────────────

def _resolve_token_id(client, market_id: str, outcome: str) -> Optional[str]:
    """Resolve CLOB token_id for a market outcome. CLOB first, Gamma fallback."""
    cache_key = f"{market_id}:{outcome.lower()}"
    if cache_key in _token_cache:
        return _token_cache[cache_key]

    try:
        mkt = client.get_market(condition_id=market_id)
        if mkt and isinstance(mkt, dict):
            for tok in mkt.get("tokens", []):
                if tok.get("outcome", "").strip().lower() == outcome.strip().lower():
                    tid = tok.get("token_id", "")
                    if tid:
                        _token_cache[cache_key] = tid
                        return tid
    except Exception as exc:
        logger.debug("CLOB market lookup failed for {}: {}", market_id[:16], exc)

    try:
        import httpx
        url = "https://gamma-api.polymarket.com/markets"
        resp = httpx.get(url, params={"condition_id": market_id, "limit": 1}, timeout=10)
        if resp.is_success:
            items = resp.json()
            mkt = items[0] if isinstance(items, list) and items else None
            if mkt:
                raw_ids = mkt.get("clobTokenIds", "[]")
                if isinstance(raw_ids, str):
                    raw_ids = json.loads(raw_ids)
                raw_outcomes = mkt.get("outcomes", "[]")
                if isinstance(raw_outcomes, str):
                    raw_outcomes = json.loads(raw_outcomes)
                for i, name in enumerate(raw_outcomes):
                    if str(name).strip().lower() == outcome.strip().lower() and i < len(raw_ids):
                        tid = str(raw_ids[i])
                        _token_cache[cache_key] = tid
                        return tid
    except Exception as exc:
        logger.debug("Gamma token lookup failed for {}: {}", market_id[:16], exc)

    logger.warning("Could not resolve token_id for {} outcome={}", market_id[:16], outcome)
    return None


# ── State management ───────────────────────────────────────────────────────────

def _state_path() -> Path:
    return Path(settings.live_state_file)


def _load_state() -> Dict[str, Any]:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        try:
            with open(p) as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            logger.warning("Corrupt live state file — starting fresh")
    return _init_state(settings.live_starting_balance)


def _init_state(balance: float) -> Dict[str, Any]:
    return {
        "balance": balance,
        "initial_balance": balance,
        "positions": [],
        "trade_history": [],
        "total_realized_pnl": 0.0,
        "total_trades": 0,
        "winning_trades": 0,
        "losing_trades": 0,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }


def _save_state(state: Dict[str, Any]) -> None:
    state["last_updated"] = datetime.now(timezone.utc).isoformat()
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w") as f:
        json.dump(state, f, indent=2, default=str)


# ── LiveTrader ─────────────────────────────────────────────────────────────────

class LiveTrader:
    """
    Live trading engine that reads scanner signals and executes real CLOB orders.

    Each cycle:
    1. process_signals() — read unacted DB signals, place FOK buy orders
    2. check_positions() — fetch current prices, apply SL/TP/resolution
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    async def run_live_trade_cycle(self) -> None:
        """Run one full cycle: process new signals then check existing positions."""
        logger.info("Live trade cycle starting")
        opened = await self.process_signals()
        closed = await self.check_positions()
        state = _load_state()
        logger.info(
            "Live trade cycle complete — opened={} closed={} "
            "balance=${:.2f} positions={}",
            len(opened),
            len(closed),
            state["balance"],
            len(state["positions"]),
        )

    async def process_signals(self) -> List[Dict[str, Any]]:
        """Read unacted signals from DB and open live positions."""
        unacted: List[Signal] = await self._db.get_unacted_signals()
        if not unacted:
            logger.debug("No unacted signals to process")
            return []

        state = _load_state()
        open_keys = {
            (p.get("market_id", ""), p.get("wallet_followed", ""))
            for p in state["positions"]
        }

        opened: List[Dict[str, Any]] = []
        for signal in unacted:
            # Mark acted regardless of outcome to avoid re-processing
            await self._db.mark_signal_acted_on(signal.id)

            pos = await self._open_position(signal, state, open_keys)
            if pos:
                opened.append(pos)
                open_keys.add((signal.market_id, signal.wallet_address))

        if opened:
            _save_state(state)
        return opened

    async def _open_position(
        self,
        signal: Signal,
        state: Dict[str, Any],
        open_keys: set,
    ) -> Optional[Dict[str, Any]]:
        """Attempt to open one live position from a signal. Returns position dict or None."""
        market_id = signal.market_id
        wallet = signal.wallet_address
        entry_price = float(signal.price)

        # Block unwanted market types
        # (title not stored on Signal — skip keyword check here, signal engine handles it)

        if entry_price <= 0 or entry_price >= 1.0:
            logger.debug("Skipping signal {}: invalid price {:.4f}", signal.id, entry_price)
            return None

        # Live entry price filter — only copy signals in the configured R:R window
        if not (settings.live_min_entry_price <= entry_price <= settings.live_max_entry_price):
            logger.debug(
                "Skipping signal {}: price {:.4f} outside live range [{:.2f}, {:.2f}]",
                signal.id, entry_price, settings.live_min_entry_price, settings.live_max_entry_price,
            )
            return None

        if (market_id, wallet) in open_keys:
            logger.debug("Skipping signal {}: already have open position for market+wallet", signal.id)
            return None

        # 24-hour cooldown after a recent close on same market
        cutoff = datetime.now(timezone.utc) - timedelta(days=1)
        for t in state.get("trade_history", []):
            try:
                closed_at = datetime.fromisoformat(t.get("closed_at", ""))
                if closed_at.tzinfo is None:
                    closed_at = closed_at.replace(tzinfo=timezone.utc)
                if closed_at >= cutoff and t.get("market_id") == market_id:
                    logger.debug("Skipping signal {}: market {} closed within last 24h", signal.id, market_id[:16])
                    return None
            except (ValueError, TypeError):
                pass

        if len(state["positions"]) >= settings.max_live_positions:
            logger.info(
                "Skipping signal {}: max live positions reached ({}/{})",
                signal.id, len(state["positions"]), settings.max_live_positions,
            )
            return None

        # Size: use signal recommendation capped at max_live_bet_usd
        size = min(float(signal.recommended_size_usd), settings.max_live_bet_usd)
        size = round(size, 2)
        if size < 1.0:
            logger.debug("Skipping signal {}: position size ${:.2f} too small", signal.id, size)
            return None
        if size > state["balance"]:
            logger.info(
                "Skipping signal {}: insufficient balance ${:.2f} < ${:.2f}",
                signal.id, state["balance"], size,
            )
            return None

        # Build client and resolve token
        try:
            client = _build_clob_client()
        except RuntimeError as exc:
            logger.error("Cannot place live order — client error: {}", exc)
            return None

        outcome = signal.notes.split("outcome=")[-1].strip() if "outcome=" in (signal.notes or "") else "Yes"
        token_id = _resolve_token_id(client, market_id, outcome)
        if not token_id:
            # Fallback: try with asset_id directly as token_id
            if signal.asset_id:
                token_id = signal.asset_id
                logger.debug("Using asset_id as token_id for signal {}", signal.id)
            else:
                logger.warning("Cannot resolve token_id for signal {} market={}", signal.id, market_id[:16])
                return None

        limit_price = round(min(entry_price * (1 + settings.live_clob_slippage), 0.99), 4)

        try:
            from py_clob_client_v2.clob_types import MarketOrderArgsV2, OrderType
            order_args = MarketOrderArgsV2(
                token_id=token_id,
                amount=size,
                side="BUY",
                price=limit_price,
                order_type=OrderType.FOK,
                user_usdc_balance=state["balance"],
            )
            resp = client.create_and_post_market_order(order_args, order_type=OrderType.FOK)
            order_id = resp.get("orderID", resp.get("id", "")) if isinstance(resp, dict) else ""
        except Exception as exc:
            logger.error("Order placement failed for signal {} market={}: {}", signal.id, market_id[:16], exc)
            return None

        shares = round(size / limit_price, 4) if limit_price > 0 else 0.0

        position: Dict[str, Any] = {
            "id": f"live_{state['total_trades'] + 1}_{int(datetime.now(timezone.utc).timestamp())}",
            "order_id": order_id,
            "signal_id": signal.id,
            "token_id": token_id,
            "market_id": market_id,
            "wallet_followed": wallet,
            "outcome": outcome,
            "entry_price": round(limit_price, 4),
            "size": size,
            "shares": shares,
            "price_bucket": signal.price_bucket,
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "status": "open",
        }

        state["positions"].append(position)
        state["balance"] = round(state["balance"] - size, 2)
        state["total_trades"] += 1

        logger.info(
            "LIVE BUY: market={} outcome={} @ {:.4f} | ${:.2f} | order_id={}",
            market_id[:16], outcome, limit_price, size,
            order_id[:12] if order_id else "?",
        )
        return position

    async def check_positions(self) -> List[Dict[str, Any]]:
        """Fetch current prices and close positions that hit SL/TP/resolution."""
        state = _load_state()
        if not state["positions"]:
            return []

        # Fetch current prices for all open positions via scanner's CLOB client
        from src.api.clob_client import ClobClient as ScannerClobClient
        prices: Dict[str, float] = {}
        async with ScannerClobClient() as clob:
            for pos in state["positions"]:
                token_id = pos.get("token_id", "")
                if not token_id:
                    continue
                try:
                    ask = await clob.get_best_ask(token_id)
                    if ask is not None:
                        prices[pos["id"]] = ask
                except Exception as exc:
                    logger.debug("Price fetch failed for position {}: {}", pos["id"], exc)

        closed: List[Dict[str, Any]] = []
        for pos in list(state["positions"]):
            result = self._check_one_position(pos, prices, state)
            if result:
                closed.append(result)

        if closed:
            _save_state(state)
        return closed

    def _check_one_position(
        self,
        pos: Dict[str, Any],
        prices: Dict[str, float],
        state: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Evaluate one position. Removes from state["positions"] if closed. Returns trade record."""
        pos_id = pos["id"]
        entry_price = pos["entry_price"]

        # Age-based expiry
        try:
            opened_at = datetime.fromisoformat(pos.get("opened_at", ""))
            if opened_at.tzinfo is None:
                opened_at = opened_at.replace(tzinfo=timezone.utc)
            age_days = (datetime.now(timezone.utc) - opened_at).days
            if age_days >= settings.live_max_position_age_days:
                current = prices.get(pos_id, entry_price)
                return self._close_position(pos, current, "expired", state)
        except (ValueError, TypeError):
            pass

        if pos_id not in prices:
            return None

        current = prices[pos_id]

        # Resolution
        if current >= 0.99 or current <= 0.01:
            return self._close_position(pos, current, "resolved", state)

        # Stop-loss (only for entries >= live_stop_loss_min_entry)
        if entry_price >= settings.live_stop_loss_min_entry:
            loss_pct = (entry_price - current) / entry_price if entry_price > 0 else 0
            if loss_pct >= settings.live_stop_loss_pct:
                return self._close_position(pos, current, "stop_loss", state)

        # Take-profit: low-entry positions held to 0.80
        if entry_price < 0.30 and current >= settings.live_take_profit_price:
            return self._close_position(pos, current, "take_profit", state)

        # Take-profit: mid-range entries — percentage gain
        if entry_price >= 0.30:
            gain_pct = (current - entry_price) / entry_price if entry_price > 0 else 0
            if gain_pct >= settings.live_take_profit_gain_pct:
                return self._close_position(pos, current, "take_profit", state)

        return None

    def _close_position(
        self,
        pos: Dict[str, Any],
        exit_price: float,
        reason: str,
        state: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Record a position close. For non-resolved positions, attempt a sell order first.
        Mutates state in-place (removes from positions, updates balance).
        """
        pos_id = pos["id"]
        shares = pos.get("shares", 0.0)
        token_id = pos.get("token_id", "")
        is_resolved = exit_price >= 0.99 or exit_price <= 0.01

        if not is_resolved and shares > 0 and token_id:
            try:
                client = _build_clob_client()
                from py_clob_client_v2.clob_types import MarketOrderArgsV2, OrderType
                sell_price = round(exit_price * (1 - settings.live_clob_slippage), 4)
                order_args = MarketOrderArgsV2(
                    token_id=token_id,
                    amount=shares,
                    side="SELL",
                    price=sell_price,
                    order_type=OrderType.FOK,
                )
                resp = client.create_and_post_market_order(order_args, order_type=OrderType.FOK)
                logger.info("LIVE SELL placed for {}: resp={}", pos_id, resp)
            except Exception as exc:
                logger.error("Sell order failed for {} ({}): {} — recording at price anyway", pos_id, reason, exc)

        state["positions"] = [p for p in state["positions"] if p["id"] != pos_id]

        pnl = round(shares * (exit_price - pos["entry_price"]), 4)
        proceeds = round(shares * exit_price, 2)
        state["balance"] = round(state["balance"] + proceeds, 2)
        state["total_realized_pnl"] = round(state["total_realized_pnl"] + pnl, 4)

        if pnl > 0:
            state["winning_trades"] += 1
        elif pnl < 0:
            state["losing_trades"] += 1

        record = {
            **pos,
            "exit_price": round(exit_price, 4),
            "pnl": pnl,
            "close_reason": reason,
            "closed_at": datetime.now(timezone.utc).isoformat(),
            "status": "closed",
        }
        state["trade_history"].append(record)

        logger.info(
            "LIVE CLOSE: {} | {} @ {:.4f} → {:.4f} | PnL: ${:+.4f} | balance: ${:.2f}",
            reason, pos["market_id"][:16], pos["entry_price"], exit_price, pnl, state["balance"],
        )
        return record

    def get_portfolio_summary(self) -> Dict[str, Any]:
        state = _load_state()
        positions = state["positions"]
        history = state["trade_history"]

        total_closed = state["winning_trades"] + state["losing_trades"]
        win_rate = state["winning_trades"] / total_closed if total_closed > 0 else 0.0

        return {
            "balance": state["balance"],
            "initial_balance": state["initial_balance"],
            "realized_pnl": state["total_realized_pnl"],
            "total_trades": state["total_trades"],
            "winning_trades": state["winning_trades"],
            "losing_trades": state["losing_trades"],
            "win_rate": round(win_rate, 4),
            "open_positions": len(positions),
            "positions": positions,
            "trade_history": history,
            "last_updated": state.get("last_updated", ""),
        }

    def print_status(self) -> None:
        s = self.get_portfolio_summary()
        print(f"\n{'='*60}")
        print("LIVE TRADING STATUS")
        print(f"{'='*60}")
        print(f"  Balance:        ${s['balance']:.2f}")
        print(f"  Initial:        ${s['initial_balance']:.2f}")
        print(f"  Realized P&L:   ${s['realized_pnl']:+.4f}")
        print(f"  Open positions: {s['open_positions']}")
        print(f"  Total trades:   {s['total_trades']}")
        print(f"  Win rate:       {s['win_rate']:.1%} ({s['winning_trades']}W / {s['losing_trades']}L)")
        print(f"  Last updated:   {s['last_updated']}")
        if s["positions"]:
            print(f"\n  OPEN POSITIONS:")
            for p in s["positions"]:
                print(
                    f"    {p['id']}  market={p['market_id'][:16]}  "
                    f"entry={p['entry_price']:.4f}  size=${p['size']:.2f}"
                )
        if s["trade_history"]:
            print(f"\n  RECENT CLOSED (last 5):")
            for t in s["trade_history"][-5:]:
                print(
                    f"    {t.get('close_reason','?'):12s}  market={t['market_id'][:16]}  "
                    f"pnl=${t['pnl']:+.4f}"
                )
        print(f"{'='*60}\n")


async def init_live_balance_from_clob() -> Optional[float]:
    """
    Query the CLOB API for the wallet's current USDC balance.
    Returns None if credentials are missing or query fails.
    """
    try:
        from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams
        client = _build_clob_client()
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        resp = client.get_balance_allowance(params=params)
        raw = None
        if isinstance(resp, dict):
            raw = resp.get("balance", resp.get("usdc"))
        elif isinstance(resp, (int, float, str)):
            raw = resp
        if raw is not None:
            # USDC on Polygon has 6 decimals — CLOB returns raw integer string
            return float(raw) / 1_000_000
    except Exception as exc:
        logger.debug("Could not fetch live USDC balance: {}", exc)
    return None
