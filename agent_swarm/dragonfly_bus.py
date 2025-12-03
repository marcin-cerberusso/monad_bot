#!/usr/bin/env python3
"""
🐉 DRAGONFLY BUS - DEPRECATED STUB

⚠️ Ta implementacja jest przestarzała!
   Użyj message_bus.py (MessageBus V2) zamiast DragonflyBus.

Ten plik istnieje tylko dla kompatybilności wstecznej.
Wszystkie wywołania są przekierowywane do MessageBus.
"""

import warnings
from typing import Any, Callable, Dict, List, Optional
from dataclasses import dataclass, asdict
from datetime import datetime
from enum import Enum
import json

# Import nowej implementacji
from .message_bus import MessageBus, get_bus, shutdown_all
from .message_types import MessageType, Priority, Message

warnings.warn(
    "DragonflyBus is deprecated, use MessageBus from message_bus.py instead",
    DeprecationWarning,
    stacklevel=2
)


class SignalType(Enum):
    """Typ sygnału - DEPRECATED, użyj MessageType"""
    WHALE_BUY = "whale_alert"
    WHALE_SELL = "whale_alert"
    NEW_TOKEN = "new_token"
    TOKEN_SCORE = "analysis_result"
    AI_RECOMMENDATION = "analysis_result"
    AI_STOP_LOSS = "risk_alert"
    AI_TAKE_PROFIT = "trade_signal"
    BUY_ORDER = "trade_signal"
    SELL_ORDER = "trade_signal"
    ORDER_EXECUTED = "trade_executed"
    ORDER_FAILED = "trade_executed"
    RISK_ALERT = "risk_alert"
    POSITION_UPDATE = "price_update"
    VOTE_REQUEST = "consensus_request"
    VOTE_RESPONSE = "consensus_vote"
    CONSENSUS_REACHED = "consensus_result"


@dataclass
class Signal:
    """Sygnał - DEPRECATED, użyj Message z message_types.py"""
    type: str
    source: str
    data: Dict[str, Any]
    timestamp: str = ""
    priority: int = 5
    requires_consensus: bool = False
    
    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now().isoformat()
    
    def to_json(self) -> str:
        return json.dumps(asdict(self))
    
    @classmethod
    def from_json(cls, data: str) -> "Signal":
        d = json.loads(data)
        return cls(**d)
    
    def to_message(self) -> Message:
        """Konwertuj do nowego formatu Message"""
        type_mapping = {
            "whale_buy": MessageType.WHALE_ALERT,
            "whale_sell": MessageType.WHALE_ALERT,
            "new_token": MessageType.NEW_TOKEN,
            "ai_recommendation": MessageType.ANALYSIS_RESULT,
            "buy_order": MessageType.TRADE_SIGNAL,
            "sell_order": MessageType.TRADE_SIGNAL,
            "order_executed": MessageType.TRADE_EXECUTED,
            "risk_alert": MessageType.RISK_ALERT,
            "vote_request": MessageType.CONSENSUS_REQUEST,
            "vote_response": MessageType.CONSENSUS_VOTE,
        }
        msg_type = type_mapping.get(self.type, MessageType.PRICE_UPDATE)
        
        return Message(
            type=msg_type,
            sender=self.source,
            payload=self.data,
            priority=Priority.HIGH if self.priority >= 8 else 
                     Priority.NORMAL if self.priority >= 5 else Priority.LOW
        )


class DragonflyBus:
    """
    DEPRECATED: Wrapper kompatybilności wstecznej.
    
    Użyj MessageBus zamiast tego:
        from agent_swarm.message_bus import MessageBus
        bus = MessageBus("agent_name")
        await bus.connect()
    """
    
    def __init__(self, agent_name: str):
        warnings.warn(
            f"DragonflyBus is deprecated, use MessageBus instead",
            DeprecationWarning,
            stacklevel=2
        )
        self.agent_name = agent_name
        self._bus: Optional[MessageBus] = None
        self.handlers: Dict[str, List[Callable]] = {}
        self.running = False
        
    @property
    def redis(self):
        return self._bus.redis if self._bus else None
        
    async def connect(self):
        self._bus = MessageBus(self.agent_name)
        await self._bus.connect()
        print(f"⚠️ [{self.agent_name}] Using deprecated DragonflyBus wrapper")
        
    async def disconnect(self):
        if self._bus:
            await self._bus.disconnect()
            
    async def publish(self, channel: str, signal: Signal):
        if not self._bus:
            raise RuntimeError("Not connected")
        message = signal.to_message()
        await self._bus.publish(message, channel)
        
    async def subscribe(self, *channels: str):
        if not self._bus:
            raise RuntimeError("Not connected")
        await self._bus.subscribe(*channels)
        
    def on_signal(self, signal_type: str):
        def decorator(func: Callable):
            if signal_type not in self.handlers:
                self.handlers[signal_type] = []
            self.handlers[signal_type].append(func)
            return func
        return decorator
        
    async def listen(self):
        if not self._bus:
            raise RuntimeError("Not connected")
        await self._bus.listen()
        
    async def signal_whale_buy(self, token: str, whale: str, amount: float, **kwargs):
        if not self._bus:
            raise RuntimeError("Not connected")
        await self._bus.signal_whale_alert(whale=whale, token=token, action="buy", amount=amount, **kwargs)
        
    async def signal_new_token(self, token: str, name: str, score: int, **kwargs):
        if not self._bus:
            raise RuntimeError("Not connected")
        await self._bus.signal_new_token(token=token, name=name, symbol=name[:4].upper(), score=score, **kwargs)
        
    async def signal_risk_alert(self, level: str, message: str, **kwargs):
        if not self._bus:
            raise RuntimeError("Not connected")
        await self._bus.signal_risk_alert(level=level, message=message, **kwargs)
        
    async def request_consensus(self, action: str, data: dict, timeout: float = 5.0) -> dict:
        if not self._bus:
            raise RuntimeError("Not connected")
        from .message_types import ConsensusRequestPayload
        payload = ConsensusRequestPayload(
            action=action,
            token_address=data.get("token", ""),
            amount_mon=data.get("amount_mon", 0),
            reason=data.get("reason", ""),
            timeout_seconds=timeout,
            min_approvals=2
        )
        approved = await self._bus.request_consensus(payload)
        return {"approved": approved, "votes": {"approve": 1 if approved else 0}, "total": 1}
        
    async def vote(self, vote_id: str, decision: str):
        if not self._bus:
            raise RuntimeError("Not connected")
        await self._bus.vote(vote_id, decision)
        
    async def set_state(self, key: str, value: Any, ttl: int = None):
        if not self._bus:
            raise RuntimeError("Not connected")
        await self._bus.set_state(key, value, ttl)
            
    async def get_state(self, key: str) -> Any:
        if not self._bus:
            raise RuntimeError("Not connected")
        return await self._bus.get_state(key)


# Re-export
__all__ = ["MessageBus", "get_bus", "shutdown_all", "DragonflyBus", "Signal", "SignalType"]
