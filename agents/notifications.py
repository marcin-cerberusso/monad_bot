"""
🔔 NOTIFICATION SERVICE - Obsługa powiadomień Discord/Telegram
"""
import aiohttp
import logging
import os
import asyncio
from typing import Optional
from datetime import datetime

from .config import DISCORD_WEBHOOK_URL, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, setup_logging

logger = setup_logging("Notifications")

class NotificationService:
    """Serwis do wysyłania powiadomień"""
    
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self.discord_enabled = bool(DISCORD_WEBHOOK_URL)
        self.telegram_enabled = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
        
        if self.discord_enabled:
            logger.info("✅ Discord notifications enabled")
        if self.telegram_enabled:
            logger.info("✅ Telegram notifications enabled")
            
    async def start(self):
        """Start sesji HTTP"""
        if not self.session:
            self.session = aiohttp.ClientSession()
            
    async def stop(self):
        """Zamknięcie sesji HTTP"""
        if self.session:
            await self.session.close()
            self.session = None

    async def send_alert(self, title: str, message: str, color: int = 0xFF0000):
        """
        Wyślij alert do wszystkich skonfigurowanych kanałów.
        
        Args:
            title: Tytuł alertu
            message: Treść wiadomości
            color: Kolor dla embeda Discord (domyślnie czerwony dla błędów)
        """
        if not self.session:
            await self.start()
            
        tasks = []
        if self.discord_enabled:
            tasks.append(self._send_discord(title, message, color))
        if self.telegram_enabled:
            tasks.append(self._send_telegram(title, message))
            
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _send_discord(self, title: str, message: str, color: int):
        """Wyślij webhook do Discord"""
        try:
            payload = {
                "embeds": [{
                    "title": title,
                    "description": message,
                    "color": color,
                    "footer": {"text": "Monad Trading Bot 🤖"}
                }]
            }
            async with self.session.post(DISCORD_WEBHOOK_URL, json=payload) as resp:
                if resp.status not in (200, 204):
                    logger.error(f"Discord webhook failed: {resp.status}")
        except Exception as e:
            logger.error(f"Discord send error: {e}")

    async def _send_telegram(self, title: str, message: str):
        """Wyślij wiadomość na Telegram"""
        try:
            text = f"<b>{title}</b>\n\n{message}"
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML"
            }
            async with self.session.post(url, json=payload) as resp:
                if resp.status != 200:
                    logger.error(f"Telegram send failed: {resp.status}")
        except Exception as e:
            logger.error(f"Telegram send error: {e}")

"""
📱 Telegram Notifications for Trading Bot
"""

class TelegramNotifier:
    """Send trading notifications to Telegram"""
    
    def __init__(self):
        self.bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID")
        self.enabled = bool(self.bot_token and self.chat_id)
        
        if not self.enabled:
            print("⚠️ Telegram notifications disabled - missing BOT_TOKEN or CHAT_ID")
    
    async def send(self, message: str, parse_mode: str = "HTML"):
        """Send message to Telegram"""
        if not self.enabled:
            return False
            
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            payload = {
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload) as resp:
                    return resp.status == 200
                    
        except Exception as e:
            print(f"Telegram error: {e}")
            return False
    
    # 🛒 Trade notifications
    async def notify_buy(self, token: str, amount_mon: float, whale: str, confidence: float):
        """Notify about buy execution"""
        msg = f"""
🛒 <b>BUY EXECUTED</b>

💰 Amount: <code>{amount_mon:.2f} MON</code>
🪙 Token: <code>{token[:16]}...</code>
🐳 Whale: <code>{whale[:10]}...</code>
🎯 Confidence: <code>{confidence:.0f}%</code>
⏰ Time: {datetime.now().strftime('%H:%M:%S')}
"""
        await self.send(msg)
    
    async def notify_sell(self, token: str, percent: int, reason: str, pnl: float):
        """Notify about sell execution"""
        emoji = "🟢" if pnl >= 0 else "🔴"
        msg = f"""
{emoji} <b>SELL EXECUTED</b>

🪙 Token: <code>{token[:16]}...</code>
📊 Sold: <code>{percent}%</code>
💵 PnL: <code>{pnl:+.1f}%</code>
📝 Reason: {reason}
⏰ Time: {datetime.now().strftime('%H:%M:%S')}
"""
        await self.send(msg)
    
    async def notify_whale_detected(self, whale: str, amount_mon: float, token: str):
        """Notify about whale detection"""
        msg = f"""
🐳 <b>WHALE DETECTED</b>

👤 Whale: <code>{whale[:16]}...</code>
💰 Amount: <code>{amount_mon:.0f} MON</code>
🪙 Token: <code>{token[:16]}...</code>
⏰ Time: {datetime.now().strftime('%H:%M:%S')}
"""
        await self.send(msg)
    
    async def notify_position_update(self, token: str, pnl: float, action: str):
        """Notify about position status"""
        if action == "TP1":
            emoji = "🎯"
            title = "TP1 HIT"
        elif action == "TP2":
            emoji = "🎯🎯"
            title = "TP2 HIT"
        elif action == "STOP_LOSS":
            emoji = "🛑"
            title = "STOP LOSS"
        elif action == "TRAILING_STOP":
            emoji = "📉"
            title = "TRAILING STOP"
        else:
            emoji = "📊"
            title = "POSITION UPDATE"
            
        msg = f"""
{emoji} <b>{title}</b>

🪙 Token: <code>{token[:16]}...</code>
💵 PnL: <code>{pnl:+.1f}%</code>
⏰ Time: {datetime.now().strftime('%H:%M:%S')}
"""
        await self.send(msg)
    
    async def send_position_alert(self, token: str, action: str, pnl: float, 
                                   sell_percent: float, reason: str):
        """Notify about TP/SL triggers (alias for notify_position_update with more details)"""
        emoji_map = {
            "TP1": "💰",
            "TP2": "💰💰",
            "STOP_LOSS": "🛑",
            "TRAILING_STOP": "🎯"
        }
        emoji = emoji_map.get(action, "📊")
        
        msg = f"""
{emoji} <b>{action} TRIGGERED</b>

🪙 Token: <code>{token[:16]}...</code>
💵 PnL: <code>{pnl:+.1f}%</code>
📊 Selling: <code>{sell_percent}%</code>
📝 Reason: {reason}
⏰ Time: {datetime.now().strftime('%H:%M:%S')}
"""
        await self.send(msg)
    
    async def notify_error(self, error: str, context: str = ""):
        """Notify about errors"""
        msg = f"""
⚠️ <b>ERROR</b>

📝 Context: {context}
❌ Error: <code>{error[:200]}</code>
⏰ Time: {datetime.now().strftime('%H:%M:%S')}
"""
        await self.send(msg)
    
    async def notify_daily_summary(self, stats: dict):
        """Send daily trading summary"""
        msg = f"""
📊 <b>DAILY SUMMARY</b>

💰 Total PnL: <code>{stats.get('total_pnl', 0):+.2f} MON</code>
📈 Trades: <code>{stats.get('total_trades', 0)}</code>
✅ Wins: <code>{stats.get('wins', 0)}</code>
❌ Losses: <code>{stats.get('losses', 0)}</code>
🎯 Win Rate: <code>{stats.get('win_rate', 0):.1f}%</code>
💼 Open Positions: <code>{stats.get('open_positions', 0)}</code>
⏰ Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}
"""
        await self.send(msg)


# Globalna instancja
notifier = NotificationService()

# Singleton instance
_notifier: Optional[TelegramNotifier] = None

def get_notifier() -> TelegramNotifier:
    """Get singleton notifier instance"""
    global _notifier
    if _notifier is None:
        _notifier = TelegramNotifier()
    return _notifier


# Quick helper functions
async def notify_buy(*args, **kwargs):
    await get_notifier().notify_buy(*args, **kwargs)

async def notify_sell(*args, **kwargs):
    await get_notifier().notify_sell(*args, **kwargs)

async def notify_whale(*args, **kwargs):
    await get_notifier().notify_whale_detected(*args, **kwargs)

async def notify_error(*args, **kwargs):
    await get_notifier().notify_error(*args, **kwargs)
