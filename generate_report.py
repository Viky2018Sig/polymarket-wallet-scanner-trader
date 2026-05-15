"""Generate complete trades report with live prices."""
import asyncio
import aiosqlite
from src.api.clob_client import ClobClient


async def main():
    async with aiosqlite.connect("polymarket_scanner.db") as db:
        async with db.execute(
            """SELECT id, wallet_followed, entry_price, shares, dollar_amount,
                      exit_price, pnl, status, opened_at, closed_at
               FROM paper_trades WHERE status != 'OPEN' ORDER BY id"""
        ) as c:
            closed = await c.fetchall()

        async with db.execute(
            """SELECT id, wallet_followed, asset_id, entry_price, shares, dollar_amount, opened_at
               FROM paper_trades WHERE status = 'OPEN' ORDER BY id"""
        ) as c:
            open_trades = await c.fetchall()

    # Fetch live prices for all open positions
    async with ClobClient() as clob:
        open_rows = []
        for r in open_trades:
            tid, wallet, asset_id, entry, shares, cost, opened = r
            ask = await clob.get_best_ask(asset_id) if asset_id else None
            entry_f = float(entry)
            shares_f = float(shares)
            unreal = (ask - entry_f) * shares_f if ask is not None else 0.0
            ret = (ask - entry_f) / entry_f * 100 if ask is not None else 0.0
            open_rows.append((tid, wallet[:10], entry_f, ask or 0.0, unreal, ret, float(cost), opened[:16]))

    open_rows.sort(key=lambda x: x[4], reverse=True)

    total_closed_pnl = sum(float(r[6]) for r in closed if r[6])
    total_unreal = sum(r[4] for r in open_rows)

    SEP = "=" * 100
    sep = "-" * 100

    lines = [
        SEP,
        "PAPER TRADING — COMPLETE REPORT (live prices)",
        SEP,
        "",
        f"CLOSED TRADES ({len(closed)})",
        f"  {'#':>4}  {'Wallet':12}  {'Entry':>7}  {'Exit':>7}  {'P&L':>9}  {'Result':6}  {'Opened':16}  {'Closed':16}",
        "  " + sep,
    ]

    for r in closed:
        tid, wallet, entry, shares, cost, exit_p, pnl, status, opened, closed_at = r
        pnl_f = float(pnl) if pnl else 0.0
        result = "WIN" if "WIN" in status else "LOSS"
        exit_f = float(exit_p) if exit_p else 0.0
        closed_s = (closed_at or "")[:16]
        lines.append(
            f"  {tid:>4}  {wallet[:10]:12}  {float(entry):>7.4f}  {exit_f:>7.4f}"
            f"  {pnl_f:>+9.2f}  {result:6}  {opened[:16]}  {closed_s}"
        )

    lines += [
        f"  {'':>4}  {'':12}  {'':>7}  {'TOTAL':>7}  {total_closed_pnl:>+9.2f}",
        "",
        f"OPEN POSITIONS ({len(open_trades)}) — sorted by unrealised P&L",
        f"  {'#':>4}  {'Wallet':12}  {'Entry':>7}  {'Now':>7}  {'Unreal':>9}  {'Return':>8}  {'Cost':>7}  {'Opened':16}",
        "  " + sep,
    ]

    for tid, wallet, entry_f, ask, unreal, ret, cost_f, opened in open_rows:
        lines.append(
            f"  {tid:>4}  {wallet:12}  {entry_f:>7.4f}  {ask:>7.4f}"
            f"  {unreal:>+9.2f}  {ret:>+7.0f}%  {cost_f:>7.2f}  {opened}"
        )

    lines += [
        f"  {'':>4}  {'':12}  {'':>7}  {'TOTAL':>7}  {total_unreal:>+9.2f}",
        "",
        SEP,
        "PORTFOLIO SUMMARY",
        SEP,
        f"  Starting bankroll  : $2,000.00",
        f"  Realised P&L       : ${total_closed_pnl:+.2f}",
        f"  Current bankroll   : ${2000 + total_closed_pnl:.2f}",
        f"  Unrealised P&L     : ${total_unreal:+.2f}",
        f"  TOTAL PORTFOLIO    : ${2000 + total_closed_pnl + total_unreal:.2f}",
        SEP,
    ]

    report = "\n".join(lines)
    with open("trades_report.txt", "w") as f:
        f.write(report)

    print(report)
    print("\n[Saved to trades_report.txt]")


asyncio.run(main())
