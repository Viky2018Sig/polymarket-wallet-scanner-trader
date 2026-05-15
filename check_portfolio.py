cd /root/polymarket-wallet-scanner-trader && python3 -c "
import asyncio, sys
from decimal import Decimal
sys.path.insert(0, '.')
from src.storage.database import Database
from src.api.clob_client import ClobClient

async def main():
    async with Database() as db:
        # Realized stats
        rows = await db._conn.execute_fetchall('''
            SELECT 
              COUNT(*) FILTER (WHERE status != 'OPEN') as closed,
              COUNT(*) FILTER (WHERE status = 'CLOSED_WIN') as wins,
              COUNT(*) FILTER (WHERE status = 'CLOSED_LOSS') as losses,
              SUM(CAST(pnl AS REAL)) FILTER (WHERE status != 'OPEN') as total_pnl,
              SUM(CAST(pnl AS REAL)) FILTER (WHERE status = 'CLOSED_WIN') as gross_profit,
              SUM(CAST(pnl AS REAL)) FILTER (WHERE status = 'CLOSED_LOSS') as gross_loss
            FROM paper_trades
        ''')
        r = rows[0]
        print(f'=== REALIZED ===')
        print(f'Closed: {r[0]}  Wins: {r[1]}  Losses: {r[2]}')
        print(f'Win rate: {r[1]/r[0]*100:.1f}%')
        print(f'Total realised PnL: \${r[3]:.2f}')
        print(f'Gross profit: \${r[4]:.2f}  Gross loss: \${r[5]:.2f}')
        print(f'Profit factor: {abs(r[4]/r[5]):.2f}x')

        # Open positions details
        open_trades = await db.get_open_paper_trades()
        print(f'\n=== OPEN POSITIONS: {len(open_trades)} ===')
        
        total_cost = sum(float(t.dollar_amount) for t in open_trades)
        print(f'Total capital deployed: \${total_cost:.2f}')

    async with ClobClient() as clob:
        unrealised_pnl = 0.0
        for t in open_trades:
            if not t.asset_id:
                continue
            try:
                ask = await clob.get_best_ask(t.asset_id)
                if ask is not None:
                    pnl = (float(ask) - float(t.entry_price)) * float(t.shares)
                    unrealised_pnl += pnl
            except Exception:
                pass
        print(f'Unrealised PnL (at current ask): \${unrealised_pnl:.2f}')
        print(f'\n=== TOTAL PORTFOLIO ===')
        realised = r[3] or 0
        print(f'Starting bankroll:   \$2000.00')
        print(f'Realised PnL:        \${realised:.2f}')
        print(f'Unrealised PnL:      \${unrealised_pnl:.2f}')
        print(f'Total PnL:           \${realised + unrealised_pnl:.2f}')
        print(f'Portfolio value:     \${2000 + realised + unrealised_pnl:.2f}')

asyncio.run(main())
" 2>/dev/null
