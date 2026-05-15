"""
Telegram notification module.

Sends formatted performance reports to a Telegram chat via the Bot API.
Uses httpx (already a project dependency) with retry on transient errors.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, Optional

import httpx
from loguru import logger
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from config import settings


TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


class TelegramNotifier:
    """Send formatted messages to a Telegram chat via the Bot API."""

    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
    ) -> None:
        self._token = bot_token or settings.telegram_bot_token
        self._chat_id = chat_id or settings.telegram_chat_id

    def is_configured(self) -> bool:
        return bool(self._token and self._chat_id)

    async def send_performance_report(self, report: Dict[str, Any]) -> bool:
        """
        Format and send a performance report to Telegram.
        Returns True if sent successfully, False otherwise.
        """
        if not self.is_configured():
            logger.warning(
                "Telegram not configured — set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID"
            )
            return False

        message = _format_report(report)
        return await self._send_message(message, parse_mode="HTML")

    async def send_signal_alert(
        self,
        market_id: str,
        wallet_address: str,
        price: float,
        size_usd: float,
        price_bucket: str,
        kelly_fraction: float,
        profit_factor: float,
    ) -> bool:
        """Send a real-time signal alert."""
        if not self.is_configured():
            return False

        rr = (1.0 - price) / price if price > 0 else 0
        message = (
            f"<b>SIGNAL — New Low-Price Entry</b>\n\n"
            f"<b>Market:</b> <code>{market_id[:24]}…</code>\n"
            f"<b>Wallet:</b> <code>{wallet_address[:8]}…{wallet_address[-6:]}</code>\n"
            f"<b>Entry Price:</b> {price:.4f}\n"
            f"<b>R:R Ratio:</b> {rr:.1f}:1\n"
            f"<b>Bucket:</b> {price_bucket}\n"
            f"<b>Kelly Size:</b> ${size_usd:.2f} ({kelly_fraction:.2%})\n"
            f"<b>Wallet PF:</b> {profit_factor:.2f}\n\n"
            f"<i>{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}</i>"
        )
        return await self._send_message(message, parse_mode="HTML")

    async def send_unfollow_alert(
        self,
        wallet_address: str,
        recent_profit_factor: float,
        threshold: float,
    ) -> bool:
        """Notify when a wallet is unfollowed due to performance decay."""
        if not self.is_configured():
            return False

        message = (
            f"<b>UNFOLLOW — Wallet Performance Declined</b>\n\n"
            f"<b>Wallet:</b> <code>{wallet_address[:8]}…{wallet_address[-6:]}</code>\n"
            f"<b>Recent PF (last 10):</b> {recent_profit_factor:.2f}\n"
            f"<b>Threshold:</b> {threshold:.2f}\n\n"
            f"<i>{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}</i>"
        )
        return await self._send_message(message, parse_mode="HTML")

    @retry(
        retry=retry_if_exception_type((httpx.HTTPError, httpx.TimeoutException)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
    )
    async def _send_message(
        self,
        text: str,
        parse_mode: str = "HTML",
    ) -> bool:
        """Send a message via the Telegram Bot API."""
        url = TELEGRAM_API.format(token=self._token, method="sendMessage")
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code == 200 and resp.json().get("ok"):
                logger.debug("Telegram message sent successfully")
                return True
            else:
                logger.error(
                    f"Telegram API error {resp.status_code}: {resp.text[:200]}"
                )
                return False


def _format_report(report: Dict[str, Any]) -> str:
    """
    Format a performance report dict as an HTML Telegram message.
    Keeps under 4096 chars (Telegram message limit).
    """
    bankroll = report.get("bankroll", 0.0)
    starting = report.get("starting_bankroll", 0.0)
    total_pnl = report.get("total_pnl", 0.0)
    total_pnl_pct = report.get("total_pnl_pct", 0.0)
    unrealised_pnl = report.get("unrealised_pnl", 0.0)
    total_portfolio_value = report.get("total_portfolio_value", bankroll + unrealised_pnl)
    open_count = report.get("open_count", 0)
    closed_count = report.get("closed_count", 0)
    wins = report.get("wins", 0)
    losses = report.get("losses", 0)
    win_rate = report.get("win_rate", 0.0)
    profit_factor = report.get("profit_factor", 0.0)
    gross_profit = report.get("gross_profit", 0.0)
    gross_loss = report.get("gross_loss", 0.0)
    max_drawdown = report.get("max_drawdown", 0.0)
    tracked_wallets = report.get("tracked_wallets", 0)

    pnl_emoji = "📈" if total_pnl >= 0 else "📉"
    unr_emoji = "📈" if unrealised_pnl >= 0 else "📉"
    pf_emoji = "✅" if profit_factor >= 2 else ("⚠️" if profit_factor >= 1 else "❌")
    dd_emoji = "❌" if max_drawdown > 0.3 else ("⚠️" if max_drawdown > 0.15 else "✅")

    lines = [
        "🤖 <b>Polymarket Scanner — Hourly Update</b>",
        "",
        "💼 <b>Portfolio</b>",
        f"  Total Value:        <code>${total_portfolio_value:,.2f}</code>",
        f"  Starting Capital:   <code>${starting:,.2f}</code>",
        f"  Realised P&amp;L:   {pnl_emoji} <code>${total_pnl:+,.2f} ({total_pnl_pct:+.1%})</code>",
        f"  Bankroll (realised):<code>${bankroll:,.2f}</code>",
        f"  Unrealised P&amp;L: {unr_emoji} <code>${unrealised_pnl:+,.2f}</code>",
        "",
        "📊 <b>Performance</b>",
        f"  Closed Trades:  <b>{closed_count}</b>  ({wins}W / {losses}L)",
        f"  Win Rate:       <b>{win_rate:.1%}</b>",
        f"  Profit Factor:  {pf_emoji} <b>{profit_factor:.2f}</b>",
        f"  Gross Profit:   <code>+${gross_profit:,.2f}</code>",
        f"  Gross Loss:     <code>-${gross_loss:,.2f}</code>",
        f"  Max Drawdown:   {dd_emoji} <b>{max_drawdown:.1%}</b>",
        f"  Open Positions: <b>{open_count}</b>",
        f"  Tracked Wallets: <b>{tracked_wallets}</b>",
    ]

    # Top closed trades by PnL
    top_trades = report.get("top_trades", [])
    if top_trades:
        lines.append("")
        lines.append("🏆 <b>Top Trades by Profit</b>")
        for t in top_trades:
            market_slug = t["market_id"][2:14]  # strip 0x, take 12 chars
            wallet_short = f"{t['wallet'][:6]}…{t['wallet'][-4:]}"
            pnl = t["pnl"]
            entry = t["entry_price"]
            exit_p = t["exit_price"]
            lines.append(
                f"  <code>{market_slug}</code> | <code>{wallet_short}</code>\n"
                f"    Profit: <b>${pnl:+,.2f}</b>  "
                f"In: {entry:.4f} → Out: {exit_p:.4f}"
            )

    lines.append("")
    lines.append(f"<i>🕐 {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}</i>")

    return "\n".join(lines)
