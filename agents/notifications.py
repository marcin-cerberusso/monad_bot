"""
🔔 NOTIFICATION SERVICE - Obsługa powiadomień Discord/Telegram
"""
import aiohttp
import logging
from typing import Optional

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
            import asyncio
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

# Globalna instancja
notifier = NotificationService()
