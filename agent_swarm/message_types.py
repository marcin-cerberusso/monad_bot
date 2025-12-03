#!/usr/bin/env python3
"""
📨 MESSAGE TYPES - Definicje wiadomości dla Agent Swarm

Wszystkie typy wiadomości i ich payloady.
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
import json
import uuid


class MessageType(Enum):
    """Typy wiadomości w systemie"""
    
    # === MARKET DATA ===
    PRICE_UPDATE = "price_update"           # Aktualizacja ceny tokena
    WHALE_ALERT = "whale_alert"             # Wykryto whale transaction
    NEW_TOKEN = "new_token"                 # Nowy token na rynku
    
    # === ANALYSIS ===
    ANALYSIS_REQUEST = "analysis_request"   # Prośba o analizę
    ANALYSIS_RESULT = "analysis_result"     # Wynik analizy
    
    # === TRADING ===
    TRADE_SIGNAL = "trade_signal"           # Sygnał kupna/sprzedaży
    TRADE_EXECUTED = "trade_executed"       # Potwierdzenie wykonania
    TRADE_FAILED = "trade_failed"           # Błąd wykonania
    
    # === RISK ===
    RISK_ALERT = "risk_alert"               # Alert ryzyka
    STOP_LOSS_TRIGGER = "stop_loss_trigger" # Trigger SL
    TAKE_PROFIT_TRIGGER = "tp_trigger"      # Trigger TP
    
    # === CONSENSUS ===
    CONSENSUS_REQUEST = "consensus_request" # Prośba o głosowanie
    CONSENSUS_VOTE = "consensus_vote"       # Głos agenta
    CONSENSUS_RESULT = "consensus_result"   # Wynik głosowania
    
    # === SYSTEM ===
    SYSTEM_STATUS = "system_status"         # Status systemu
    AGENT_HEARTBEAT = "agent_heartbeat"     # Agent żyje
    ERROR = "error"                         # Błąd


class Priority(Enum):
    """Priorytet wiadomości"""
    LOW = 1
    NORMAL = 5
    HIGH = 7
    URGENT = 9
    CRITICAL = 10


class TradeAction(Enum):
    """Typ akcji tradingowej"""
    BUY = "buy"
    SELL = "sell"
    PARTIAL_SELL = "partial_sell"


class RiskLevel(Enum):
    """Poziom ryzyka"""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class Message:
    """Wiadomość w systemie"""
    type: MessageType
    sender: str                             # Nazwa agenta wysyłającego
    payload: Dict[str, Any]
    recipient: str = "all"                  # "all" = broadcast
    priority: Priority = Priority.NORMAL
    requires_consensus: bool = False
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    
    def to_json(self) -> str:
        data = asdict(self)
        data["type"] = self.type.value
        data["priority"] = self.priority.value
        return json.dumps(data)
    
    @classmethod
    def from_json(cls, data: str) -> "Message":
        d = json.loads(data)
        d["type"] = MessageType(d["type"])
        d["priority"] = Priority(d["priority"])
        return cls(**d)
    
    def __str__(self):
        return f"[{self.type.value}] {self.sender}→{self.recipient}: {self.payload}"


# === PAYLOAD DEFINITIONS ===

@dataclass
class PriceUpdatePayload:
    """Payload dla PRICE_UPDATE"""
    token_address: str
    token_name: str
    price_mon: float
    price_usd: float = 0.0
    change_1h: float = 0.0
    change_24h: float = 0.0
    volume_24h: float = 0.0
    liquidity: float = 0.0
    
    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class WhaleAlertPayload:
    """Payload dla WHALE_ALERT"""
    whale_address: str
    whale_name: str
    token_address: str
    token_name: str
    action: TradeAction
    amount_mon: float
    tx_hash: str = ""
    whale_win_rate: float = 0.0
    confidence: float = 0.5
    
    def to_dict(self) -> dict:
        data = asdict(self)
        data["action"] = self.action.value
        return data


@dataclass
class NewTokenPayload:
    """Payload dla NEW_TOKEN"""
    token_address: str
    token_name: str
    token_symbol: str
    creator: str
    quality_score: int = 0
    liquidity: float = 0.0
    holder_count: int = 0
    
    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AnalysisRequestPayload:
    """Payload dla ANALYSIS_REQUEST"""
    token_address: str
    analysis_type: str = "full"  # full, quick, risk
    source: str = ""  # whale_alert, scanner, manual
    context: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AnalysisResultPayload:
    """Payload dla ANALYSIS_RESULT"""
    token_address: str
    recommendation: str  # buy, sell, hold, avoid
    confidence: float
    reasons: List[str]
    suggested_action: Optional[TradeAction] = None
    suggested_amount: float = 0.0
    stop_loss_pct: float = -15.0
    take_profit_pct: float = 30.0
    risk_level: RiskLevel = RiskLevel.MEDIUM
    
    def to_dict(self) -> dict:
        data = asdict(self)
        if self.suggested_action:
            data["suggested_action"] = self.suggested_action.value
        data["risk_level"] = self.risk_level.value
        return data


@dataclass
class TradeSignalPayload:
    """Payload dla TRADE_SIGNAL"""
    action: TradeAction
    token_address: str
    token_name: str
    amount_mon: float = 0.0           # dla BUY
    sell_percent: float = 100.0       # dla SELL
    reason: str = ""
    source_signal: str = ""           # whale, ai, sniper, manual
    urgency: Priority = Priority.NORMAL
    
    def to_dict(self) -> dict:
        data = asdict(self)
        data["action"] = self.action.value
        data["urgency"] = self.urgency.value
        return data


@dataclass
class TradeExecutedPayload:
    """Payload dla TRADE_EXECUTED"""
    action: TradeAction
    token_address: str
    token_name: str
    amount_mon: float
    tx_hash: str = ""
    price_mon: float = 0.0
    slippage: float = 0.0
    success: bool = True
    error: str = ""
    
    def to_dict(self) -> dict:
        data = asdict(self)
        data["action"] = self.action.value
        return data


@dataclass
class RiskAlertPayload:
    """Payload dla RISK_ALERT"""
    level: RiskLevel
    message: str
    token_address: Optional[str] = None
    suggested_action: Optional[str] = None  # sell, reduce, hold
    
    def to_dict(self) -> dict:
        data = asdict(self)
        data["level"] = self.level.value
        return data


@dataclass
class ConsensusRequestPayload:
    """Payload dla CONSENSUS_REQUEST"""
    action: str  # buy, sell, etc.
    token_address: str
    token_name: str
    amount_mon: float = 0.0
    reason: str = ""
    timeout_seconds: float = 5.0
    min_approvals: int = 2
    
    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ConsensusVotePayload:
    """Payload dla CONSENSUS_VOTE"""
    request_id: str
    vote: str  # approve, reject, abstain
    reason: str = ""
    
    def to_dict(self) -> dict:
        return asdict(self)


@dataclass 
class ConsensusResultPayload:
    """Payload dla CONSENSUS_RESULT"""
    request_id: str
    approved: bool
    votes_approve: int
    votes_reject: int
    votes_abstain: int
    
    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AgentHeartbeatPayload:
    """Payload dla AGENT_HEARTBEAT"""
    agent_name: str
    status: str = "running"  # running, busy, error
    current_task: str = ""
    memory_mb: float = 0.0
    uptime_seconds: float = 0.0
    
    def to_dict(self) -> dict:
        return asdict(self)


# === MESSAGE BUILDERS ===

class MessageBuilder:
    """Helper do tworzenia wiadomości"""
    
    @staticmethod
    def price_update(sender: str, payload: PriceUpdatePayload) -> Message:
        return Message(
            type=MessageType.PRICE_UPDATE,
            sender=sender,
            payload=payload.to_dict(),
            priority=Priority.NORMAL
        )
    
    @staticmethod
    def whale_alert(sender: str, payload: WhaleAlertPayload) -> Message:
        return Message(
            type=MessageType.WHALE_ALERT,
            sender=sender,
            payload=payload.to_dict(),
            priority=Priority.HIGH
        )
    
    @staticmethod
    def new_token(sender: str, payload: NewTokenPayload) -> Message:
        return Message(
            type=MessageType.NEW_TOKEN,
            sender=sender,
            payload=payload.to_dict(),
            priority=Priority.HIGH
        )
    
    @staticmethod
    def analysis_request(sender: str, recipient: str, payload: AnalysisRequestPayload) -> Message:
        return Message(
            type=MessageType.ANALYSIS_REQUEST,
            sender=sender,
            recipient=recipient,
            payload=payload.to_dict(),
            priority=Priority.NORMAL
        )
    
    @staticmethod
    def analysis_result(sender: str, recipient: str, payload: AnalysisResultPayload) -> Message:
        return Message(
            type=MessageType.ANALYSIS_RESULT,
            sender=sender,
            recipient=recipient,
            payload=payload.to_dict(),
            priority=Priority.HIGH
        )
    
    @staticmethod
    def trade_signal(sender: str, payload: TradeSignalPayload, 
                     requires_consensus: bool = False) -> Message:
        return Message(
            type=MessageType.TRADE_SIGNAL,
            sender=sender,
            payload=payload.to_dict(),
            priority=payload.urgency,
            requires_consensus=requires_consensus
        )
    
    @staticmethod
    def trade_executed(sender: str, payload: TradeExecutedPayload) -> Message:
        return Message(
            type=MessageType.TRADE_EXECUTED,
            sender=sender,
            payload=payload.to_dict(),
            priority=Priority.HIGH
        )
    
    @staticmethod
    def risk_alert(sender: str, payload: RiskAlertPayload) -> Message:
        prio = Priority.CRITICAL if payload.level == RiskLevel.CRITICAL else Priority.HIGH
        return Message(
            type=MessageType.RISK_ALERT,
            sender=sender,
            payload=payload.to_dict(),
            priority=prio
        )
    
    @staticmethod
    def consensus_request(sender: str, payload: ConsensusRequestPayload) -> Message:
        return Message(
            type=MessageType.CONSENSUS_REQUEST,
            sender=sender,
            payload=payload.to_dict(),
            priority=Priority.URGENT,
            requires_consensus=True
        )
    
    @staticmethod
    def consensus_vote(sender: str, request_id: str, payload: ConsensusVotePayload) -> Message:
        return Message(
            type=MessageType.CONSENSUS_VOTE,
            sender=sender,
            recipient=request_id.split(":")[0] if ":" in request_id else "trader",
            payload=payload.to_dict(),
            priority=Priority.URGENT
        )
    
    @staticmethod
    def heartbeat(sender: str, payload: AgentHeartbeatPayload) -> Message:
        return Message(
            type=MessageType.AGENT_HEARTBEAT,
            sender=sender,
            payload=payload.to_dict(),
            priority=Priority.LOW
        )
