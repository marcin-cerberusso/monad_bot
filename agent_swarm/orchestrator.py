#!/usr/bin/env python3
"""
🐝 AGENT SWARM ORCHESTRATOR - Multi-Agent Trading System

Architektura:
┌─────────────────────────────────────────────────────────────────┐
│                    🧠 ORCHESTRATOR (CDN Layer)                   │
│         Zarządza komunikacją, routingiem, consensus             │
└─────────────────────────────────────────────────────────────────┘
        │              │              │              │
        ▼              ▼              ▼              ▼
┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
│ 🔍 SCANNER   │ │ 📊 ANALYST   │ │ 💰 TRADER    │ │ 🛡️ RISK      │
│   Agent      │ │   Agent      │ │   Agent      │ │   Agent      │
│              │ │              │ │              │ │              │
│ - Price feed │ │ - TA/FA      │ │ - Execute    │ │ - Monitor    │
│ - Whale      │ │ - Sentiment  │ │ - Position   │ │ - Stop loss  │
│ - New tokens │ │ - Scoring    │ │ - Timing     │ │ - Exposure   │
└──────────────┘ └──────────────┘ └──────────────┘ └──────────────┘
        │              │              │              │
        └──────────────┴──────────────┴──────────────┘
                              │
                    ┌─────────▼─────────┐
                    │  📡 MESSAGE BUS    │
                    │  (Redis/In-Memory) │
                    └───────────────────┘

Każdy agent:
- Izolowane środowisko (własny context, bez halucynacji)
- Własna specjalizacja i prompt
- Komunikuje się tylko przez Message Bus
- Może być zastąpiony/zaktualizowany niezależnie
"""

import asyncio
import json
import os
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Callable
import aiohttp
import redis.asyncio as redis
from dotenv import load_dotenv

load_dotenv()

# Config
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


class MessageType(Enum):
    """Typy wiadomości w systemie"""
    PRICE_UPDATE = "price_update"
    WHALE_ALERT = "whale_alert"
    NEW_TOKEN = "new_token"
    ANALYSIS_REQUEST = "analysis_request"
    ANALYSIS_RESULT = "analysis_result"
    TRADE_SIGNAL = "trade_signal"
    TRADE_EXECUTED = "trade_executed"
    RISK_ALERT = "risk_alert"
    SYSTEM_STATUS = "system_status"
    CONSENSUS_REQUEST = "consensus_request"
    CONSENSUS_VOTE = "consensus_vote"


@dataclass
class Message:
    """Wiadomość w Message Bus"""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    type: MessageType = MessageType.SYSTEM_STATUS
    sender: str = "system"
    recipient: str = "all"  # "all" = broadcast
    payload: Dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    priority: int = 5  # 1-10, higher = more urgent
    requires_consensus: bool = False
    
    def to_json(self) -> str:
        return json.dumps({
            "id": self.id,
            "type": self.type.value,
            "sender": self.sender,
            "recipient": self.recipient,
            "payload": self.payload,
            "timestamp": self.timestamp,
            "priority": self.priority,
            "requires_consensus": self.requires_consensus
        })
    
    @classmethod
    def from_json(cls, data: str) -> "Message":
        d = json.loads(data)
        return cls(
            id=d["id"],
            type=MessageType(d["type"]),
            sender=d["sender"],
            recipient=d["recipient"],
            payload=d["payload"],
            timestamp=d["timestamp"],
            priority=d["priority"],
            requires_consensus=d.get("requires_consensus", False)
        )


class MessageBus:
    """
    Message Bus - CDN warstwa komunikacji między agentami
    
    Używa Redis pub/sub dla distributed deployment
    lub in-memory queue dla single-node
    """
    
    def __init__(self, use_redis: bool = False):
        self.use_redis = use_redis
        self.redis: Optional[redis.Redis] = None
        self.subscribers: Dict[str, List[Callable]] = {}
        self.message_queue: asyncio.Queue = asyncio.Queue()
        self.running = False
        
    async def connect(self):
        """Połącz z Redis (jeśli używamy)"""
        if self.use_redis:
            self.redis = await redis.from_url(REDIS_URL)
            print("📡 Connected to Redis Message Bus")
        else:
            print("📡 Using in-memory Message Bus")
        self.running = True
        
    async def disconnect(self):
        """Rozłącz"""
        self.running = False
        if self.redis:
            await self.redis.close()
            
    async def publish(self, message: Message):
        """Publikuj wiadomość"""
        if self.use_redis and self.redis:
            await self.redis.publish("agent_swarm", message.to_json())
        else:
            await self.message_queue.put(message)
            
        # Notify local subscribers
        channel = message.recipient
        if channel in self.subscribers:
            for callback in self.subscribers[channel]:
                asyncio.create_task(callback(message))
                
        # Broadcast to "all" subscribers
        if "all" in self.subscribers and channel != "all":
            for callback in self.subscribers["all"]:
                asyncio.create_task(callback(message))
                
    def subscribe(self, channel: str, callback: Callable):
        """Subskrybuj kanał"""
        if channel not in self.subscribers:
            self.subscribers[channel] = []
        self.subscribers[channel].append(callback)
        
    async def get_message(self) -> Optional[Message]:
        """Pobierz wiadomość z kolejki (in-memory mode)"""
        try:
            return await asyncio.wait_for(self.message_queue.get(), timeout=0.1)
        except asyncio.TimeoutError:
            return None


class BaseAgent(ABC):
    """
    Bazowa klasa agenta
    
    Każdy agent:
    - Ma własny izolowany context (system prompt)
    - Nie dzieli pamięci z innymi agentami
    - Komunikuje się tylko przez Message Bus
    """
    
    def __init__(self, name: str, message_bus: MessageBus):
        self.name = name
        self.message_bus = message_bus
        self.running = False
        self.context: List[Dict] = []  # Izolowany context
        self.system_prompt = ""
        self.model = "deepseek-chat"
        self.max_context_messages = 20
        
    @abstractmethod
    async def process_message(self, message: Message) -> Optional[Message]:
        """Przetwórz wiadomość - do implementacji przez podklasy"""
        pass
    
    async def start(self):
        """Uruchom agenta"""
        self.running = True
        self.message_bus.subscribe(self.name, self._handle_message)
        self.message_bus.subscribe("all", self._handle_message)
        print(f"🤖 Agent {self.name} started")
        
    async def stop(self):
        """Zatrzymaj agenta"""
        self.running = False
        print(f"🛑 Agent {self.name} stopped")
        
    async def _handle_message(self, message: Message):
        """Obsłuż wiadomość"""
        if not self.running:
            return
            
        # Ignoruj własne wiadomości
        if message.sender == self.name:
            return
            
        response = await self.process_message(message)
        if response:
            await self.message_bus.publish(response)
            
    async def think(self, prompt: str, use_context: bool = True) -> str:
        """
        Zapytaj AI (izolowany context!)
        """
        if not DEEPSEEK_API_KEY:
            print(f"❌ {self.name} AI error: DEEPSEEK_API_KEY missing")
            return ""
        
        messages = [{"role": "system", "content": self.system_prompt}]
        
        if use_context:
            messages.extend(self.context[-self.max_context_messages:])
            
        messages.append({"role": "user", "content": prompt})
        
        attempts = 0
        last_error = ""
        while attempts < 3:
            attempts += 1
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        "https://api.deepseek.com/v1/chat/completions",
                        headers={
                            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                            "Content-Type": "application/json"
                        },
                        json={
                            "model": self.model,
                            "messages": messages,
                            "temperature": 0.3,
                            "max_tokens": 1000
                        },
                        timeout=aiohttp.ClientTimeout(total=60)
                    ) as resp:
                        if resp.status >= 500:
                            last_error = f"Server {resp.status}"
                            await asyncio.sleep(1.5 * attempts)
                            continue
                        data = await resp.json()
                        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                        
                        # Zapisz do context (izolowany!)
                        self.context.append({"role": "user", "content": prompt})
                        self.context.append({"role": "assistant", "content": content})
                        
                        return content
            except Exception as e:
                last_error = str(e)
                await asyncio.sleep(1.5 * attempts)
        
        print(f"❌ {self.name} AI error after retries: {last_error}")
        return ""
            
    def clear_context(self):
        """Wyczyść context (reset pamięci)"""
        self.context = []


# ═══════════════════════════════════════════════════════════════════════════════
# 🔍 SCANNER AGENT - Monitoruje rynek, ceny, wieloryby
# ═══════════════════════════════════════════════════════════════════════════════

class ScannerAgent(BaseAgent):
    """
    Agent skanujący rynek 24/7
    
    Odpowiada za:
    - Monitoring cen tokenów
    - Wykrywanie whale movements
    - Znajdowanie nowych tokenów
    - Wysyłanie alertów do innych agentów
    """
    
    def __init__(self, message_bus: MessageBus):
        super().__init__("scanner", message_bus)
        self.system_prompt = """Jesteś agentem skanującym rynek memecoinów na Monad blockchain.
Twoja rola to TYLKO zbieranie danych i wysyłanie alertów.
NIE podejmujesz decyzji tradingowych - to robią inni agenci.

Formatuj alerty jako JSON:
{"type": "whale|price|token", "data": {...}, "urgency": 1-10}
"""
        self.watched_tokens: Dict[str, float] = {}  # token -> last_price
        
    async def process_message(self, message: Message) -> Optional[Message]:
        """Przetwórz wiadomość"""
        if message.type == MessageType.SYSTEM_STATUS:
            # Status request
            return Message(
                type=MessageType.SYSTEM_STATUS,
                sender=self.name,
                payload={
                    "status": "active",
                    "watched_tokens": len(self.watched_tokens)
                }
            )
        return None
        
    async def scan_whale_activity(self, whale_address: str, amount: float, token: str):
        """Wykryto whale activity - wyślij alert"""
        analysis = await self.think(f"""
Wykryto whale buy:
- Whale: {whale_address[:12]}...
- Amount: {amount} MON
- Token: {token[:12]}...

Oceń urgency (1-10) i czy warto śledzić. Odpowiedz JSON:
{{"urgency": 7, "follow": true, "reason": "..."}}
""")
        
        try:
            result = json.loads(analysis)
        except (json.JSONDecodeError, TypeError):
            result = {"urgency": 5, "follow": True, "reason": "default"}
            
        await self.message_bus.publish(Message(
            type=MessageType.WHALE_ALERT,
            sender=self.name,
            recipient="analyst",  # Wyślij do analityka
            payload={
                "whale": whale_address,
                "amount": amount,
                "token": token,
                "urgency": result.get("urgency", 5),
                "scanner_opinion": result
            },
            priority=result.get("urgency", 5)
        ))
        
    async def monitor_prices(self, prices: Dict[str, float]):
        """Monitoruj zmiany cen"""
        for token, price in prices.items():
            if price <= 0:
                continue  # Skip invalid prices
                
            if token in self.watched_tokens:
                old_price = self.watched_tokens[token]
                
                # Avoid division by zero
                if old_price <= 0:
                    self.watched_tokens[token] = price
                    continue
                    
                change_pct = ((price - old_price) / old_price) * 100
                
                # Alert jeśli duża zmiana
                if abs(change_pct) > 5:
                    await self.message_bus.publish(Message(
                        type=MessageType.PRICE_UPDATE,
                        sender=self.name,
                        payload={
                            "token": token,
                            "old_price": old_price,
                            "new_price": price,
                            "change_pct": change_pct
                        },
                        priority=8 if abs(change_pct) > 10 else 5
                    ))
                    
            self.watched_tokens[token] = price


# ═══════════════════════════════════════════════════════════════════════════════
# 📊 ANALYST AGENT - Analizuje dane, daje scoring
# ═══════════════════════════════════════════════════════════════════════════════

class AnalystAgent(BaseAgent):
    """
    Agent analityczny
    
    Odpowiada za:
    - Analiza techniczna/fundamentalna
    - Scoring tokenów
    - Sentiment analysis
    - Rekomendacje (ale NIE decyzje!)
    """
    
    def __init__(self, message_bus: MessageBus):
        super().__init__("analyst", message_bus)
        self.system_prompt = """Jesteś ekspertem analizy memecoinów na Monad blockchain.
Twoja rola to ANALIZA i SCORING - nie podejmujesz decyzji.

Dla każdego tokena oceń:
1. Technical score (0-100)
2. Fundamental score (0-100)  
3. Risk score (0-100, wyższy = bardziej ryzykowny)
4. Overall recommendation: BUY/HOLD/SELL/AVOID

Zawsze odpowiadaj w formacie JSON.
Bądź obiektywny i ostrożny - lepiej przegapić okazję niż stracić pieniądze.
"""
        self.token_scores: Dict[str, Dict] = {}
        
    async def process_message(self, message: Message) -> Optional[Message]:
        """Przetwórz wiadomość"""
        
        if message.type == MessageType.WHALE_ALERT:
            # Whale alert od scannera - przeanalizuj token
            return await self._analyze_whale_signal(message.payload)
            
        elif message.type == MessageType.ANALYSIS_REQUEST:
            # Ktoś prosi o analizę
            token = message.payload.get("token")
            return await self._analyze_token(token)
            
        elif message.type == MessageType.PRICE_UPDATE:
            # Zmiana ceny - zaktualizuj scoring
            await self._update_score(message.payload)
            
        return None
        
    async def _analyze_whale_signal(self, payload: dict) -> Message:
        """Przeanalizuj sygnał od whale"""
        
        analysis = await self.think(f"""
WHALE SIGNAL DO ANALIZY:
- Whale: {payload.get('whale', 'unknown')[:12]}...
- Amount: {payload.get('amount', 0)} MON
- Token: {payload.get('token', 'unknown')[:12]}...
- Scanner urgency: {payload.get('urgency', 5)}/10

Przeanalizuj i daj scoring. Odpowiedz JSON:
{{
    "technical_score": 65,
    "fundamental_score": 50,
    "risk_score": 70,
    "overall": "BUY|HOLD|SELL|AVOID",
    "confidence": 0.7,
    "reasoning": "...",
    "suggested_amount_mon": 10,
    "take_profit_pct": 50,
    "stop_loss_pct": -20
}}
""")
        
        try:
            result = json.loads(analysis)
        except (json.JSONDecodeError, TypeError):
            result = {
                "technical_score": 50,
                "overall": "AVOID",
                "confidence": 0.3,
                "reasoning": "Parse error"
            }
            
        # Zapisz scoring
        token = payload.get("token", "")
        self.token_scores[token] = result
        
        return Message(
            type=MessageType.ANALYSIS_RESULT,
            sender=self.name,
            recipient="trader",  # Wyślij do tradera
            payload={
                "token": token,
                "whale_amount": payload.get("amount", 0),
                "analysis": result
            },
            priority=7 if result.get("overall") == "BUY" else 3,
            requires_consensus=result.get("overall") == "BUY"  # BUY wymaga consensus
        )
        
    async def _analyze_token(self, token: str) -> Message:
        """Pełna analiza tokena"""
        # TODO: Pobierz dane on-chain i z API
        pass
        
    async def _update_score(self, payload: dict):
        """Zaktualizuj scoring po zmianie ceny"""
        token = payload.get("token")
        change = payload.get("change_pct", 0)
        
        if token in self.token_scores:
            # Dynamiczna aktualizacja
            if change > 10:
                self.token_scores[token]["momentum"] = "bullish"
            elif change < -10:
                self.token_scores[token]["momentum"] = "bearish"


# ═══════════════════════════════════════════════════════════════════════════════
# 💰 TRADER AGENT - Wykonuje transakcje
# ═══════════════════════════════════════════════════════════════════════════════

class TraderAgent(BaseAgent):
    """
    Agent tradingowy
    
    Odpowiada za:
    - Wykonywanie transakcji
    - Position sizing
    - Entry/exit timing
    - Portfolio management
    """
    
    def __init__(self, message_bus: MessageBus):
        super().__init__("trader", message_bus)
        self.system_prompt = """Jesteś agentem wykonującym transakcje na Monad blockchain.
Twoja rola to WYKONYWANIE decyzji po uzyskaniu consensus.

Zasady:
1. NIGDY nie działaj bez consensus od Risk Agenta
2. Maksymalnie 5 otwartych pozycji
3. Maksymalnie 10% portfolio na jedną pozycję
4. Zawsze ustawiaj stop loss

Odpowiadaj w formacie JSON z akcją do wykonania.
"""
        self.pending_trades: Dict[str, Dict] = {}
        self.open_positions: int = 0
        self.max_positions: int = 5
        
    async def process_message(self, message: Message) -> Optional[Message]:
        """Przetwórz wiadomość"""
        
        if message.type == MessageType.ANALYSIS_RESULT:
            # Dostaliśmy analizę - sprawdź czy kupić
            return await self._handle_analysis(message)
            
        elif message.type == MessageType.CONSENSUS_VOTE:
            # Głos w consensus
            return await self._handle_consensus_vote(message)
            
        elif message.type == MessageType.RISK_ALERT:
            # Alert od Risk Agenta
            return await self._handle_risk_alert(message)
            
        return None
        
    async def _handle_analysis(self, message: Message) -> Optional[Message]:
        """Obsłuż wynik analizy"""
        analysis = message.payload.get("analysis", {})
        token = message.payload.get("token", "")
        
        # Jeśli rekomendacja BUY i mamy miejsce
        if analysis.get("overall") == "BUY" and self.open_positions < self.max_positions:
            
            # Wymaga consensus - wyślij request
            if message.requires_consensus:
                self.pending_trades[token] = {
                    "analysis": analysis,
                    "votes": {},
                    "timestamp": datetime.now().isoformat()
                }
                
                return Message(
                    type=MessageType.CONSENSUS_REQUEST,
                    sender=self.name,
                    recipient="all",  # Broadcast
                    payload={
                        "token": token,
                        "action": "BUY",
                        "amount": analysis.get("suggested_amount_mon", 10),
                        "analysis": analysis
                    },
                    requires_consensus=True
                )
        
        return None
        
    async def _handle_consensus_vote(self, message: Message) -> Optional[Message]:
        """Obsłuż głos w consensus"""
        token = message.payload.get("token", "")
        vote = message.payload.get("vote", False)
        voter = message.sender
        
        if token in self.pending_trades:
            self.pending_trades[token]["votes"][voter] = vote
            
            # Sprawdź czy mamy wystarczająco głosów
            votes = self.pending_trades[token]["votes"]
            if len(votes) >= 2:  # Min 2 głosy
                approvals = sum(1 for v in votes.values() if v)
                
                if approvals >= 2:  # Wymagane 2 approval
                    # EXECUTE TRADE
                    return await self._execute_trade(
                        token,
                        self.pending_trades[token]["analysis"]
                    )
                    
        return None
        
    async def _execute_trade(self, token: str, analysis: dict) -> Message:
        """Wykonaj transakcję"""
        amount = analysis.get("suggested_amount_mon", 10)
        
        # Tu byłby kod wykonujący transakcję on-chain
        # ...
        
        self.open_positions += 1
        del self.pending_trades[token]
        
        return Message(
            type=MessageType.TRADE_EXECUTED,
            sender=self.name,
            payload={
                "token": token,
                "action": "BUY",
                "amount": amount,
                "success": True
            },
            priority=8
        )
        
    async def _handle_risk_alert(self, message: Message) -> Optional[Message]:
        """Obsłuż alert od Risk Agenta"""
        action = message.payload.get("recommended_action")
        token = message.payload.get("token")
        
        if action == "SELL_NOW":
            # Emergency sell
            return await self._execute_sell(token, 100)  # 100% pozycji
            
        return None
        
    async def _execute_sell(self, token: str, percent: float) -> Message:
        """Wykonaj sprzedaż"""
        # Tu byłby kod sprzedaży
        # ...
        
        if percent >= 99:
            self.open_positions -= 1
            
        return Message(
            type=MessageType.TRADE_EXECUTED,
            sender=self.name,
            payload={
                "token": token,
                "action": "SELL",
                "percent": percent,
                "success": True
            }
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 🛡️ RISK AGENT - Monitoruje ryzyko, zatrzymuje straty
# ═══════════════════════════════════════════════════════════════════════════════

class RiskAgent(BaseAgent):
    """
    Agent zarządzania ryzykiem
    
    Odpowiada za:
    - Monitoring wszystkich pozycji
    - Stop loss enforcement
    - Portfolio exposure control
    - Veto power dla dużych transakcji
    """
    
    def __init__(self, message_bus: MessageBus):
        super().__init__("risk", message_bus)
        self.system_prompt = """Jesteś agentem zarządzania ryzykiem dla systemu tradingowego.
Twoja rola to OCHRONA KAPITAŁU - jesteś ostatnią linią obrony.

Zasady:
1. Maksymalny drawdown: 20% portfolio
2. Stop loss: -15% na pozycję
3. Max exposure na jeden token: 15%
4. Veto dla ryzykownych transakcji

Możesz VETOWAĆ decyzje innych agentów jeśli ryzyko jest za duże.
Lepiej przegapić zysk niż stracić kapitał!

Odpowiadaj w formacie JSON.
"""
        self.portfolio_value: float = 1000
        self.max_drawdown_pct: float = 20
        self.position_stop_loss_pct: float = -15
        self.positions: Dict[str, Dict] = {}
        self.emergency_sells: set = set()  # Tokeny do natychmiastowej sprzedaży
        
    async def process_message(self, message: Message) -> Optional[Message]:
        """Przetwórz wiadomość"""
        
        if message.type == MessageType.CONSENSUS_REQUEST:
            # Ktoś chce handlować - oceń ryzyko
            return await self._evaluate_trade_risk(message)
            
        elif message.type == MessageType.PRICE_UPDATE:
            # Sprawdź stop lossy
            return await self._check_stop_losses(message.payload)
            
        elif message.type == MessageType.TRADE_EXECUTED:
            # Zaktualizuj tracking
            await self._update_positions(message.payload)
            
        return None
        
    async def _evaluate_trade_risk(self, message: Message) -> Message:
        """Oceń ryzyko proponowanej transakcji"""
        token = message.payload.get("token", "")
        amount = message.payload.get("amount", 0)
        analysis = message.payload.get("analysis", {})
        
        # Oblicz exposure
        current_exposure = sum(p.get("amount", 0) for p in self.positions.values())
        new_exposure = current_exposure + amount
        exposure_pct = (new_exposure / self.portfolio_value) * 100
        
        # Oceń
        risk_evaluation = await self.think(f"""
OCENA RYZYKA TRANSAKCJI:

Proponowana transakcja:
- Token: {token[:12]}...
- Amount: {amount} MON
- Analysis score: {analysis.get('overall', 'N/A')}
- Confidence: {analysis.get('confidence', 0)}

Stan portfolio:
- Portfolio value: {self.portfolio_value} MON
- Current exposure: {current_exposure} MON ({(current_exposure/self.portfolio_value)*100:.1f}%)
- After trade: {new_exposure} MON ({exposure_pct:.1f}%)
- Open positions: {len(self.positions)}

Oceń ryzyko i zagłosuj. Odpowiedz JSON:
{{
    "vote": true|false,
    "risk_score": 65,
    "reasoning": "...",
    "conditions": ["stop_loss_required", "reduce_size"]
}}
""")
        
        try:
            result = json.loads(risk_evaluation)
        except (json.JSONDecodeError, TypeError):
            result = {"vote": False, "risk_score": 100, "reasoning": "Parse error - VETO"}
            
        # Hard rules override
        if exposure_pct > 80:
            result["vote"] = False
            result["reasoning"] = "VETO: Portfolio exposure too high"
            
        if len(self.positions) >= 5:
            result["vote"] = False
            result["reasoning"] = "VETO: Too many open positions"
            
        return Message(
            type=MessageType.CONSENSUS_VOTE,
            sender=self.name,
            payload={
                "token": token,
                "vote": result.get("vote", False),
                "risk_evaluation": result
            }
        )
        
    async def _check_stop_losses(self, payload: dict) -> Optional[Message]:
        """Sprawdź stop lossy"""
        token = payload.get("token", "")
        
        if token in self.positions:
            pos = self.positions[token]
            entry_price = pos.get("entry_price", 0)
            current_price = payload.get("new_price", entry_price)
            
            if entry_price > 0:
                pnl_pct = ((current_price - entry_price) / entry_price) * 100
                
                if pnl_pct <= self.position_stop_loss_pct:
                    # Add to emergency sells for Sell Executor to pick up
                    self.emergency_sells.add(token)
                    print(f"🚨 RISK AGENT: Emergency sell triggered for {token[:16]}... ({pnl_pct:.1f}%)")
                    
                    return Message(
                        type=MessageType.RISK_ALERT,
                        sender=self.name,
                        recipient="trader",
                        payload={
                            "token": token,
                            "pnl_pct": pnl_pct,
                            "recommended_action": "SELL_NOW",
                            "reason": f"Stop loss triggered ({pnl_pct:.1f}%)"
                        },
                        priority=10  # URGENT
                    )
                    
        return None
        
    async def _update_positions(self, payload: dict):
        """Zaktualizuj tracking pozycji"""
        token = payload.get("token", "")
        action = payload.get("action", "")
        
        if action == "BUY":
            self.positions[token] = {
                "amount": payload.get("amount", 0),
                "entry_price": 0,  # TODO: Get actual price
                "timestamp": datetime.now().isoformat()
            }
        elif action == "SELL" and token in self.positions:
            if payload.get("percent", 0) >= 99:
                del self.positions[token]


# ═══════════════════════════════════════════════════════════════════════════════
# 🧠 ORCHESTRATOR - Zarządza całym systemem
# ═══════════════════════════════════════════════════════════════════════════════

class Orchestrator:
    """
    Główny orkiestrator systemu agentów
    
    Odpowiada za:
    - Uruchamianie/zatrzymywanie agentów
    - Routing wiadomości
    - Health checks
    - Logging i monitoring
    """
    
    def __init__(self, use_redis: bool = False):
        self.message_bus = MessageBus(use_redis=use_redis)
        self.agents: Dict[str, BaseAgent] = {}
        self.running = False
        
    async def start(self):
        """Uruchom system"""
        print("=" * 70)
        print("🐝 AGENT SWARM ORCHESTRATOR")
        print("   Multi-Agent Trading System")
        print("=" * 70)
        
        # Connect message bus
        await self.message_bus.connect()
        
        # Create agents
        self.agents["scanner"] = ScannerAgent(self.message_bus)
        self.agents["analyst"] = AnalystAgent(self.message_bus)
        self.agents["trader"] = TraderAgent(self.message_bus)
        self.agents["risk"] = RiskAgent(self.message_bus)
        
        # Start agents
        for name, agent in self.agents.items():
            await agent.start()
            
        self.running = True
        print("\n✅ All agents started!")
        print("📡 Message Bus active")
        print("🔄 System ready for trading")
        print("=" * 70)
        
        # Send startup notification
        await self._send_telegram("🐝 <b>AGENT SWARM</b> uruchomiony!\n\n4 agentów aktywnych:\n• Scanner\n• Analyst\n• Trader\n• Risk")
        
    async def stop(self):
        """Zatrzymaj system"""
        self.running = False
        
        for agent in self.agents.values():
            await agent.stop()
            
        await self.message_bus.disconnect()
        print("\n🛑 Agent Swarm stopped")
        
    async def run(self):
        """Główna pętla"""
        await self.start()
        
        try:
            while self.running:
                # Health check co minutę
                await asyncio.sleep(60)
                await self._health_check()
                
        except KeyboardInterrupt:
            await self.stop()
            
    async def _health_check(self):
        """Sprawdź status agentów"""
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 🔍 Health check...")
        
        for name, agent in self.agents.items():
            status = "🟢" if agent.running else "🔴"
            context_size = len(agent.context)
            print(f"  {status} {name}: context_size={context_size}")
            
    async def _send_telegram(self, message: str):
        """Wyślij na Telegram"""
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            return
            
        try:
            async with aiohttp.ClientSession() as session:
                await session.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                    json={
                        "chat_id": TELEGRAM_CHAT_ID,
                        "text": message,
                        "parse_mode": "HTML"
                    }
                )
        except Exception:
            pass  # Telegram alerts are non-critical
            
    # API dla zewnętrznych systemów
    async def inject_whale_alert(self, whale: str, amount: float, token: str):
        """Wstrzyknij whale alert do systemu"""
        scanner = self.agents.get("scanner")
        if scanner:
            await scanner.scan_whale_activity(whale, amount, token)
            
    async def inject_prices(self, prices: Dict[str, float]):
        """Wstrzyknij aktualizacje cen"""
        scanner = self.agents.get("scanner")
        if scanner:
            await scanner.monitor_prices(prices)


async def main():
    """Test mode"""
    orchestrator = Orchestrator(use_redis=False)
    
    try:
        await orchestrator.start()
        
        # Symuluj whale alert
        print("\n🐳 Symulacja whale alert...")
        await orchestrator.inject_whale_alert(
            "0x37556b2c49bebf840f2bec6e3c066fb93aee7f9e",
            1500.0,
            "0x5E1b1A14c8758104B8560514e94ab8320e587777"
        )
        
        # Poczekaj na przetworzenie
        await asyncio.sleep(10)
        
        # Health check
        await orchestrator._health_check()
        
    finally:
        await orchestrator.stop()


if __name__ == "__main__":
    asyncio.run(main())
