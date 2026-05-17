sqlite3 fast_copier.db "
SELECT wallet_followed,
  COUNT(*) as trades,
  SUM(CASE WHEN status='CLOSED_WIN' THEN 1 ELSE 0 END) as wins,
  SUM(CASE WHEN status='CLOSED_LOSS' THEN 1 ELSE 0 END) as losses,
  ROUND(SUM(COALESCE(pnl,0)),2) as pnl
FROM fast_trades
WHERE opened_at > '2026-05-15T16:57:00'
GROUP BY wallet_followed ORDER BY pnl DESC;
"
