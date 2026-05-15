"""
FastCopier entry point — sub-second trade copying without G3 price-ceiling guard.

How it's different from the main realtime monitor:
  • No G3 block  — copies at the WS price directly; never checks current ask.
  • No REST wallet lookup — wallet resolved from pre-built asset_id map in O(1).
  • No signals table — opens paper trades immediately in fast_copier.db.
  • Activity poller catches brand-new markets within ~5s as a fallback.

Requirements:
  • The main scanner must have run at least once so wallet_trades exist in
    polymarket_scanner.db (needed to build the asset_id map at startup).
  • websockets library:  pip install websockets

Usage:
  python fast_copier.py                         # all defaults
  python fast_copier.py --max-price 0.15        # only copy under 0.15
  python fast_copier.py --bankroll 3733         # set starting bankroll
  python fast_copier.py --scanner-db /path/to/polymarket_scanner.db
  python fast_copier.py --fast-db /path/to/fast_copier.db
  python fast_copier.py --log-level DEBUG
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from loguru import logger

# Allow running from any directory
_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "src"))

from src.trader.fast_copier import FastCopier


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="FastCopier — sub-second Polymarket trade copy bot",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--scanner-db",
        default=str(_ROOT / "polymarket_scanner.db"),
        help="Path to main scanner DB (read-only; source of wallet scores & asset IDs)",
    )
    p.add_argument(
        "--fast-db",
        default=str(_ROOT / "fast_copier.db"),
        help="Path to fast copier DB (written by this script)",
    )
    p.add_argument(
        "--max-price",
        type=float,
        default=0.20,
        help="Only copy trades at or below this price",
    )
    p.add_argument(
        "--bankroll",
        type=float,
        default=2000.0,
        help="Starting bankroll in USD (for Kelly sizing)",
    )
    p.add_argument(
        "--max-pos-pct",
        type=float,
        default=0.0025,
        help="Max position size as fraction of bankroll (hard cap)",
    )
    p.add_argument(
        "--kelly",
        type=float,
        default=0.25,
        help="Kelly safety multiplier (0.25 = quarter Kelly)",
    )
    p.add_argument(
        "--lookback-days",
        type=int,
        default=14,
        help="Days of wallet_trades history used to build initial asset_id map",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    p.add_argument(
        "--log-file",
        default=str(_ROOT / "logs" / "fast_copier.log"),
        help="Log file path (set to '' to disable file logging)",
    )
    p.add_argument(
        "--live",
        action="store_true",
        default=False,
        help="Enable live CLOB order placement (paper-only if not set)",
    )
    p.add_argument(
        "--live-env",
        default="/root/copybot-live/polymarket-copybot/.env",
        help="Path to .env with live trading credentials (PK, CLOB_API_KEY, etc.)",
    )
    p.add_argument(
        "--live-max-bet",
        type=float,
        default=2.0,
        help="Max USD per single live order",
    )
    p.add_argument(
        "--live-slippage",
        type=float,
        default=0.005,
        help="Slippage buffer on limit price (0.005 = 0.5%%)",
    )
    p.add_argument(
        "--profit-lock-at",
        type=float,
        default=2.0,
        help="Activate trailing stop when bid reaches N× entry price (0 = disabled)",
    )
    p.add_argument(
        "--profit-lock-trail",
        type=float,
        default=0.50,
        help="Sell when bid drops to this fraction of peak (0.50 = 50%% of peak)",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()

    logger.remove()
    logger.add(
        sys.stderr,
        level=args.log_level,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
            "<level>{level:8}</level> | {message}"
        ),
    )
    if args.log_file:
        Path(args.log_file).parent.mkdir(parents=True, exist_ok=True)
        logger.add(
            args.log_file,
            level="DEBUG",
            rotation="50 MB",
            retention="7 days",
            format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:8} | {name}:{function} | {message}",
        )

    lock_str = (
        f"profit_lock=OFF" if args.profit_lock_at <= 0
        else f"profit_lock={args.profit_lock_at}x→trail@{args.profit_lock_trail:.0%}"
    )
    logger.info(
        f"FastCopier config — scanner_db={args.scanner_db} "
        f"fast_db={args.fast_db} max_price={args.max_price} "
        f"bankroll=${args.bankroll:,.2f} "
        f"live={'ON max_bet=$' + str(args.live_max_bet) if args.live else 'OFF (paper only)'} "
        f"{lock_str}"
    )

    copier = FastCopier(
        scanner_db_path=args.scanner_db,
        fast_db_path=args.fast_db,
        max_entry_price=args.max_price,
        starting_bankroll=args.bankroll,
        max_position_pct=args.max_pos_pct,
        kelly_fraction=args.kelly,
        lookback_days=args.lookback_days,
        live_mode=args.live,
        live_env_file=args.live_env,
        live_max_bet=args.live_max_bet,
        live_slippage=args.live_slippage,
        profit_lock_at=args.profit_lock_at,
        profit_lock_trail=args.profit_lock_trail,
    )

    try:
        asyncio.run(copier.run())
    except KeyboardInterrupt:
        logger.info("FastCopier stopped by user")


if __name__ == "__main__":
    main()
