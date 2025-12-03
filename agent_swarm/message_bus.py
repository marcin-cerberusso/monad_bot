#!/usr/bin/env python3
"""
🚌 MESSAGE BUS V2 - Centralny bus komunikacyjny dla Agent Swarm

Obsługuje:
- Dragonfly (Redis-compatible) jako backend
- In-memory fallback gdy Dragonfly niedostępny
- Routing wiadomości
- Consensus management
- Walidacja payloadów
- Metryki i observability
- Rate limiting i backpressure
- ACL (Access Control List)
"""

import asyncio
import json
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Set
from dotenv import load_dotenv

load_dotenv()

# Próbuj zaimportować redis
try:
    import redis.asyncio as aioredis
    REDIS_AVAILABLE = True
except ImportError:
    aioredis = None  # type: ignore
    REDIS_AVAILABLE = False
    print("⚠️ redis package not installed - using in-memory bus")

from .message_types import (
    Message, MessageType, Priority,
    ConsensusRequestPayload, ConsensusVotePayload, ConsensusResultPayload,
    MessageBuilder
)

# Import nowych komponentów (lazy, żeby uniknąć circular imports)
def _get_metrics():
    from .bus_metrics import get_metrics
    return get_metrics()

def _get_validator():
    from .message_validator import get_validator
    return get_validator()

def _get_config():
    from .bus_config import get_config
    return get_config()

# Dragonfly/Redis connection
# URL should be set in .env file
DRAGONFLY_URL = os.getenv("DRAGONFLY_URL", "")

# Channels
CHANNEL_PREFIX = "monad_bot:"
CHANNELS = {
    "all": f"{CHANNEL_PREFIX}all",
    "scanner": f"{CHANNEL_PREFIX}scanner",
    "analyst": f"{CHANNEL_PREFIX}analyst",
    "trader": f"{CHANNEL_PREFIX}trader",
    "risk": f"{CHANNEL_PREFIX}risk",
    "consensus": f"{CHANNEL_PREFIX}consensus",
}


@dataclass
class PendingConsensus:
    """Pending consensus request"""
    request_id: str
    request: Message
    votes: Dict[str, str] = field(default_factory=dict)  # agent -> vote
    created_at: datetime = field(default_factory=datetime.now)
    timeout_seconds: float = 5.0
    min_approvals: int = 2
    resolved: bool = False
    result: Optional[bool] = None


class MessageBus:
    """
    Centralny Message Bus dla Agent Swarm
    
    Używa Dragonfly jako backend, z in-memory fallback.
    Features:
    - Backpressure z priority queues
    - Rate limiting per agent
    - Dynamiczne kanały
    - Ulepszony consensus z quorum
    """
    
    def __init__(self, agent_name: str):
        self.agent_name = agent_name
        self.redis: Optional[Any] = None
        self.pubsub: Optional[Any] = None
        self.connected = False
        self.running = False
        
        # Handlers dla różnych typów wiadomości
        self.handlers: Dict[MessageType, List[Callable]] = defaultdict(list)
        self.wildcard_handlers: List[Callable] = []
        
        # Consensus management
        self.pending_consensus: Dict[str, PendingConsensus] = {}
        
        # In-memory queues per priority (backpressure)
        self.priority_queues: Dict[Priority, asyncio.Queue] = {
            Priority.CRITICAL: asyncio.Queue(maxsize=100),
            Priority.URGENT: asyncio.Queue(maxsize=200),
            Priority.HIGH: asyncio.Queue(maxsize=500),
            Priority.NORMAL: asyncio.Queue(maxsize=1000),
            Priority.LOW: asyncio.Queue(maxsize=500),
        }
        self.local_queue: asyncio.Queue = asyncio.Queue()  # Legacy fallback
        
        # Rate limiting
        self._rate_limit_tokens: float = 50.0  # Token bucket
        self._rate_limit_last_update: float = time.time()
        self._rate_limit_max_tokens: float = 50.0
        self._rate_limit_refill_rate: float = 100.0  # tokens per second
        
        # Allowed communications (isolation)
        self.allowed_senders: Set[str] = set()  # puste = wszyscy dozwoleni
        
        # Dynamic channel registry
        self._registered_channels: Set[str] = set()
        
        # Stats
        self.stats = {
            "messages_sent": 0,
            "messages_received": 0,
            "consensus_requests": 0,
            "errors": 0,
            "rate_limited": 0,
            "dropped_low_priority": 0,
        }
        
    def _refill_rate_limit_tokens(self):
        """Uzupełnij tokeny rate limitera (token bucket algorithm)"""
        now = time.time()
        elapsed = now - self._rate_limit_last_update
        self._rate_limit_tokens = min(
            self._rate_limit_max_tokens,
            self._rate_limit_tokens + elapsed * self._rate_limit_refill_rate
        )
        self._rate_limit_last_update = now
        
    def _check_rate_limit(self, priority: Priority) -> bool:
        """Sprawdź czy możemy wysłać (rate limiting)"""
        config = _get_config()
        if not config.rate_limit.enabled:
            return True
            
        # CRITICAL zawsze przechodzi
        if priority == Priority.CRITICAL and config.rate_limit.critical_no_limit:
            return True
            
        self._refill_rate_limit_tokens()
        
        # HIGH/URGENT zużywają mniej tokenów
        cost = 1.0
        if priority in (Priority.HIGH, Priority.URGENT):
            cost = 1.0 / config.rate_limit.high_priority_multiplier
            
        if self._rate_limit_tokens >= cost:
            self._rate_limit_tokens -= cost
            return True
        else:
            self.stats["rate_limited"] += 1
            return False
        
    async def connect(self) -> bool:
        """Połącz z Dragonfly"""
        config = _get_config()
        
        # Sprawdź tryb pracy
        from .bus_config import BusMode
        if config.mode == BusMode.LOCAL:
            print(f"🚌 [{self.agent_name}] Using in-memory bus (LOCAL mode)")
            self.connected = False
            return False
            
        if not REDIS_AVAILABLE:
            if config.mode == BusMode.NETWORK:
                raise RuntimeError("NETWORK mode requires redis package")
            print(f"🚌 [{self.agent_name}] Using in-memory bus (no redis)")
            self.connected = False
            return False
            
        if not DRAGONFLY_URL:
            if config.mode == BusMode.NETWORK:
                raise RuntimeError("NETWORK mode requires DRAGONFLY_URL environment variable")
            print(f"⚠️ [{self.agent_name}] DRAGONFLY_URL not set, using in-memory")
            self.connected = False
            return False
            
        try:
            self.redis = aioredis.from_url(
                DRAGONFLY_URL, 
                decode_responses=True,
                socket_timeout=config.connection.socket_timeout,
                socket_connect_timeout=config.connection.socket_connect_timeout
            )
            await self.redis.ping()
            self.pubsub = self.redis.pubsub()
            self.connected = True
            print(f"🐉 [{self.agent_name}] Connected to Dragonfly")
            return True
        except Exception as e:
            if config.mode == BusMode.NETWORK:
                raise RuntimeError(f"NETWORK mode: Dragonfly connection failed: {e}")
            print(f"❌ [{self.agent_name}] Dragonfly connection failed: {e}")
            print(f"🚌 [{self.agent_name}] Falling back to in-memory bus")
            self.connected = False
            return False
            
    async def disconnect(self):
        """Rozłącz"""
        self.running = False
        if self.pubsub:
            await self.pubsub.close()
        if self.redis:
            await self.redis.close()
        print(f"🚌 [{self.agent_name}] Disconnected")
        
    async def subscribe(self, *channels: str):
        """Subskrybuj kanały"""
        if not self.connected or not self.pubsub:
            return
            
        full_channels = []
        for ch in channels:
            if ch in CHANNELS:
                full_channels.append(CHANNELS[ch])
            else:
                full_channels.append(f"{CHANNEL_PREFIX}{ch}")
                
        # Zawsze subskrybuj "all"
        if CHANNELS["all"] not in full_channels:
            full_channels.append(CHANNELS["all"])
            
        await self.pubsub.subscribe(*full_channels)
        print(f"📥 [{self.agent_name}] Subscribed: {channels}")
        
    def on(self, message_type: MessageType):
        """Dekorator do obsługi wiadomości danego typu"""
        def decorator(func: Callable):
            self.handlers[message_type].append(func)
            return func
        return decorator
        
    def on_any(self):
        """Dekorator do obsługi wszystkich wiadomości"""
        def decorator(func: Callable):
            self.wildcard_handlers.append(func)
            return func
        return decorator
        
    async def publish(self, message: Message, channel: str = "all", 
                      validate: bool = True, retry: bool = True):
        """Wyślij wiadomość z walidacją, rate limiting i retry"""
        # Rate limiting check
        priority = message.priority if hasattr(message, 'priority') else Priority.NORMAL
        if not self._check_rate_limit(priority):
            print(f"⚠️ [{self.agent_name}] Rate limited, dropping: {message.type.value}")
            return
            
        # Walidacja
        if validate:
            try:
                config = _get_config()
                if config.validation.enabled:
                    result = _get_validator().validate(message)
                    if not result.valid:
                        if config.validation.strict:
                            raise ValueError(f"Invalid message: {result}")
                        else:
                            print(f"⚠️ [{self.agent_name}] Validation warnings: {result.warnings}")
            except Exception as e:
                _get_metrics().record_error("validation", str(e))
                raise
        
        self.stats["messages_sent"] += 1
        json_data = message.to_json()
        size_bytes = len(json_data.encode('utf-8'))
        
        # Metryki
        metrics = _get_metrics()
        metrics.record_send(channel, message.type, message.id, size_bytes)
        
        if self.connected and self.redis:
            # Dragonfly pub z retry
            ch = CHANNELS.get(channel, f"{CHANNEL_PREFIX}{channel}")
            
            config = _get_config()
            attempts = config.connection.retry_attempts if retry else 1
            delay = config.connection.retry_delay_seconds
            
            for attempt in range(attempts):
                try:
                    await self.redis.publish(ch, json_data)
                    break
                except Exception as e:
                    if attempt < attempts - 1:
                        print(f"⚠️ [{self.agent_name}] Publish failed, retrying in {delay}s...")
                        await asyncio.sleep(delay)
                        delay *= config.connection.retry_backoff_multiplier
                    else:
                        metrics.record_error("publish", str(e))
                        raise
        else:
            # In-memory
            await self.local_queue.put(message)
            
        print(f"📤 [{self.agent_name}] → {channel}: {message.type.value}")
        
    async def send_to(self, recipient: str, message: Message):
        """Wyślij wiadomość do konkretnego agenta"""
        message.recipient = recipient
        await self.publish(message, channel=recipient)
        
    async def broadcast(self, message: Message):
        """Broadcast do wszystkich"""
        message.recipient = "all"
        await self.publish(message, channel="all")
        
    async def listen(self):
        """Nasłuchuj wiadomości z exponential backoff na błędach"""
        self.running = True
        print(f"👂 [{self.agent_name}] Listening...")
        
        config = _get_config()
        error_delay = 0.5
        max_error_delay = 30.0
        
        while self.running:
            try:
                if self.connected and self.pubsub:
                    # Dragonfly z konfigurowalnym timeout
                    message = await self.pubsub.get_message(
                        ignore_subscribe_messages=True,
                        timeout=config.connection.pubsub_timeout
                    )
                    if message and message["type"] == "message":
                        await self._handle_raw_message(message["data"])
                        error_delay = 0.5  # Reset on success
                else:
                    # In-memory
                    try:
                        message = await asyncio.wait_for(
                            self.local_queue.get(), 
                            timeout=0.1
                        )
                        await self._handle_message(message)
                    except asyncio.TimeoutError:
                        pass
                        
                # Process pending consensus timeouts
                await self._check_consensus_timeouts()
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.stats["errors"] += 1
                _get_metrics().record_error("listen", str(e))
                print(f"❌ [{self.agent_name}] Listen error: {e}")
                # Exponential backoff
                await asyncio.sleep(error_delay)
                error_delay = min(error_delay * 2, max_error_delay)

    # Maximum message size (64KB)
    MAX_MESSAGE_SIZE = 64 * 1024
                
    async def _handle_raw_message(self, data: str):
        """Parse i obsłuż wiadomość z Dragonfly"""
        try:
            # SECURITY: Check message size before parsing
            size_bytes = len(data.encode('utf-8'))
            if size_bytes > self.MAX_MESSAGE_SIZE:
                _get_metrics().record_error("size_limit", f"Message too large: {size_bytes} bytes")
                print(f"⚠️ [{self.agent_name}] Dropping oversized message: {size_bytes} bytes")
                return
                
            message = Message.from_json(data)
            # Record receive metric
            _get_metrics().record_receive("all", message.type, message.id, size_bytes)
            await self._handle_message(message)
        except Exception as e:
            _get_metrics().record_error("parse", str(e))
            print(f"❌ [{self.agent_name}] Parse error: {e}")
            
    async def _handle_message(self, message: Message):
        """Obsłuż wiadomość"""
        # Ignoruj własne wiadomości
        if message.sender == self.agent_name:
            return
            
        # Sprawdź czy wiadomość jest dla nas
        if message.recipient != "all" and message.recipient != self.agent_name:
            return
            
        # Sprawdź allowed senders (isolation) - używaj też ACL z config
        config = _get_config()
        if config.acl.enabled:
            if not config.acl.is_allowed(message.sender, self.agent_name):
                if config.acl.log_blocked:
                    print(f"🚫 [{self.agent_name}] ACL blocked message from {message.sender}")
                return
        
        # Legacy allowed_senders check
        if self.allowed_senders and message.sender not in self.allowed_senders:
            print(f"🚫 [{self.agent_name}] Blocked message from {message.sender}")
            return
            
        self.stats["messages_received"] += 1
        print(f"📨 [{self.agent_name}] ← {message.sender}: {message.type.value}")
        
        # Specjalna obsługa consensus
        if message.type == MessageType.CONSENSUS_REQUEST:
            await self._handle_consensus_request(message)
            return
        elif message.type == MessageType.CONSENSUS_VOTE:
            await self._handle_consensus_vote(message)
            return
        elif message.type == MessageType.AGENT_HEARTBEAT:
            # Zapisz heartbeat do health monitora
            try:
                from .health_monitor import get_health_monitor
                from .message_types import AgentHeartbeatPayload
                payload_dict = message.payload if isinstance(message.payload, dict) else message.payload.to_dict()
                heartbeat = AgentHeartbeatPayload(**payload_dict)
                get_health_monitor().record_heartbeat(message.sender, heartbeat)
            except Exception:
                pass  # Health monitor jest opcjonalny
            
        # Wywołaj handlery
        handlers = self.handlers.get(message.type, [])
        for handler in handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(message)
                else:
                    handler(message)
            except Exception as e:
                _get_metrics().record_handler_error(message.type, e)
                print(f"❌ [{self.agent_name}] Handler error: {e}")
                
        # Wildcard handlers
        for handler in self.wildcard_handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(message)
                else:
                    handler(message)
            except Exception as e:
                _get_metrics().record_handler_error(message.type, e)
                print(f"❌ [{self.agent_name}] Wildcard handler error: {e}")
                
    # === CONSENSUS ===
    
    async def request_consensus(self, payload: ConsensusRequestPayload, 
                                 voters: Optional[List[str]] = None) -> bool:
        """
        Poproś o consensus i czekaj na wynik.
        
        Args:
            payload: Dane żądania consensus
            voters: Opcjonalna lista wymaganych głosujących. Jeśli None,
                   używa konfiguracji z ConsensusConfig.voters_for_action
                   
        Returns:
            True jeśli zatwierdzono, False jeśli odrzucono
        """
        request_id = f"{self.agent_name}:{datetime.now().timestamp()}"
        config = _get_config()
        
        # Metryki
        _get_metrics().record_consensus_request()
        
        # Określ wymaganych głosujących
        if voters is None:
            action = payload.action if hasattr(payload, 'action') else 'buy'
            action_str = action.value if hasattr(action, 'value') else str(action).lower()
            voters = config.consensus.voters_for_action.get(action_str, [])
        
        # Utwórz pending
        pending = PendingConsensus(
            request_id=request_id,
            request=MessageBuilder.consensus_request(self.agent_name, payload),
            timeout_seconds=payload.timeout_seconds,
            min_approvals=payload.min_approvals
        )
        pending.request.id = request_id
        pending.expected_voters = voters  # type: ignore
        self.pending_consensus[request_id] = pending
        self.stats["consensus_requests"] += 1
        
        # Zapisz w Dragonfly żeby inne agenty mogły głosować
        if self.connected and self.redis:
            await self.redis.set(
                f"consensus:{request_id}:votes", 
                json.dumps({}),
                ex=int(payload.timeout_seconds) + 10
            )
        
        # Wyślij request
        await self.broadcast(pending.request)
        
        # Czekaj na wynik - sprawdzaj głosy w Dragonfly
        start = datetime.now()
        timed_out = False
        while not pending.resolved:
            elapsed = (datetime.now() - start).total_seconds()
            if elapsed >= payload.timeout_seconds:
                timed_out = True
                break
                
            # Pobierz głosy z Dragonfly
            if self.connected and self.redis:
                votes_json = await self.redis.get(f"consensus:{request_id}:votes")
                if votes_json:
                    pending.votes = json.loads(votes_json)
                    
            # Sprawdź czy mamy wystarczająco głosów (z wagami)
            weighted_approvals = self._calculate_weighted_votes(pending.votes, "approve", config)
            weighted_min = float(payload.min_approvals)
            if weighted_approvals >= weighted_min:
                pending.resolved = True
                break
                
            # Sprawdź veto
            if self._check_veto(pending.votes, config):
                pending.resolved = True
                pending.result = False  # Veto = odrzucenie
                break
                
            await asyncio.sleep(0.2)
            
        # Oceń wynik końcowy z wagami
        weighted_approvals = self._calculate_weighted_votes(pending.votes, "approve", config)
        weighted_rejections = self._calculate_weighted_votes(pending.votes, "reject", config)
        
        # Sprawdź quorum (jeśli włączony)
        quorum_met = True
        if config.consensus.require_quorum and hasattr(pending, 'expected_voters'):
            expected_count = len(pending.expected_voters) if pending.expected_voters else 1
            vote_count = len(pending.votes)
            quorum_met = (vote_count / expected_count) * 100 >= config.consensus.quorum_percentage
            
        # Sprawdź veto
        vetoed = self._check_veto(pending.votes, config)
        
        approved = (weighted_approvals >= float(payload.min_approvals) and 
                   weighted_approvals > weighted_rejections and 
                   quorum_met and 
                   not vetoed)
        pending.resolved = True
        pending.result = approved
        
        # Metryki consensus
        total_votes = len(pending.votes)
        _get_metrics().record_consensus_result(approved, total_votes, timed_out)
        
        # Policz głosy (dla raportu)
        approve_count = sum(1 for v in pending.votes.values() if v == "approve")
        reject_count = sum(1 for v in pending.votes.values() if v == "reject")
        
        # Wyślij wynik
        result_payload = ConsensusResultPayload(
            request_id=request_id,
            approved=approved,
            votes_approve=approve_count,
            votes_reject=reject_count,
            votes_abstain=len(pending.votes) - approve_count - reject_count
        )
        await self.broadcast(Message(
            type=MessageType.CONSENSUS_RESULT,
            sender=self.agent_name,
            payload=result_payload.to_dict(),
            priority=Priority.HIGH
        ))
        
        # Cleanup
        del self.pending_consensus[request_id]
        
        status = '✅ APPROVED' if approved else '❌ REJECTED'
        extras = []
        if vetoed:
            extras.append("VETOED")
        if not quorum_met:
            extras.append("NO QUORUM")
        extra_str = f" ({', '.join(extras)})" if extras else ""
        
        print(f"🗳️ [{self.agent_name}] Consensus: {status}{extra_str} "
              f"(weighted: {weighted_approvals:.1f}/{payload.min_approvals} needed)")
        
        return approved
        
    async def _handle_consensus_request(self, message: Message):
        """Obsłuż request o consensus"""
        # Automatyczne głosowanie - można nadpisać handlerem
        handlers = self.handlers.get(MessageType.CONSENSUS_REQUEST, [])
        
        if handlers:
            for handler in handlers:
                try:
                    if asyncio.iscoroutinefunction(handler):
                        await handler(message)
                    else:
                        handler(message)
                except Exception as e:
                    print(f"❌ Consensus handler error: {e}")
        else:
            # Domyślnie: abstain
            await self.vote(message.id, "abstain", "no handler")
    def _calculate_weighted_votes(self, votes: Dict[str, str], vote_type: str, 
                                     config) -> float:
        """
        Oblicz sumę ważonych głosów danego typu.
        
        Args:
            votes: Dict {agent_name: vote_decision}
            vote_type: "approve", "reject", "abstain"
            config: BusConfig
            
        Returns:
            Suma wag głosów danego typu
        """
        total = 0.0
        for voter, vote in votes.items():
            if vote == vote_type:
                weight = config.consensus.voter_weights.get(voter, 1.0)
                total += weight
        return total
        
    def _check_veto(self, votes: Dict[str, str], config) -> bool:
        """
        Sprawdź czy ktoś z uprawnieniami veto odrzucił.
        
        Args:
            votes: Dict {agent_name: vote_decision}
            config: BusConfig
            
        Returns:
            True jeśli jest veto (ktoś z veto_agents głosował "reject")
        """
        for voter, vote in votes.items():
            if vote == "reject" and voter in config.consensus.veto_agents:
                print(f"🚫 Veto from {voter}!")
                return True
        return False
            
    async def _handle_consensus_vote(self, message: Message):
        """Obsłuż głos - zapisz do Dragonfly"""
        payload = message.payload
        request_id = payload.get("request_id", "")
        voter = message.sender
        vote = payload.get("vote", "abstain")
        
        # Zapisz głos do Dragonfly (shared state)
        if self.connected and self.redis:
            try:
                key = f"consensus:{request_id}:votes"
                votes_json = await self.redis.get(key)
                votes = json.loads(votes_json) if votes_json else {}
                votes[voter] = vote
                await self.redis.set(key, json.dumps(votes), ex=120)
                print(f"📥 [{self.agent_name}] Recorded vote from {voter}: {vote}")
            except Exception as e:
                print(f"⚠️ Failed to save vote to Dragonfly: {e}")
        
        # Lokalnie też zaktualizuj (dla przypadku bez Redis)
        if request_id in self.pending_consensus:
            pending = self.pending_consensus[request_id]
            pending.votes[voter] = vote
            
            # Sprawdź czy mamy już wynik
            approvals = sum(1 for v in pending.votes.values() if v == "approve")
            if approvals >= pending.min_approvals:
                pending.resolved = True
                pending.result = True
                
    async def vote(self, request_id: str, decision: str, reason: str = ""):
        """Zagłosuj na consensus - zapisz do Dragonfly"""
        # Najpierw zapisz bezpośrednio do Dragonfly (nie czekaj na broadcast)
        if self.connected and self.redis:
            try:
                key = f"consensus:{request_id}:votes"
                votes_json = await self.redis.get(key)
                votes = json.loads(votes_json) if votes_json else {}
                votes[self.agent_name] = decision
                await self.redis.set(key, json.dumps(votes), ex=120)
                print(f"📤 [{self.agent_name}] Saved vote directly to Dragonfly: {decision}")
            except Exception as e:
                print(f"⚠️ Failed to save vote to Dragonfly: {e}")
        
        # Też wyślij przez pub/sub (dla kompatybilności)
        payload = ConsensusVotePayload(
            request_id=request_id,
            vote=decision,
            reason=reason
        )
        await self.broadcast(MessageBuilder.consensus_vote(
            self.agent_name, request_id, payload
        ))
        print(f"🗳️ [{self.agent_name}] Vote: {decision} on {request_id[:16]}...")
        
    async def _check_consensus_timeouts(self):
        """Sprawdź timeouty consensus"""
        now = datetime.now()
        for request_id, pending in list(self.pending_consensus.items()):
            if pending.resolved:
                continue
            elapsed = (now - pending.created_at).total_seconds()
            if elapsed >= pending.timeout_seconds:
                pending.resolved = True
                
    # === CONVENIENCE METHODS ===
    
    async def signal_whale_alert(self, whale: str, token: str, action: str, 
                                  amount: float, **kwargs):
        """Wyślij whale alert"""
        from .message_types import WhaleAlertPayload, TradeAction
        payload = WhaleAlertPayload(
            whale_address=whale,
            whale_name=kwargs.get("whale_name", whale[:10]),
            token_address=token,
            token_name=kwargs.get("token_name", token[:10]),
            action=TradeAction.BUY if action.lower() == "buy" else TradeAction.SELL,
            amount_mon=amount,
            **{k: v for k, v in kwargs.items() if k not in ["whale_name", "token_name"]}
        )
        await self.broadcast(MessageBuilder.whale_alert(self.agent_name, payload))
        
    async def signal_new_token(self, token: str, name: str, symbol: str, 
                                score: int = 0, **kwargs):
        """Wyślij new token alert"""
        from .message_types import NewTokenPayload
        payload = NewTokenPayload(
            token_address=token,
            token_name=name,
            token_symbol=symbol,
            creator=kwargs.get("creator", ""),
            quality_score=score,
            **{k: v for k, v in kwargs.items() if k != "creator"}
        )
        await self.broadcast(MessageBuilder.new_token(self.agent_name, payload))
        
    async def signal_trade(self, action: str, token: str, amount: float = 0, 
                           percent: float = 100, reason: str = "", 
                           requires_consensus: bool = False):
        """Wyślij trade signal"""
        from .message_types import TradeSignalPayload, TradeAction
        
        trade_action = TradeAction.BUY if action.lower() == "buy" else TradeAction.SELL
        payload = TradeSignalPayload(
            action=trade_action,
            token_address=token,
            token_name=token[:12],
            amount_mon=amount,
            sell_percent=percent,
            reason=reason,
            source_signal=self.agent_name
        )
        await self.broadcast(MessageBuilder.trade_signal(
            self.agent_name, payload, requires_consensus
        ))
        
    async def signal_risk_alert(self, level: str, message: str, token: Optional[str] = None):
        """Wyślij risk alert"""
        from .message_types import RiskAlertPayload, RiskLevel
        
        risk_level = RiskLevel.CRITICAL if level == "critical" else \
                     RiskLevel.HIGH if level == "high" else \
                     RiskLevel.MEDIUM if level == "medium" else RiskLevel.LOW
                     
        payload = RiskAlertPayload(
            level=risk_level,
            message=message,
            token_address=token
        )
        await self.broadcast(MessageBuilder.risk_alert(self.agent_name, payload))
        
    async def send_heartbeat(self, status: str = "running", task: str = ""):
        """Wyślij heartbeat"""
        from .message_types import AgentHeartbeatPayload
        import psutil
        
        payload = AgentHeartbeatPayload(
            agent_name=self.agent_name,
            status=status,
            current_task=task,
            memory_mb=psutil.Process().memory_info().rss / 1024 / 1024
        )
        await self.broadcast(MessageBuilder.heartbeat(self.agent_name, payload))
        
    # === STATE (via Dragonfly) ===
    
    async def set_state(self, key: str, value: Any, ttl: Optional[int] = None):
        """Zapisz stan w Dragonfly"""
        if not self.connected or not self.redis:
            return
        full_key = f"state:{self.agent_name}:{key}"
        await self.redis.set(full_key, json.dumps(value))
        if ttl:
            await self.redis.expire(full_key, ttl)
            
    async def get_state(self, key: str) -> Any:
        """Pobierz stan z Dragonfly"""
        if not self.connected or not self.redis:
            return None
        full_key = f"state:{self.agent_name}:{key}"
        data = await self.redis.get(full_key)
        return json.loads(data) if data else None
        
    async def get_shared_state(self, key: str) -> Any:
        """Pobierz shared state"""
        if not self.connected or not self.redis:
            return None
        data = await self.redis.get(f"shared:{key}")
        return json.loads(data) if data else None
        
    async def set_shared_state(self, key: str, value: Any, ttl: Optional[int] = None):
        """Zapisz shared state"""
        if not self.connected or not self.redis:
            return
        await self.redis.set(f"shared:{key}", json.dumps(value))
        if ttl:
            await self.redis.expire(f"shared:{key}", ttl)

    # === DYNAMIC CHANNELS ===
    
    async def register_channel(self, channel_name: str, persist: bool = True) -> str:
        """
        Rejestruj nowy dynamiczny kanał.
        
        Args:
            channel_name: Nazwa kanału (bez prefixu)
            persist: Czy zapisać w Dragonfly dla innych agentów
            
        Returns:
            Pełna nazwa kanału z prefixem
        """
        full_channel = f"{CHANNEL_PREFIX}{channel_name}"
        self._registered_channels.add(full_channel)
        
        if persist and self.connected and self.redis:
            # Zapisz w zbiorze kanałów w Dragonfly
            await self.redis.sadd("registered_channels", full_channel)
            
        print(f"📺 [{self.agent_name}] Registered channel: {channel_name}")
        return full_channel
        
    async def unregister_channel(self, channel_name: str):
        """Wyrejestruj kanał"""
        full_channel = f"{CHANNEL_PREFIX}{channel_name}"
        self._registered_channels.discard(full_channel)
        
        if self.connected and self.redis:
            await self.redis.srem("registered_channels", full_channel)
            
    async def get_registered_channels(self) -> Set[str]:
        """Pobierz wszystkie zarejestrowane kanały (lokalne + z Dragonfly)"""
        channels = set(self._registered_channels)
        
        if self.connected and self.redis:
            remote_channels = await self.redis.smembers("registered_channels")
            channels.update(remote_channels)
            
        return channels
        
    async def subscribe_dynamic(self, channel_name: str, register_if_missing: bool = True):
        """
        Subskrybuj dynamiczny kanał.
        
        Args:
            channel_name: Nazwa kanału (bez prefixu)
            register_if_missing: Czy zarejestrować jeśli nie istnieje
        """
        full_channel = f"{CHANNEL_PREFIX}{channel_name}"
        
        if register_if_missing and full_channel not in self._registered_channels:
            await self.register_channel(channel_name)
            
        if self.connected and self.pubsub:
            await self.pubsub.subscribe(full_channel)
            print(f"📥 [{self.agent_name}] Subscribed to dynamic channel: {channel_name}")
            
    async def create_topic_channel(self, topic: str, participants: List[str]) -> str:
        """
        Utwórz kanał tematyczny dla grupy agentów.
        
        Args:
            topic: Temat (np. "whale_tracking", "token_0x123")
            participants: Lista agentów którzy powinni subskrybować
            
        Returns:
            Nazwa kanału
        """
        channel_name = f"topic:{topic}"
        full_channel = await self.register_channel(channel_name)
        
        # Zapisz uczestników
        if self.connected and self.redis:
            await self.redis.sadd(f"channel_participants:{channel_name}", *participants)
            
        return channel_name


# === FACTORY ===

_buses: Dict[str, MessageBus] = {}

async def get_bus(agent_name: str) -> MessageBus:
    """Pobierz lub utwórz bus dla agenta"""
    if agent_name not in _buses:
        bus = MessageBus(agent_name)
        await bus.connect()
        _buses[agent_name] = bus
    return _buses[agent_name]


async def shutdown_all():
    """Zamknij wszystkie busy"""
    for bus in _buses.values():
        await bus.disconnect()
    _buses.clear()
