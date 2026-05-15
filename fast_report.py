#!/usr/bin/env python3
"""
Send a FastCopier portfolio summary to Telegram.
Usage: python fast_report.py [--bankroll 750]
"""
import argparse
import sqlite3
import urllib.request
import urllib.parse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent

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
    # .env values can be overridden by real environment
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        if k in os.environ:
            env[k] = os.environ[k]
    return env

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--bankroll", type=float, default=750.0)
    p.add_argument("--fast-db", default=str(_ROOT / "fast_copier.db"))
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
    con.close()

    open_trades, invested, closed_trades, realised_pnl, profitable, unprofitable, first_trade = row

    portfolio_value = args.bankroll + realised_pnl
    roi_pct = (realised_pnl / args.bankroll) * 100
    win_rate = (profitable / closed_trades * 100) if closed_trades > 0 else 0.0

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    since = first_trade[:16].replace("T", " ") + " UTC" if first_trade else "—"

    pnl_sign = "+" if realised_pnl >= 0 else ""
    roi_sign = "+" if roi_pct >= 0 else ""

    msg = (
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

    send_telegram(token, chat_id, msg)
    print(msg)

if __name__ == "__main__":
    main()
