#!/usr/bin/env python3
"""
🏥 HEALTH MONITOR - Monitoring zdrowia agentów

Funkcje:
- Śledzenie heartbeatów
- Alertowanie o martwych agentach
- Auto-recovery (opcjonalne)
- Status dashboard
"""

import asyncio
import json
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set
from enum import Enum

from .bus_config import get_config, HealthConfig
from .bus_metrics import get_metrics, AgentHealth
from .message_types import MessageType, Message, AgentHeartbeatPayload


class AgentStatus(Enum):
    """Status agenta"""
    UNKNOWN = "unknown"
    STARTING = "starting"
    RUNNING = "running"
    IDLE = "idle"
    BUSY = "busy"
    DEGRADED = "degraded"
    DEAD = "dead"
    STOPPED = "stopped"


@dataclass
class AgentInfo:
    """Pełne info o agencie"""
    name: str
    status: AgentStatus = AgentStatus.UNKNOWN
    last_heartbeat: Optional[datetime] = None
    last_message_sent: Optional[datetime] = None
    last_message_received: Optional[datetime] = None
    current_task: str = ""
    memory_mb: float = 0
    error_count: int = 0
    restart_count: int = 0
    started_at: Optional[datetime] = None
    
    # Custom metrics
    custom_metrics: Dict[str, Any] = field(default_factory=dict)
    
    @property
    def uptime_seconds(self) -> float:
        if not self.started_at:
            return 0
        return (datetime.now() - self.started_at).total_seconds()
    
    @property
    def seconds_since_heartbeat(self) -> float:
        if not self.last_heartbeat:
            return float('inf')
        return (datetime.now() - self.last_heartbeat).total_seconds()
        
    def is_alive(self, timeout_seconds: float = 30.0) -> bool:
        return self.seconds_since_heartbeat < timeout_seconds


@dataclass
class HealthAlert:
    """Alert zdrowia"""
    agent_name: str
    alert_type: str  # dead, degraded, high_memory, etc
    message: str
    timestamp: datetime = field(default_factory=datetime.now)
    resolved: bool = False
    resolved_at: Optional[datetime] = None


class HealthMonitor:
    """
    Monitor zdrowia agentów
    
    Śledzi heartbeaty i alertuje o problemach.
    """
    
    def __init__(self, config: Optional[HealthConfig] = None):
        self.config = config or get_config().health
        self.agents: Dict[str, AgentInfo] = {}
        self.alerts: List[HealthAlert] = []
        self.alert_handlers: List[Callable[[HealthAlert], None]] = []
        
        # Tracking last alert time per agent (anti-spam)
        self._last_alert_time: Dict[str, datetime] = {}
        
        # Running state
        self._running = False
        self._monitor_task: Optional[asyncio.Task] = None
        
    def register_agent(self, name: str, status: AgentStatus = AgentStatus.STARTING):
        """Zarejestruj agenta"""
        if name not in self.agents:
            self.agents[name] = AgentInfo(
                name=name,
                status=status,
                started_at=datetime.now()
            )
        else:
            self.agents[name].status = status
            self.agents[name].started_at = datetime.now()
            
    def unregister_agent(self, name: str):
        """Wyrejestruj agenta"""
        if name in self.agents:
            self.agents[name].status = AgentStatus.STOPPED
            
    def record_heartbeat(self, agent_name: str, heartbeat: AgentHeartbeatPayload):
        """Zapisz heartbeat"""
        if agent_name not in self.agents:
            self.register_agent(agent_name)
            
        agent = self.agents[agent_name]
        agent.last_heartbeat = datetime.now()
        agent.current_task = heartbeat.current_task
        agent.memory_mb = heartbeat.memory_mb
        
        # Update status
        status_map = {
            "running": AgentStatus.RUNNING,
            "idle": AgentStatus.IDLE,
            "busy": AgentStatus.BUSY,
            "degraded": AgentStatus.DEGRADED,
            "starting": AgentStatus.STARTING,
        }
        agent.status = status_map.get(heartbeat.status, AgentStatus.RUNNING)
        
        # Resolve any dead alerts
        self._resolve_alerts(agent_name, "dead")
        
        # Also record in metrics
        get_metrics().record_heartbeat(agent_name, {
            "status": heartbeat.status,
            "current_task": heartbeat.current_task,
            "memory_mb": heartbeat.memory_mb
        })
        
    def record_message_sent(self, agent_name: str):
        """Zapisz wysłanie wiadomości"""
        if agent_name in self.agents:
            self.agents[agent_name].last_message_sent = datetime.now()
            
    def record_message_received(self, agent_name: str):
        """Zapisz odebranie wiadomości"""
        if agent_name in self.agents:
            self.agents[agent_name].last_message_received = datetime.now()
            
    def record_error(self, agent_name: str):
        """Zapisz błąd"""
        if agent_name in self.agents:
            self.agents[agent_name].error_count += 1
            
    def check_health(self) -> Dict[str, bool]:
        """Sprawdź zdrowie wszystkich agentów"""
        results = {}
        timeout = self.config.heartbeat_timeout_seconds
        
        for name, agent in self.agents.items():
            if agent.status == AgentStatus.STOPPED:
                results[name] = False
                continue
                
            is_alive = agent.is_alive(timeout)
            results[name] = is_alive
            
            if not is_alive and agent.status != AgentStatus.DEAD:
                agent.status = AgentStatus.DEAD
                self._raise_alert(name, "dead", f"Agent {name} is dead (no heartbeat for {timeout}s)")
                
        return results
        
    def get_dead_agents(self) -> List[str]:
        """Pobierz listę martwych agentów"""
        return [
            name for name, agent in self.agents.items()
            if not agent.is_alive(self.config.heartbeat_timeout_seconds)
            and agent.status != AgentStatus.STOPPED
        ]
        
    def get_status_summary(self) -> Dict[str, Any]:
        """Pobierz podsumowanie statusu"""
        total = len(self.agents)
        alive = sum(1 for a in self.agents.values() if a.is_alive(self.config.heartbeat_timeout_seconds))
        dead = sum(1 for a in self.agents.values() if a.status == AgentStatus.DEAD)
        stopped = sum(1 for a in self.agents.values() if a.status == AgentStatus.STOPPED)
        
        return {
            "total_agents": total,
            "alive": alive,
            "dead": dead,
            "stopped": stopped,
            "health_percentage": (alive / total * 100) if total > 0 else 0,
            "agents": {
                name: {
                    "status": agent.status.value,
                    "uptime_seconds": agent.uptime_seconds,
                    "seconds_since_heartbeat": agent.seconds_since_heartbeat 
                        if agent.seconds_since_heartbeat != float('inf') else None,
                    "memory_mb": agent.memory_mb,
                    "error_count": agent.error_count,
                    "current_task": agent.current_task
                }
                for name, agent in self.agents.items()
            },
            "active_alerts": [
                {
                    "agent": a.agent_name,
                    "type": a.alert_type,
                    "message": a.message,
                    "timestamp": a.timestamp.isoformat()
                }
                for a in self.alerts if not a.resolved
            ]
        }
        
    def on_alert(self, handler: Callable[[HealthAlert], None]):
        """Dodaj handler alertów"""
        self.alert_handlers.append(handler)
        
    def _raise_alert(self, agent_name: str, alert_type: str, message: str):
        """Podnieś alert"""
        if not self.config.alert_on_dead_agent:
            return
            
        # Anti-spam: nie wysyłaj alertu jeśli niedawno był
        key = f"{agent_name}:{alert_type}"
        if key in self._last_alert_time:
            elapsed = (datetime.now() - self._last_alert_time[key]).total_seconds()
            if elapsed < self.config.dead_agent_alert_interval:
                return
                
        alert = HealthAlert(
            agent_name=agent_name,
            alert_type=alert_type,
            message=message
        )
        self.alerts.append(alert)
        self._last_alert_time[key] = datetime.now()
        
        # Wywołaj handlery
        for handler in self.alert_handlers:
            try:
                handler(alert)
            except Exception as e:
                print(f"❌ Alert handler error: {e}")
                
        print(f"🚨 HEALTH ALERT: {message}")
        
    def _resolve_alerts(self, agent_name: str, alert_type: str):
        """Rozwiąż alerty"""
        for alert in self.alerts:
            if alert.agent_name == agent_name and alert.alert_type == alert_type:
                if not alert.resolved:
                    alert.resolved = True
                    alert.resolved_at = datetime.now()
                    
    async def start_monitoring(self, interval: float = None):
        """Uruchom monitoring w tle"""
        if self._running:
            return
            
        self._running = True
        interval = interval or self.config.heartbeat_interval_seconds
        
        async def monitor_loop():
            while self._running:
                try:
                    self.check_health()
                    await asyncio.sleep(interval)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    print(f"❌ Health monitor error: {e}")
                    await asyncio.sleep(1)
                    
        self._monitor_task = asyncio.create_task(monitor_loop())
        print("🏥 Health monitor started")
        
    async def stop_monitoring(self):
        """Zatrzymaj monitoring"""
        self._running = False
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        print("🏥 Health monitor stopped")


# Singleton
_monitor: Optional[HealthMonitor] = None

def get_health_monitor() -> HealthMonitor:
    """Pobierz singleton monitora"""
    global _monitor
    if _monitor is None:
        _monitor = HealthMonitor()
    return _monitor
