"""
Fetch latest activity for all is_tracked=1 wallets and upsert into wallet_trades.
Runs hourly via cron to keep the asset_id map fresh for FastCopier.
"""
import asyncio, sqlite3, httpx
from datetime import datetime, timedelta

DB = "/root/polymarket-wallet-scanner-trader/polymarket_scanner.db"

async def fetch_activity(client, addr, limit=200):
    r = await client.get("https://data-api.polymarket.com/activity",
                         params={"user": addr, "limit": limit})
    d = r.json()
    return d if isinstance(d, list) else []

async def main():
    db = sqlite3.connect(DB)
    wallets = [r[0] for r in db.execute(
        "SELECT wallet_address FROM wallets WHERE is_tracked=1"
    ).fetchall()]
    print(f"{datetime.utcnow().isoformat()} — refreshing {len(wallets)} tracked wallets")

    async with httpx.AsyncClient(timeout=30) as client:
        tasks = [fetch_activity(client, w) for w in wallets]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    now_iso = datetime.utcnow().isoformat()
    total_new = 0
    for addr, trades in zip(wallets, results):
        if isinstance(trades, Exception) or not trades:
            continue
        inserted = 0
        for t in trades:
            tx = t.get("transactionHash") or t.get("id") or ""
            if not tx:
                continue
            ts = t.get("timestamp", 0)
            try:
                matched_at = datetime.utcfromtimestamp(int(ts)).isoformat() if ts else now_iso
            except Exception:
                matched_at = now_iso
            market_id = t.get("conditionId") or ""
            asset_id  = t.get("asset") or ""
            side      = (t.get("side") or "").upper()
            price     = str(t.get("price") or 0)
            size      = str(t.get("size") or 0)
            notional  = str(float(price) * float(size))
            outcome   = t.get("outcome") or ""
            if not market_id or not asset_id:
                continue
            try:
                db.execute(
                    "INSERT OR IGNORE INTO wallet_trades "
                    "(trade_id,wallet_address,market_id,asset_id,side,price,size,notional_usd,matched_at,outcome) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (tx, addr, market_id, asset_id, side, price, size, notional, matched_at, outcome)
                )
                inserted += 1
            except Exception:
                pass
        db.commit()
        total_new += inserted
        print(f"  {addr[:14]}  +{inserted} trades")

    print(f"Done — {total_new} new trade rows inserted")
    db.close()

asyncio.run(main())
