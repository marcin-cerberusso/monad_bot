
import asyncio
import json
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Dict, Any, Optional, Callable, List
from pathlib import Path
from collections import defaultdict

# Global in-memory message bus (fallback when no Redis)
_memory_bus = defaultdict(list)  # channel -> [callbacks]
_memory_queue = asyncio.Queue()


class Message:
    """Wiadomość między agentami"""
    def __init__(self, type: str, data: dict, sender: str = "", priority: int = 5):
        self.type = type
        self.data = data
        self.sender = sender
        self.priority = priority
        self.timestamp = datetime.now().isoformat()
        self.id = f"{type}_{int(datetime.now().timestamp()*1000)}"
    
    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "data": self.data,
            "sender": self.sender,
            "priority": self.priority,
            "timestamp": self.timestamp
        }
    
    def to_json(self) -> str:
        return json.dumps(self.to_dict())
    
    @classmethod
    def from_json(cls, data: str) -> 'Message':
        d = json.loads(data)
        msg = cls(d["type"], d["data"], d.get("sender", ""), d.get("priority", 5))
        msg.id = d.get("id", msg.id)
        msg.timestamp = d.get("timestamp", msg.timestamp)
        return msg


class BaseAgent(ABC):
    """Bazowa klasa agenta - działa z lub bez Redis"""
    
    def __init__(self, name: str, redis_url: str = "redis://localhost:6379"):
        self.name = name
        self.redis_url = redis_url
        self.redis = None
        self.pubsub = None
        self.running = False
        self.subscriptions: List[str] = []
        self.use_redis = False
        from .config import setup_logging
        self.logger = setup_logging(name)
        
    async def connect(self):
        """Połącz z Redis/Dragonfly lub użyj in-memory"""
        try:
            import redis.asyncio as redis_lib
            self.redis = await redis_lib.from_url(self.redis_url)
            await self.redis.ping()
            self.pubsub = self.redis.pubsub()
            self.use_redis = True
            self.log("Connected to Redis")
        except Exception as e:
            self.log(f"Redis unavailable, using in-memory bus")
            self.use_redis = False
    
    async def disconnect(self):
        """Rozłącz"""
        if self.pubsub:
            await self.pubsub.unsubscribe()
        if self.redis:
            await self.redis.close()
    
    async def publish(self, channel: str, message: Message):
        """Wyślij wiadomość"""
        if self.use_redis and self.redis:
            await self.redis.publish(channel, message.to_json())
        else:
            # In-memory: direct call to subscribers
            for callback in _memory_bus.get(channel, []):
                asyncio.create_task(callback(message))
        self.log(f"-> {channel}: {message.type}")
    
    async def subscribe(self, *channels):
        """Subskrybuj kanały"""
        self.subscriptions.extend(channels)
        if self.use_redis and self.pubsub:
            await self.pubsub.subscribe(*channels)
        else:
            # In-memory: register callback
            for ch in channels:
                _memory_bus[ch].append(self.on_message)
        self.log(f"Subscribed: {list(channels)}")
    
    async def listen(self):
        """Nasłuchuj wiadomości"""
        if self.use_redis and self.pubsub:
            async for msg in self.pubsub.listen():
                if not self.running:
                    break
                if msg["type"] == "message":
                    try:
                        message = Message.from_json(msg["data"])
                        await self.on_message(message)
                    except Exception as e:
                        self.log_error(f"Error processing message: {e}")
        else:
            # In-memory: just keep running (callbacks handle messages)
            while self.running:
                await asyncio.sleep(0.1)
    
    @abstractmethod
    async def on_message(self, message: Message):
        """Obsłuż wiadomość - do implementacji"""
        pass
    
    @abstractmethod
    async def run(self):
        """Główna pętla agenta - do implementacji"""
        pass
    
    async def _heartbeat_loop(self):
        """Log heartbeat every 5 minutes"""
        while self.running:
            self.log("💓 ALIVE")
            await asyncio.sleep(300)

    async def start(self):
        """Uruchom agenta"""
        self.running = True
        await self.connect()
        self.log("Starting...")
        
        # Uruchom listener, run i heartbeat równolegle
        await asyncio.gather(
            self.listen(),
            self.run(),
            self._heartbeat_loop()
        )
    
    async def stop(self):
        """Zatrzymaj agenta"""
        self.running = False
        await self.disconnect()
        self.log("Stopped")
    
    def log(self, msg: str):
        """Log info"""
        self.logger.info(msg)

    def log_error(self, msg: str):
        """Log error and notify"""
        self.logger.error(msg)
        asyncio.create_task(self.notify_error(f"Error in {self.name}", msg))

    async def notify(self, title: str, message: str, color: int = 0x00FF00):
        """Wyślij powiadomienie"""
        from .notifications import notifier
        await notifier.send_alert(f"[{self.name}] {title}", message, color)

    async def notify_error(self, title: str, message: str):
        """Wyślij alert o błędzie"""
        await self.notify(f"❌ {title}", message, 0xFF0000)


# Typy wiadomości
class MessageTypes:
    WHALE_BUY = "whale_buy"
    RISK_CHECK = "risk_check"
    RISK_RESULT = "risk_result"
    AI_ANALYZE = "ai_analyze"
    AI_RESULT = "ai_result"
    BUY_ORDER = "buy_order"
    SELL_ORDER = "sell_order"
    TRADE_EXECUTED = "trade_executed"
    POSITION_UPDATE = "position_update"
    PRICE_UPDATE = "price_update"
    ALERT = "alert"


# Kanały
class Channels:
    WHALE = "monad:whale"
    RISK = "monad:risk"
    AI = "monad:ai"
    TRADER = "monad:trader"
    POSITION = "monad:position"
    BROADCAST = "monad:broadcast"
