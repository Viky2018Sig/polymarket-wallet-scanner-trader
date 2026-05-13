"""
Rich terminal dashboard for the Polymarket paper trader.

Displays:
- Portfolio summary (bankroll, PnL, win rate, profit factor)
- Top tracked wallets
- Open positions
- Recent signals feed
- Price bucket breakdown
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from rich import box
from rich.columns import Columns
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.rule import Rule

from src.api.models import PaperTrade, PaperTradeStatus, Signal, WalletScore
from src.storage.database import Database

console = Console()


class Dashboard:
    """Rich terminal dashboard renderer."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def render(self) -> None:
        """Render the full dashboard to terminal (one-shot)."""
        # Gather all data
        stats = await self._db.get_paper_trade_stats()
        snapshot = await self._db.get_latest_snapshot()
        tracked_wallets = await self._db.get_tracked_wallets()
        open_trades = await self._db.get_open_paper_trades()
        recent_signals = await self._db.get_recent_signals(limit=10)
        closed_trades = await self._db.get_closed_paper_trades(limit=20)

        # Compute bankroll
        from config import settings
        starting = Decimal(str(settings.starting_bankroll))
        total_pnl = Decimal(str(stats.get("total_pnl", 0)))
        bankroll = starting + total_pnl

        console.print()
        console.print(Rule("[bold cyan] Polymarket Wallet Scanner & Paper Trader [/bold cyan]"))
        console.print()

        # Portfolio summary
        console.print(_build_portfolio_panel(stats, bankroll, snapshot))
        console.print()

        # Top tracked wallets
        if tracked_wallets:
            console.print(_build_wallets_table(tracked_wallets))
            console.print()

        # Open positions
        if open_trades:
            console.print(_build_open_positions_table(open_trades))
            console.print()
        else:
            console.print(Panel("[dim]No open paper positions.[/dim]", title="Open Positions"))
            console.print()

        # Recent signals
        if recent_signals:
            console.print(_build_signals_table(recent_signals))
            console.print()

        # Price bucket breakdown
        all_trades = closed_trades + open_trades
        if all_trades:
            console.print(_build_bucket_breakdown(all_trades))
            console.print()

        # Recent closed trades
        if closed_trades:
            console.print(_build_closed_trades_table(closed_trades[:10]))
            console.print()

        console.print(
            f"[dim]Last updated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}[/dim]"
        )


def _build_portfolio_panel(
    stats: Dict[str, Any],
    bankroll: Decimal,
    snapshot: Optional[Any],
) -> Panel:
    """Build the portfolio summary panel."""
    from config import settings

    starting = Decimal(str(settings.starting_bankroll))
    total_pnl = Decimal(str(stats.get("total_pnl", 0)))
    pnl_pct = float(total_pnl / starting) if starting > 0 else 0.0

    win_rate = stats.get("win_rate", 0.0)
    pf = stats.get("profit_factor", 0.0)
    wins = stats.get("wins", 0)
    losses = stats.get("losses", 0)
    open_count = stats.get("open_count", 0)
    gross_profit = stats.get("gross_profit", 0.0)
    gross_loss = stats.get("gross_loss", 0.0)
    max_dd = snapshot.max_drawdown_to_date if snapshot else 0.0

    pnl_color = "green" if total_pnl >= 0 else "red"
    pnl_sign = "+" if total_pnl >= 0 else ""

    table = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    table.add_column("Metric", style="bold cyan", width=24)
    table.add_column("Value", width=20)
    table.add_column("Metric", style="bold cyan", width=24)
    table.add_column("Value", width=20)

    table.add_row(
        "Bankroll",
        f"[bold white]${float(bankroll):,.2f}[/bold white]",
        "Open Positions",
        f"[yellow]{open_count}[/yellow]",
    )
    table.add_row(
        "Starting Bankroll",
        f"${float(starting):,.2f}",
        "Closed Trades",
        f"{wins + losses}",
    )
    table.add_row(
        "Total P&L",
        f"[{pnl_color}]{pnl_sign}${float(total_pnl):,.2f} ({pnl_pct:+.1%})[/{pnl_color}]",
        "Win / Loss",
        f"[green]{wins}[/green] / [red]{losses}[/red]",
    )
    table.add_row(
        "Gross Profit",
        f"[green]${gross_profit:,.2f}[/green]",
        "Win Rate",
        f"[{'green' if win_rate >= 0.5 else 'yellow'}]{win_rate:.1%}[/]",
    )
    table.add_row(
        "Gross Loss",
        f"[red]${gross_loss:,.2f}[/red]",
        "Profit Factor",
        f"[{'green' if pf >= 2 else ('yellow' if pf >= 1 else 'red')}]{pf:.2f}[/]",
    )
    table.add_row(
        "",
        "",
        "Max Drawdown",
        f"[{'red' if max_dd > 0.2 else 'yellow'}]{max_dd:.1%}[/]",
    )

    return Panel(table, title="[bold]Portfolio Summary[/bold]", border_style="cyan")


def _build_wallets_table(wallets: List[WalletScore]) -> Panel:
    """Build the tracked wallets table."""
    table = Table(
        box=box.ROUNDED,
        show_lines=False,
        header_style="bold magenta",
        title=None,
    )
    table.add_column("#", width=3, justify="right")
    table.add_column("Wallet", width=14)
    table.add_column("Score", width=7, justify="right")
    table.add_column("Prof.Factor", width=11, justify="right")
    table.add_column("Win Rate", width=9, justify="right")
    table.add_column("Low-Price%", width=11, justify="right")
    table.add_column("Trades", width=7, justify="right")
    table.add_column("Max DD", width=8, justify="right")
    table.add_column("Last Trade", width=12)

    for i, w in enumerate(wallets[:20], 1):
        pf_color = "green" if w.profit_factor >= 2 else "yellow"
        wr_color = "green" if w.win_rate >= 0.5 else "yellow"
        dd_color = "red" if w.max_drawdown > 0.2 else "yellow"
        last = (
            w.last_trade_at.strftime("%Y-%m-%d")
            if w.last_trade_at
            else "—"
        )
        table.add_row(
            str(i),
            f"[dim]{w.wallet_address[:6]}…{w.wallet_address[-4:]}[/dim]",
            f"[bold]{w.composite_score:.3f}[/bold]",
            f"[{pf_color}]{w.profit_factor:.2f}[/{pf_color}]",
            f"[{wr_color}]{w.win_rate:.1%}[/{wr_color}]",
            f"{w.low_price_pct:.1%}",
            str(w.resolved_trades),
            f"[{dd_color}]{w.max_drawdown:.1%}[/{dd_color}]",
            last,
        )

    return Panel(
        table,
        title=f"[bold]Tracked Wallets[/bold] ({len(wallets)} total)",
        border_style="magenta",
    )


def _build_open_positions_table(trades: List[PaperTrade]) -> Panel:
    """Build the open positions table."""
    table = Table(box=box.ROUNDED, header_style="bold yellow")
    table.add_column("ID", width=5, justify="right")
    table.add_column("Market", width=16)
    table.add_column("Wallet", width=14)
    table.add_column("Entry", width=8, justify="right")
    table.add_column("Shares", width=10, justify="right")
    table.add_column("Amount", width=10, justify="right")
    table.add_column("Bucket", width=12)
    table.add_column("Kelly%", width=8, justify="right")
    table.add_column("Opened", width=12)

    for t in trades[:15]:
        table.add_row(
            str(t.id or "?"),
            f"[dim]{t.market_id[:14]}[/dim]",
            f"[dim]{t.wallet_followed[:6]}…{t.wallet_followed[-4:]}[/dim]",
            f"{float(t.entry_price):.4f}",
            f"{float(t.shares):.2f}",
            f"[bold]${float(t.dollar_amount):.2f}[/bold]",
            t.price_bucket,
            f"{t.kelly_fraction_used:.2%}",
            t.opened_at.strftime("%m-%d %H:%M"),
        )

    return Panel(
        table,
        title=f"[bold]Open Positions[/bold] ({len(trades)} total)",
        border_style="yellow",
    )


def _build_signals_table(signals: List[Signal]) -> Panel:
    """Build the recent signals table."""
    table = Table(box=box.ROUNDED, header_style="bold green")
    table.add_column("ID", width=5, justify="right")
    table.add_column("Type", width=6)
    table.add_column("Market", width=16)
    table.add_column("Wallet", width=14)
    table.add_column("Price", width=8, justify="right")
    table.add_column("Bucket", width=12)
    table.add_column("Size $", width=9, justify="right")
    table.add_column("Kelly%", width=8, justify="right")
    table.add_column("Time", width=12)

    for s in signals:
        sig_color = "green" if s.signal_type.value == "BUY" else "red"
        table.add_row(
            str(s.id or "?"),
            f"[{sig_color}]{s.signal_type.value}[/{sig_color}]",
            f"[dim]{s.market_id[:14]}[/dim]",
            f"[dim]{s.wallet_address[:6]}…{s.wallet_address[-4:]}[/dim]",
            f"{float(s.price):.4f}",
            s.price_bucket,
            f"${float(s.recommended_size_usd):.2f}",
            f"{s.kelly_fraction:.2%}",
            s.generated_at.strftime("%m-%d %H:%M"),
        )

    return Panel(
        table,
        title="[bold]Recent Signals[/bold]",
        border_style="green",
    )


def _build_bucket_breakdown(trades: List[PaperTrade]) -> Panel:
    """Build the price bucket breakdown panel."""
    from config import settings

    bucket_data: Dict[str, Dict[str, int]] = {}
    for lo, hi in settings.price_buckets:
        label = f"{lo:.2f}–{hi:.2f}"
        bucket_data[label] = {"total": 0, "wins": 0, "losses": 0, "open": 0}

    for t in trades:
        b = t.price_bucket.replace("-", "–")
        if b not in bucket_data:
            # try to map
            for label in bucket_data:
                if label.replace("–", "-") == t.price_bucket:
                    b = label
                    break
        if b not in bucket_data:
            continue
        bucket_data[b]["total"] += 1
        if t.status == PaperTradeStatus.CLOSED_WIN:
            bucket_data[b]["wins"] += 1
        elif t.status == PaperTradeStatus.CLOSED_LOSS:
            bucket_data[b]["losses"] += 1
        elif t.status == PaperTradeStatus.OPEN:
            bucket_data[b]["open"] += 1

    table = Table(box=box.SIMPLE, header_style="bold blue")
    table.add_column("Price Bucket", width=16)
    table.add_column("Total", width=7, justify="right")
    table.add_column("Wins", width=6, justify="right")
    table.add_column("Losses", width=7, justify="right")
    table.add_column("Open", width=6, justify="right")
    table.add_column("Win Rate", width=9, justify="right")
    table.add_column("Bar", width=22)

    total_trades = sum(d["total"] for d in bucket_data.values())

    for label, data in bucket_data.items():
        count = data["total"]
        wins = data["wins"]
        losses = data["losses"]
        open_c = data["open"]
        resolved = wins + losses
        wr = wins / resolved if resolved > 0 else 0.0
        pct = count / total_trades if total_trades > 0 else 0.0
        bar_len = int(pct * 20)
        bar = "█" * bar_len + "░" * (20 - bar_len)

        wr_color = "green" if wr >= 0.5 else ("yellow" if wr >= 0.3 else "red")

        table.add_row(
            label,
            str(count),
            f"[green]{wins}[/green]",
            f"[red]{losses}[/red]",
            f"[yellow]{open_c}[/yellow]",
            f"[{wr_color}]{wr:.1%}[/{wr_color}]" if resolved > 0 else "[dim]—[/dim]",
            f"[blue]{bar}[/blue] {pct:.1%}",
        )

    return Panel(
        table,
        title="[bold]Price Bucket Breakdown[/bold]",
        border_style="blue",
    )


def _build_closed_trades_table(trades: List[PaperTrade]) -> Panel:
    """Build recent closed trades table."""
    table = Table(box=box.ROUNDED, header_style="bold white")
    table.add_column("ID", width=5, justify="right")
    table.add_column("Result", width=8)
    table.add_column("Market", width=16)
    table.add_column("Entry", width=8, justify="right")
    table.add_column("Exit", width=8, justify="right")
    table.add_column("P&L", width=10, justify="right")
    table.add_column("P&L%", width=8, justify="right")
    table.add_column("Amount", width=9, justify="right")
    table.add_column("Closed", width=12)

    for t in trades:
        if t.status == PaperTradeStatus.CLOSED_WIN:
            res_str = "[green]WIN[/green]"
        elif t.status == PaperTradeStatus.CLOSED_LOSS:
            res_str = "[red]LOSS[/red]"
        else:
            res_str = "[dim]PARTIAL[/dim]"

        pnl = float(t.pnl or 0)
        pnl_pct = t.pnl_pct or 0.0
        pnl_color = "green" if pnl >= 0 else "red"
        pnl_sign = "+" if pnl >= 0 else ""

        table.add_row(
            str(t.id or "?"),
            res_str,
            f"[dim]{t.market_id[:14]}[/dim]",
            f"{float(t.entry_price):.4f}",
            f"{float(t.exit_price or 0):.4f}" if t.exit_price else "—",
            f"[{pnl_color}]{pnl_sign}${pnl:.2f}[/{pnl_color}]",
            f"[{pnl_color}]{pnl_sign}{pnl_pct:.1%}[/{pnl_color}]",
            f"${float(t.dollar_amount):.2f}",
            t.closed_at.strftime("%m-%d %H:%M") if t.closed_at else "—",
        )

    return Panel(
        table,
        title=f"[bold]Recent Closed Trades[/bold] (last {len(trades)})",
        border_style="white",
    )


def print_performance_report(report: Dict[str, Any]) -> None:
    """Print a text-based performance report to stdout."""
    console.print()
    console.print(Rule("[bold cyan]Performance Report[/bold cyan]"))
    console.print()

    bankroll = report.get("bankroll", 0)
    starting = report.get("starting_bankroll", 0)
    total_pnl = report.get("total_pnl", 0)
    pnl_pct = report.get("total_pnl_pct", 0)
    win_rate = report.get("win_rate", 0)
    pf = report.get("profit_factor", 0)
    wins = report.get("wins", 0)
    losses = report.get("losses", 0)
    max_dd = report.get("max_drawdown", 0)
    tracked = report.get("tracked_wallets", 0)

    pnl_color = "green" if total_pnl >= 0 else "red"

    summary = Table(box=box.SIMPLE, show_header=False)
    summary.add_column("Metric", style="cyan", width=28)
    summary.add_column("Value", width=20)

    summary.add_row("Starting Bankroll", f"${starting:,.2f}")
    summary.add_row("Current Bankroll", f"[bold white]${bankroll:,.2f}[/bold white]")
    summary.add_row(
        "Total P&L",
        f"[{pnl_color}]${total_pnl:+,.2f} ({pnl_pct:+.1%})[/{pnl_color}]",
    )
    summary.add_row("Win / Loss", f"[green]{wins}[/green] / [red]{losses}[/red]")
    summary.add_row("Win Rate", f"{win_rate:.1%}")
    summary.add_row("Profit Factor", f"{pf:.2f}")
    summary.add_row("Max Drawdown", f"{max_dd:.1%}")
    summary.add_row("Tracked Wallets", str(tracked))

    console.print(summary)

    # Wallet performance breakdown
    wallet_perf = report.get("wallet_performance", {})
    if wallet_perf:
        console.print()
        console.print("[bold]Per-Wallet Paper Performance:[/bold]")
        wt = Table(box=box.SIMPLE, header_style="bold magenta")
        wt.add_column("Wallet", width=14)
        wt.add_column("Trades", width=7, justify="right")
        wt.add_column("Wins", width=6, justify="right")
        wt.add_column("Win Rate", width=9, justify="right")
        wt.add_column("P&L", width=12, justify="right")

        for addr, data in sorted(
            wallet_perf.items(), key=lambda x: x[1]["pnl"], reverse=True
        ):
            t = data["trades"]
            w = data["wins"]
            p = data["pnl"]
            wr = w / t if t > 0 else 0
            p_color = "green" if p >= 0 else "red"
            wt.add_row(
                f"{addr[:6]}…{addr[-4:]}",
                str(t),
                str(w),
                f"{wr:.1%}",
                f"[{p_color}]${p:+,.2f}[/{p_color}]",
            )
        console.print(wt)

    # Bucket breakdown
    buckets = report.get("price_bucket_breakdown", {})
    if buckets:
        console.print()
        console.print("[bold]Price Bucket Breakdown:[/bold]")
        bt = Table(box=box.SIMPLE, header_style="bold blue")
        bt.add_column("Bucket", width=14)
        bt.add_column("Count", width=7, justify="right")
        for bucket, count in sorted(buckets.items()):
            bt.add_row(bucket, str(count))
        console.print(bt)

    console.print()
