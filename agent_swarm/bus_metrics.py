#!/usr/bin/env python3
"""
📊 BUS METRICS - Observability dla MessageBus

Metryki:
- messages_sent/received per channel/type
- latency publish→handle
- consensus timeouts/success rate
- handler errors
- agent health (heartbeats)
"""

import asyncio
import json
import time
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set
from enum import Enum

from .message_types import MessageType


@dataclass
class ChannelMetrics:
    """Metryki per kanał"""
    messages_sent: int = 0
    messages_received: int = 0
    bytes_sent: int = 0
    bytes_received: int = 0
    last_message_at: Optional[datetime] = None
    
    
@dataclass  
class TypeMetrics:
    """Metryki per typ wiadomości"""
    count: int = 0
    total_latency_ms: float = 0
    max_latency_ms: float = 0
    min_latency_ms: float = float('inf')
    errors: int = 0
    
    @property
    def avg_latency_ms(self) -> float:
        return self.total_latency_ms / self.count if self.count > 0 else 0
    

@dataclass
class ConsensusMetrics:
    """Metryki consensus"""
    requests: int = 0
    approved: int = 0
    rejected: int = 0
    timeouts: int = 0
    avg_votes: float = 0
    total_votes: int = 0
    
    @property
    def success_rate(self) -> float:
        total = self.approved + self.rejected + self.timeouts
        return self.approved / total if total > 0 else 0
    

@dataclass
class AgentHealth:
    """Status zdrowia agenta"""
    agent_name: str
    last_heartbeat: Optional[datetime] = None
    status: str = "unknown"
    current_task: str = ""
    memory_mb: float = 0
    messages_sent: int = 0
    messages_received: int = 0
    is_alive: bool = False
    
    def update(self, heartbeat_data: dict):
        self.last_heartbeat = datetime.now()
        self.status = heartbeat_data.get("status", "running")
        self.current_task = heartbeat_data.get("current_task", "")
        self.memory_mb = heartbeat_data.get("memory_mb", 0)
        self.is_alive = True
        
    def check_alive(self, timeout_seconds: float = 30.0) -> bool:
        if not self.last_heartbeat:
            return False
        elapsed = (datetime.now() - self.last_heartbeat).total_seconds()
        self.is_alive = elapsed < timeout_seconds
        return self.is_alive


class BusMetrics:
    """
    Centralny collector metryk dla MessageBus
    """
    
    def __init__(self):
        # Per channel
        self.channels: Dict[str, ChannelMetrics] = defaultdict(ChannelMetrics)
        
        # Per message type
        self.types: Dict[MessageType, TypeMetrics] = defaultdict(TypeMetrics)
        
        # Consensus
        self.consensus = ConsensusMetrics()
        
        # Agent health
        self.agents: Dict[str, AgentHealth] = {}
        
        # Errors
        self.errors: List[Dict[str, Any]] = []
        self.max_errors = 100  # Keep last N errors
        
        # Global
        self.start_time = datetime.now()
        self.total_messages = 0
        
        # Pending latency tracking
        self._pending_messages: Dict[str, float] = {}  # message_id -> timestamp
        
    def record_send(self, channel: str, msg_type: MessageType, message_id: str, size_bytes: int):
        """Zapisz wysłanie wiadomości"""
        self.channels[channel].messages_sent += 1
        self.channels[channel].bytes_sent += size_bytes
        self.channels[channel].last_message_at = datetime.now()
        
        self.types[msg_type].count += 1
        self.total_messages += 1
        
        # Track for latency
        self._pending_messages[message_id] = time.time()
        
    def record_receive(self, channel: str, msg_type: MessageType, message_id: str, size_bytes: int):
        """Zapisz odebranie wiadomości"""
        self.channels[channel].messages_received += 1
        self.channels[channel].bytes_received += size_bytes
        
        # Calculate latency if we have the send timestamp
        if message_id in self._pending_messages:
            latency_ms = (time.time() - self._pending_messages[message_id]) * 1000
            self.types[msg_type].total_latency_ms += latency_ms
            self.types[msg_type].max_latency_ms = max(self.types[msg_type].max_latency_ms, latency_ms)
            self.types[msg_type].min_latency_ms = min(self.types[msg_type].min_latency_ms, latency_ms)
            del self._pending_messages[message_id]
            
    def record_error(self, error_type: str, message: str, details: Optional[dict] = None):
        """Zapisz błąd"""
        error = {
            "timestamp": datetime.now().isoformat(),
            "type": error_type,
            "message": message,
            "details": details or {}
        }
        self.errors.append(error)
        
        # Keep only last N
        if len(self.errors) > self.max_errors:
            self.errors = self.errors[-self.max_errors:]
            
    def record_handler_error(self, msg_type: MessageType, error: Exception):
        """Zapisz błąd handlera"""
        self.types[msg_type].errors += 1
        self.record_error("handler_error", str(error), {"message_type": msg_type.value})
        
    def record_consensus_request(self):
        """Zapisz request consensus"""
        self.consensus.requests += 1
        
    def record_consensus_result(self, approved: bool, votes: int, timed_out: bool = False):
        """Zapisz wynik consensus"""
        if timed_out:
            self.consensus.timeouts += 1
        elif approved:
            self.consensus.approved += 1
        else:
            self.consensus.rejected += 1
            
        self.consensus.total_votes += votes
        total = self.consensus.approved + self.consensus.rejected + self.consensus.timeouts
        self.consensus.avg_votes = self.consensus.total_votes / total if total > 0 else 0
        
    def record_heartbeat(self, agent_name: str, data: dict):
        """Zapisz heartbeat agenta"""
        if agent_name not in self.agents:
            self.agents[agent_name] = AgentHealth(agent_name=agent_name)
        self.agents[agent_name].update(data)
        
    def check_agent_health(self, timeout_seconds: float = 30.0) -> Dict[str, bool]:
        """Sprawdź zdrowie wszystkich agentów"""
        results = {}
        for name, agent in self.agents.items():
            results[name] = agent.check_alive(timeout_seconds)
        return results
        
    def get_dead_agents(self, timeout_seconds: float = 30.0) -> List[str]:
        """Pobierz listę martwych agentów"""
        dead = []
        for name, agent in self.agents.items():
            if not agent.check_alive(timeout_seconds):
                dead.append(name)
        return dead
        
    def to_dict(self) -> dict:
        """Eksportuj do dict (dla JSON/Prometheus)"""
        return {
            "uptime_seconds": (datetime.now() - self.start_time).total_seconds(),
            "total_messages": self.total_messages,
            "channels": {
                ch: {
                    "sent": m.messages_sent,
                    "received": m.messages_received,
                    "bytes_sent": m.bytes_sent,
                    "bytes_received": m.bytes_received,
                    "last_message": m.last_message_at.isoformat() if m.last_message_at else None
                }
                for ch, m in self.channels.items()
            },
            "message_types": {
                t.value: {
                    "count": m.count,
                    "avg_latency_ms": round(m.avg_latency_ms, 2),
                    "max_latency_ms": round(m.max_latency_ms, 2),
                    "min_latency_ms": round(m.min_latency_ms, 2) if m.min_latency_ms != float('inf') else 0,
                    "errors": m.errors
                }
                for t, m in self.types.items()
            },
            "consensus": {
                "requests": self.consensus.requests,
                "approved": self.consensus.approved,
                "rejected": self.consensus.rejected,
                "timeouts": self.consensus.timeouts,
                "success_rate": round(self.consensus.success_rate, 3),
                "avg_votes": round(self.consensus.avg_votes, 1)
            },
            "agents": {
                name: {
                    "status": a.status,
                    "is_alive": a.is_alive,
                    "last_heartbeat": a.last_heartbeat.isoformat() if a.last_heartbeat else None,
                    "memory_mb": round(a.memory_mb, 1),
                    "current_task": a.current_task
                }
                for name, a in self.agents.items()
            },
            "recent_errors": self.errors[-10:]  # Last 10 errors
        }
        
    def to_prometheus(self) -> str:
        """Eksportuj w formacie Prometheus"""
        lines = []
        
        # Global
        lines.append(f"monad_bus_uptime_seconds {(datetime.now() - self.start_time).total_seconds():.0f}")
        lines.append(f"monad_bus_messages_total {self.total_messages}")
        
        # Per channel
        for ch, m in self.channels.items():
            lines.append(f'monad_bus_channel_sent{{channel="{ch}"}} {m.messages_sent}')
            lines.append(f'monad_bus_channel_received{{channel="{ch}"}} {m.messages_received}')
            
        # Per type
        for t, m in self.types.items():
            lines.append(f'monad_bus_type_count{{type="{t.value}"}} {m.count}')
            lines.append(f'monad_bus_type_latency_avg{{type="{t.value}"}} {m.avg_latency_ms:.2f}')
            lines.append(f'monad_bus_type_errors{{type="{t.value}"}} {m.errors}')
            
        # Consensus
        lines.append(f"monad_bus_consensus_requests {self.consensus.requests}")
        lines.append(f"monad_bus_consensus_approved {self.consensus.approved}")
        lines.append(f"monad_bus_consensus_rejected {self.consensus.rejected}")
        lines.append(f"monad_bus_consensus_timeouts {self.consensus.timeouts}")
        
        # Agents
        for name, a in self.agents.items():
            alive = 1 if a.is_alive else 0
            lines.append(f'monad_bus_agent_alive{{agent="{name}"}} {alive}')
            lines.append(f'monad_bus_agent_memory_mb{{agent="{name}"}} {a.memory_mb:.1f}')
            
        return "\n".join(lines)
        
    def to_json(self) -> str:
        """Eksportuj jako JSON"""
        return json.dumps(self.to_dict(), indent=2)


# Singleton metrics instance
_metrics: Optional[BusMetrics] = None

def get_metrics() -> BusMetrics:
    """Pobierz singleton metryk"""
    global _metrics
    if _metrics is None:
        _metrics = BusMetrics()
    return _metrics
    
def reset_metrics():
    """Reset metryk (dla testów)"""
    global _metrics
    _metrics = BusMetrics()
