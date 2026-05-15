#!/usr/bin/env python3
"""
Send a FastCopier portfolio summary to Telegram.
Usage: python fast_report.py [--bankroll 750]
"""
import argparse
import sqlite3
import urllib.request
import urllib.parse
import os
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_MAX_TRADES_IN_MSG = 4096  # Telegram message character limit


def send_telegram(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode()
    urllib.request.urlopen(url, data=data, timeout=10)


def load_env() -> dict:
    env = {}
    env_path = _ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        if k in os.environ:
            env[k] = os.environ[k]
    return env


def shorten(addr: str, n: int = 6) -> str:
    """0xabcdef1234... → 0xabcdef"""
    if not addr:
        return "—"
    return addr[:2 + n] + "…"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--bankroll", type=float, default=750.0)
    p.add_argument("--fast-db", default=str(_ROOT / "fast_copier.db"))
    p.add_argument("--closed-limit", type=int, default=30,
                   help="Max closed trades shown in detail table")
    args = p.parse_args()

    env = load_env()
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = env.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        print("ERROR: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set in .env")
        return

    db_path = args.fast_db
    if not Path(db_path).exists():
        print(f"ERROR: DB not found at {db_path}")
        return

    con = sqlite3.connect(db_path)
    cur = con.cursor()

    # ── Summary row ──────────────────────────────────────────────────────────
    cur.execute("""
        SELECT
            COUNT(*) FILTER (WHERE status='OPEN')                        AS open_trades,
            COALESCE(SUM(dollar_amount) FILTER (WHERE status='OPEN'), 0) AS invested,
            COUNT(*) FILTER (WHERE status LIKE 'CLOSED%')                AS closed_trades,
            COALESCE(SUM(pnl) FILTER (WHERE status LIKE 'CLOSED%'), 0)   AS realised_pnl,
            COUNT(*) FILTER (WHERE status LIKE 'CLOSED%' AND pnl > 0)    AS profitable,
            COUNT(*) FILTER (WHERE status LIKE 'CLOSED%' AND pnl <= 0)   AS unprofitable,
            MIN(opened_at)                                                AS first_trade
        FROM fast_trades
    """)
    row = cur.fetchone()
    open_trades, invested, closed_trades, realised_pnl, profitable, unprofitable, first_trade = row

    # ── Closed trades detail (most recent first) ─────────────────────────────
    cur.execute("""
        SELECT wallet_followed, market_id, entry_price, exit_price, pnl
        FROM fast_trades
        WHERE status LIKE 'CLOSED%'
        ORDER BY closed_at DESC
        LIMIT ?
    """, (args.closed_limit,))
    closed_rows = cur.fetchall()
    con.close()

    # ── Build summary block ───────────────────────────────────────────────────
    portfolio_value = args.bankroll + realised_pnl
    roi_pct = (realised_pnl / args.bankroll) * 100
    win_rate = (profitable / closed_trades * 100) if closed_trades > 0 else 0.0

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    since = first_trade[:16].replace("T", " ") + " UTC" if first_trade else "—"

    pnl_sign = "+" if realised_pnl >= 0 else ""
    roi_sign = "+" if roi_pct >= 0 else ""

    summary = (
        f"<b>⚡ FastCopier Report</b> — {now}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📂 Open positions:    <b>{open_trades}</b>  (${invested:,.2f} deployed)\n"
        f"✅ Closed trades:     <b>{closed_trades}</b>  ({profitable}✓ / {unprofitable}✗,  {win_rate:.0f}% hit rate)\n"
        f"\n"
        f"💰 Realised P&amp;L:     <b>{pnl_sign}${realised_pnl:,.2f}</b>  ({roi_sign}{roi_pct:.2f}%)\n"
        f"🏦 Portfolio value:   <b>${portfolio_value:,.2f}</b>  (started ${args.bankroll:,.0f})\n"
        f"\n"
        f"🕐 Running since {since}"
    )

    # ── Build closed trades table ─────────────────────────────────────────────
    if closed_rows:
        header = "\n\n<b>📋 Closed Trades</b> (most recent first)\n"
        header += "<code>Wallet   │ Market  │  Buy  │  Sell │   RR</code>\n"
        header += "<code>─────────┼─────────┼───────┼───────┼──────</code>\n"

        lines = []
        for wallet, market, buy, sell, pnl_val in closed_rows:
            w = shorten(wallet, 6)
            m = shorten(market, 6)
            buy_s = f"{buy:.4f}"
            sell_s = f"{sell:.4f}" if sell is not None else "open"
            if sell is not None and buy > 0:
                rr = sell / buy
                rr_s = f"{rr:5.1f}x"
            else:
                rr_s = "  — "
            lines.append(f"<code>{w:9s}│ {m:7s} │{buy_s:>7s}│{sell_s:>7s}│{rr_s:>6s}</code>")

        trades_block = header + "\n".join(lines)
        # Trim if combined message would exceed Telegram limit
        combined = summary + trades_block
        if len(combined) > 4000:
            max_lines = max(1, (4000 - len(summary) - len(header)) // 75)
            trades_block = header + "\n".join(lines[:max_lines])
            trades_block += f"\n<i>…and {len(lines) - max_lines} more</i>"

        full_msg = summary + trades_block
    else:
        full_msg = summary

    send_telegram(token, chat_id, full_msg)
    print(full_msg)


if __name__ == "__main__":
    main()
