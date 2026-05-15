"""
Polymarket Wallet Scanner & Paper Trader
========================================

Entry points:
  python main.py scan          # Discover and score wallets
  python main.py monitor       # Monitor wallets and generate signals
  python main.py paper-trade   # Process signals into paper positions
  python main.py dashboard     # Show rich terminal dashboard
  python main.py backtest      # Replay historical signals
  python main.py report        # Print performance report
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import List, Optional

import click
from loguru import logger

from config import settings


# ── Logging setup ──────────────────────────────────────────────────────────────

def _configure_logging(log_level: str, log_file: str) -> None:
    """Configure loguru with console + optional file sink."""
    logger.remove()
    logger.add(
        sys.stderr,
        level=log_level,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan> | "
            "<level>{message}</level>"
        ),
        colorize=True,
    )
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        logger.add(
            log_file,
            level=log_level,
            rotation="10 MB",
            retention="30 days",
            compression="gz",
            format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function} | {message}",
        )


# ── CLI ────────────────────────────────────────────────────────────────────────

@click.group()
@click.option("--log-level", default=settings.log_level, show_default=True,
              type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"]),
              help="Log verbosity level.")
@click.option("--log-file", default=settings.log_file, show_default=True,
              help="Path to log file (empty = stdout only).")
@click.pass_context
def cli(ctx: click.Context, log_level: str, log_file: str) -> None:
    """Polymarket Wallet Scanner & Paper Trader."""
    _configure_logging(log_level, log_file)
    ctx.ensure_object(dict)


# ── scan ──────────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--seed", multiple=True, metavar="ADDRESS",
              help="Seed wallet addresses (can be repeated).")
@click.option("--max-pages", default=200, show_default=True,
              help="Max CLOB trade pages to fetch for discovery.")
@click.option("--top-n", default=settings.max_tracked_wallets, show_default=True,
              help="Number of top wallets to mark as tracked.")
@click.option("--skip-discovery", is_flag=True, default=False,
              help="Skip CLOB discovery; score wallets already in DB.")
def scan(
    seed: List[str],
    max_pages: int,
    top_n: int,
    skip_discovery: bool,
) -> None:
    """Discover active wallets and score them by performance criteria."""
    asyncio.run(_run_scan(list(seed), max_pages, top_n, skip_discovery))


async def _run_scan(
    seed_addresses: List[str],
    max_pages: int,
    top_n: int,
    skip_discovery: bool,
) -> None:
    from src.api.data_client import DataApiClient
    from src.scanner.wallet_discovery import WalletDiscovery
    from src.scanner.pnl_calculator import PnlCalculator
    from src.scanner.performance import PerformanceScorer
    from src.storage.database import Database

    async with Database() as db, DataApiClient() as clob:
        discovery = WalletDiscovery(clob, db)
        scorer = PerformanceScorer(db)
        pnl_calc = PnlCalculator(db)

        if skip_discovery:
            logger.info("Skipping CLOB discovery, using wallets already in DB")
            addresses = await db.get_all_wallet_addresses()
        else:
            addresses = await discovery.discover(
                seed_addresses=seed_addresses,
                max_pages=max_pages,
            )

        if not addresses:
            logger.warning("No addresses found to score — add seed addresses or run discovery")
            return

        logger.info(f"Fetching & caching trades for {len(addresses)} wallets…")
        for i, addr in enumerate(addresses, 1):
            try:
                await discovery.fetch_and_cache_wallet_trades(addr)
                if i % 25 == 0:
                    logger.info(f"Progress: {i}/{len(addresses)} wallets fetched")
            except Exception as exc:
                logger.error(f"Failed to fetch trades for {addr[:10]}…: {exc}")

        logger.info("Calculating PnL via FIFO sell matching…")
        updated = await pnl_calc.run()
        logger.info(f"PnL calculation resolved {updated} trade rows")

        logger.info("Scoring all wallets…")
        all_scores = await scorer.score_all_wallets(addresses)

        # Persist all scores
        await scorer.save_scores(all_scores)
        qualified = [s for s in all_scores if s.qualifies]
        qualified.sort(key=lambda s: s.composite_score, reverse=True)

        logger.info(
            f"Scoring complete: {len(qualified)}/{len(all_scores)} wallets qualify"
        )

        # Mark top-N as tracked
        for i, ws in enumerate(qualified[:top_n]):
            await db.mark_wallet_tracked(ws.wallet_address, tracked=True)

        logger.info(f"Marked top {min(top_n, len(qualified))} wallets as tracked")

        # Print summary
        from rich.table import Table
        from rich.console import Console
        from rich import box
        cons = Console()
        t = Table(
            title=f"Top Qualified Wallets (showing {min(10, len(qualified))})",
            box=box.ROUNDED,
            header_style="bold green",
        )
        t.add_column("Rank", width=5, justify="right")
        t.add_column("Wallet", width=16)
        t.add_column("Score", width=7, justify="right")
        t.add_column("Prof.Factor", width=11, justify="right")
        t.add_column("Win Rate", width=9, justify="right")
        t.add_column("Low %", width=7, justify="right")
        t.add_column("Trades", width=7, justify="right")
        for rank, ws in enumerate(qualified[:10], 1):
            t.add_row(
                str(rank),
                f"{ws.wallet_address[:10]}…",
                f"{ws.composite_score:.3f}",
                f"{ws.profit_factor:.2f}",
                f"{ws.win_rate:.1%}",
                f"{ws.low_price_pct:.1%}",
                str(ws.resolved_trades),
            )
        cons.print()
        cons.print(t)


# ── monitor ───────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--once", is_flag=True, default=False,
              help="Run a single scan cycle instead of continuous loop.")
def monitor(once: bool) -> None:
    """Monitor tracked wallets for new signals (30-min polling, legacy)."""
    asyncio.run(_run_monitor(once))


async def _run_monitor(once: bool) -> None:
    from decimal import Decimal
    from src.api.data_client import DataApiClient
    from src.trader.signals import SignalEngine
    from src.storage.database import Database

    async with Database() as db, DataApiClient() as clob:
        snapshot = await db.get_latest_snapshot()
        bankroll = Decimal(str(settings.starting_bankroll))
        if snapshot:
            bankroll = snapshot.bankroll

        engine = SignalEngine(clob_client=clob, db=db, bankroll=bankroll)

        if once:
            signals = await engine.run_once()
            logger.info(f"Monitor cycle complete: {len(signals)} signals generated")
        else:
            await engine.run_continuous()


# ── realtime ──────────────────────────────────────────────────────────────────

@cli.command()
def realtime() -> None:
    """
    Real-time wallet monitor — WebSocket mode (~200ms detection latency).

    Runs five concurrent workers:
      ws_worker         — CLOB WebSocket, receives last_trade_price events (~50ms)
      trade_fetcher     — REST /trades?asset_id=X per event, finds tracked maker
      signal_processor  — applies guardrails and writes signals to DB
      housekeep_worker  — prunes old DB rows every 6 hours
      unfollow_checker  — refreshes subscriptions and open-keys every 30 min

    Guardrails applied per signal:
      1. BUY in 0.01–0.15 price range
      2. Market has ≥ MIN_MARKET_SECONDS_REMAINING before close
      3. Current ask ≤ signal_price × MAX_COPY_PRICE_MULTIPLIER
    """
    asyncio.run(_run_realtime())


async def _run_realtime() -> None:
    from decimal import Decimal
    from src.api.data_client import DataApiClient
    from src.api.clob_client import ClobClient
    from src.trader.realtime_monitor import RealtimeMonitor
    from src.storage.database import Database

    logger.info(
        f"Starting realtime monitor — WebSocket mode "
        f"(ceiling={settings.max_copy_price_multiplier}×, "
        f"min_remaining={settings.min_market_seconds_remaining}s)"
    )

    async with Database() as db, DataApiClient() as data, ClobClient() as clob:
        snapshot = await db.get_latest_snapshot()
        bankroll = Decimal(str(settings.starting_bankroll))
        if snapshot:
            bankroll = snapshot.bankroll

        monitor = RealtimeMonitor(
            data_client=data,
            clob_client=clob,
            db=db,
            bankroll=bankroll,
        )
        await monitor.run()


# ── paper-trade ────────────────────────────────────────────────────────────────

@cli.command(name="paper-trade")
@click.option("--once", is_flag=True, default=False,
              help="Run a single cycle instead of continuous loop.")
@click.option("--interval", default=settings.scan_interval_minutes, show_default=True,
              type=int, help="Minutes between paper-trade cycles.")
def paper_trade(once: bool, interval: int) -> None:
    """Process signals into paper trades and resolve closed markets."""
    asyncio.run(_run_paper_trade(once, interval))


async def _run_paper_trade(once: bool, interval: int) -> None:
    from src.api.gamma_client import GammaClient
    from src.trader.paper_trader import PaperTrader
    from src.storage.database import Database

    async with Database() as db, GammaClient() as gamma:
        trader = PaperTrader(db=db, gamma_client=gamma)
        await trader.initialise()

        if once:
            await trader.run_paper_trade_cycle()
        else:
            logger.info(f"Paper trader running (cycle every {interval} min)")
            while True:
                try:
                    await trader.run_paper_trade_cycle()
                except Exception as exc:
                    logger.error(f"Paper trade cycle error: {exc}")
                await asyncio.sleep(interval * 60)


# ── dashboard ─────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--live", is_flag=True, default=False,
              help="Auto-refresh dashboard every 30 seconds.")
@click.option("--refresh", default=30, show_default=True,
              help="Refresh interval in seconds (with --live).")
def dashboard(live: bool, refresh: int) -> None:
    """Display the rich terminal dashboard."""
    asyncio.run(_run_dashboard(live, refresh))


async def _run_dashboard(live_mode: bool, refresh_interval: int) -> None:
    from src.reporting.dashboard import Dashboard
    from src.storage.database import Database

    async with Database() as db:
        dash = Dashboard(db)
        if live_mode:
            logger.info(f"Live dashboard mode (refresh every {refresh_interval}s) — Ctrl+C to exit")
            while True:
                try:
                    # Clear terminal
                    click.clear()
                    await dash.render()
                except KeyboardInterrupt:
                    break
                except Exception as exc:
                    logger.error(f"Dashboard render error: {exc}")
                await asyncio.sleep(refresh_interval)
        else:
            await dash.render()


# ── backtest ──────────────────────────────────────────────────────────────────

@cli.command()
def backtest() -> None:
    """Replay historical signals against paper trader logic."""
    asyncio.run(_run_backtest())


async def _run_backtest() -> None:
    from src.trader.paper_trader import Backtester
    from src.reporting.dashboard import print_performance_report
    from src.storage.database import Database

    async with Database() as db:
        bt = Backtester(db)
        result = await bt.run()
        print_performance_report(result)


# ── report ────────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Output raw JSON instead of formatted table.")
def report(as_json: bool) -> None:
    """Print performance report to stdout."""
    asyncio.run(_run_report(as_json))


async def _run_report(as_json: bool) -> None:
    from src.api.gamma_client import GammaClient
    from src.trader.paper_trader import PaperTrader
    from src.reporting.dashboard import print_performance_report
    from src.storage.database import Database
    import json

    async with Database() as db, GammaClient() as gamma:
        trader = PaperTrader(db=db, gamma_client=gamma)
        await trader.initialise()
        result = await trader.get_performance_report()

        if as_json:
            # Serialise Decimal-safe
            print(json.dumps(result, indent=2, default=str))
        else:
            print_performance_report(result)


# ── notify ────────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--test", is_flag=True, default=False,
              help="Send a test ping to verify Telegram is configured correctly.")
def notify(test: bool) -> None:
    """Fetch current performance and send it to Telegram."""
    asyncio.run(_run_notify(test))


async def _run_notify(test: bool) -> None:
    from src.api.gamma_client import GammaClient
    from src.trader.paper_trader import PaperTrader
    from src.reporting.telegram import TelegramNotifier
    from src.storage.database import Database

    notifier = TelegramNotifier()

    if not notifier.is_configured():
        logger.error(
            "Telegram is not configured. "
            "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in your .env file."
        )
        raise SystemExit(1)

    if test:
        from datetime import datetime
        ok = await notifier._send_message(
            f"<b>Polymarket Scanner — Test Ping</b>\n\n"
            f"Telegram is configured correctly.\n"
            f"<i>{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}</i>"
        )
        if ok:
            logger.info("Test message sent successfully")
        else:
            logger.error("Test message failed — check your BOT_TOKEN and CHAT_ID")
        return

    async with Database() as db, GammaClient() as gamma:
        trader = PaperTrader(db=db, gamma_client=gamma)
        await trader.initialise()
        report = await trader.get_performance_report()

    ok = await notifier.send_performance_report(report)
    if ok:
        logger.info("Performance report sent to Telegram")
    else:
        logger.error("Failed to send performance report to Telegram")
        raise SystemExit(1)


# ── live-trade ────────────────────────────────────────────────────────────────

@cli.command(name="live-trade")
@click.option("--once", is_flag=True, default=False,
              help="Run a single cycle instead of continuous loop.")
@click.option("--status", is_flag=True, default=False,
              help="Print portfolio status and exit (no trading).")
@click.option("--interval", default=settings.scan_interval_minutes, show_default=True,
              type=int, help="Minutes between live-trade cycles.")
def live_trade(once: bool, status: bool, interval: int) -> None:
    """Execute real CLOB orders from scanner signals (live trading)."""
    asyncio.run(_run_live_trade(once, status, interval))


async def _run_live_trade(once: bool, show_status: bool, interval: int) -> None:
    from src.trader.live_trader import LiveTrader, init_live_balance_from_clob
    from src.storage.database import Database

    async with Database() as db:
        trader = LiveTrader(db=db)

        if show_status:
            trader.print_status()
            return

        if once:
            await trader.run_live_trade_cycle()
        else:
            logger.info(
                f"Live trader running (cycle every {interval} min) — "
                f"credentials: {settings.live_env_file}"
            )
            balance = await init_live_balance_from_clob()
            if balance is not None:
                logger.info(f"Wallet USDC balance: ${balance:.4f}")
                if balance < 1.0:
                    logger.warning(
                        f"Wallet balance ${balance:.4f} is below minimum order size. "
                        "Deposit USDC to the proxy wallet before live trading."
                    )
            else:
                logger.warning("Could not fetch live wallet balance — check credentials")
            while True:
                try:
                    await trader.run_live_trade_cycle()
                except Exception as exc:
                    logger.error(f"Live trade cycle error: {exc}")
                await asyncio.sleep(interval * 60)


# ── prune ─────────────────────────────────────────────────────────────────────

@cli.command()
def prune() -> None:
    """Prune old DB rows to reclaim disk space, then VACUUM."""
    asyncio.run(_run_prune())


async def _run_prune() -> None:
    from src.storage.database import Database

    async with Database() as db:
        size_before = await db.db_size_bytes() / 1_048_576
        deleted = await db.prune_old_data(
            wallet_trades_retention_days=settings.wallet_trades_retention_days,
            signals_retention_days=settings.signals_retention_days,
        )
        size_after = await db.db_size_bytes() / 1_048_576
        logger.info(
            f"Prune complete — {size_before:.1f} MB → {size_after:.1f} MB "
            f"(saved {size_before - size_after:.1f} MB) | deleted: {deleted}"
        )


# ── init-db (utility) ─────────────────────────────────────────────────────────

@cli.command(name="init-db")
def init_db() -> None:
    """Initialise the SQLite database schema (run once on first use)."""
    asyncio.run(_run_init_db())


async def _run_init_db() -> None:
    from src.storage.database import Database
    async with Database() as db:
        logger.info(f"Database initialised at: {settings.database_path}")


if __name__ == "__main__":
    cli()
