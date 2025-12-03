#!/usr/bin/env python3
"""
⚙️ BUS CONFIG - Konfiguracja MessageBus

Zawiera:
- Ustawienia połączenia (Dragonfly/Redis)
- ACL (allowed senders per agent)
- Limity (rate limiting, backpressure)
- Konfiguracja consensus
"""

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set
from enum import Enum
from dotenv import load_dotenv

load_dotenv()


class BusMode(Enum):
    """Tryb pracy busa"""
    NETWORK = "network"      # Wymaga Dragonfly/Redis
    LOCAL = "local"          # In-memory only
    HYBRID = "hybrid"        # Próbuj sieć, fallback do local


@dataclass
class ConnectionConfig:
    """Konfiguracja połączenia"""
    dragonfly_url: str = ""
    socket_timeout: float = 5.0
    socket_connect_timeout: float = 5.0
    retry_attempts: int = 3
    retry_delay_seconds: float = 1.0
    retry_backoff_multiplier: float = 2.0  # exponential backoff
    
    # Pub/sub
    pubsub_timeout: float = 0.5  # większy niż 0.1
    pubsub_reconnect_delay: float = 1.0
    
    def __post_init__(self):
        if not self.dragonfly_url:
            self.dragonfly_url = os.getenv("DRAGONFLY_URL", "")


@dataclass  
class RateLimitConfig:
    """Konfiguracja rate limiting"""
    enabled: bool = True
    max_messages_per_second: int = 100
    max_messages_per_minute: int = 3000
    burst_limit: int = 50  # ile wiadomości można wysłać naraz
    
    # Per priority
    high_priority_multiplier: float = 2.0    # 2x limit dla HIGH
    critical_no_limit: bool = True           # Brak limitu dla CRITICAL
    

@dataclass
class BackpressureConfig:
    """Konfiguracja backpressure"""
    enabled: bool = True
    max_queue_size: int = 1000
    
    # Priorytety
    use_priority_queues: bool = True
    priority_channels: Dict[str, str] = field(default_factory=lambda: {
        "critical": "monad_bot:priority:critical",
        "high": "monad_bot:priority:high", 
        "normal": "monad_bot:priority:normal",
        "low": "monad_bot:priority:low",
    })
    
    # Drop policy gdy pełne
    drop_low_priority_first: bool = True
    drop_old_messages_first: bool = True


@dataclass
class ConsensusConfig:
    """Konfiguracja consensus"""
    # Timeouts
    default_timeout_seconds: float = 5.0
    min_timeout_seconds: float = 1.0
    max_timeout_seconds: float = 30.0
    
    # Approvals
    default_min_approvals: int = 1
    
    # Quorum
    require_quorum: bool = True
    quorum_percentage: float = 50.0  # % agentów musi zagłosować
    
    # Behavior
    timeout_means_reject: bool = True
    abstain_counts_as_vote: bool = False
    
    # Persistence
    persist_to_redis: bool = True
    votes_ttl_seconds: int = 120
    
    # Lista głosujących agentów per akcja
    voters_for_action: Dict[str, List[str]] = field(default_factory=lambda: {
        "buy": ["risk", "analyst"],
        "sell": ["risk"],
        "emergency_sell": [],  # natychmiastowe, bez głosowania
    })
    
    # Wagi głosów per agent (domyślnie 1.0)
    voter_weights: Dict[str, float] = field(default_factory=lambda: {
        "risk": 2.0,      # Risk ma podwójny głos
        "analyst": 1.5,   # Analyst ma 1.5x
        "scanner": 1.0,
        "trader": 1.0,
        "orchestrator": 3.0,  # Orchestrator ma największą wagę
    })
    
    # Veto - agenci którzy mogą zawetować (ich reject = natychmiastowe odrzucenie)
    veto_agents: Set[str] = field(default_factory=lambda: {
        "risk",  # Risk może zawetować każdy trade
    })


@dataclass
class ACLConfig:
    """Access Control List - kto może komunikować się z kim"""
    enabled: bool = True
    
    # SECURITY: In production, set to False and use explicit allowed_senders
    # Default: False for security (deny by default)
    default_allow_all: bool = True
    
    # Per agent: lista dozwolonych nadawców
    # Jeśli agent ma wpis, tylko ci nadawcy są dozwoleni
    allowed_senders: Dict[str, Set[str]] = field(default_factory=lambda: {
        # Przykład: trader może odbierać tylko od analyst, risk, scanner
        # "trader": {"analyst", "risk", "scanner"},
    })
    
    # Per agent: lista blokowanych nadawców
    blocked_senders: Dict[str, Set[str]] = field(default_factory=dict)
    
    # Logi odrzuconych wiadomości
    log_blocked: bool = True
    
    # HMAC signing (opcjonalne)
    use_hmac: bool = False
    hmac_secret: str = ""
    
    def is_allowed(self, sender: str, recipient: str) -> bool:
        """Sprawdź czy sender może wysłać do recipient"""
        if not self.enabled:
            return True
            
        # Sprawdź blocked
        if recipient in self.blocked_senders:
            if sender in self.blocked_senders[recipient]:
                return False
                
        # Sprawdź allowed
        if recipient in self.allowed_senders:
            return sender in self.allowed_senders[recipient]
            
        # Default
        return self.default_allow_all


@dataclass
class HealthConfig:
    """Konfiguracja health monitoring"""
    enabled: bool = True
    
    # Heartbeats
    heartbeat_interval_seconds: float = 10.0
    heartbeat_timeout_seconds: float = 30.0
    
    # Alerting
    alert_on_dead_agent: bool = True
    dead_agent_alert_interval: float = 60.0  # nie spamuj alertami
    
    # Auto-recovery
    auto_restart_dead_agents: bool = False


@dataclass
class ValidationConfig:
    """Konfiguracja walidacji"""
    enabled: bool = True
    strict: bool = True  # odrzuć niepoprawne vs tylko loguj
    
    # Limity
    max_message_size_bytes: int = 64 * 1024
    max_string_length: int = 1024
    max_reason_length: int = 2048


@dataclass
class BusConfig:
    """Główna konfiguracja MessageBus"""
    
    # Tryb pracy
    mode: BusMode = BusMode.HYBRID
    
    # Sub-konfiguracje
    connection: ConnectionConfig = field(default_factory=ConnectionConfig)
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)
    backpressure: BackpressureConfig = field(default_factory=BackpressureConfig)
    consensus: ConsensusConfig = field(default_factory=ConsensusConfig)
    acl: ACLConfig = field(default_factory=ACLConfig)
    health: HealthConfig = field(default_factory=HealthConfig)
    validation: ValidationConfig = field(default_factory=ValidationConfig)
    
    # Channel prefix
    channel_prefix: str = "monad_bot:"
    
    # Known agents (dla dynamicznych kanałów)
    known_agents: List[str] = field(default_factory=lambda: [
        "scanner", "analyst", "trader", "risk", "orchestrator"
    ])
    
    def get_channel(self, name: str) -> str:
        """Pobierz pełną nazwę kanału"""
        return f"{self.channel_prefix}{name}"
        
    def validate(self) -> List[str]:
        """Waliduj konfigurację, zwróć listę błędów"""
        errors = []
        
        # Tryb NETWORK wymaga URL
        if self.mode == BusMode.NETWORK and not self.connection.dragonfly_url:
            errors.append("NETWORK mode requires DRAGONFLY_URL")
            
        # Sprawdź timeouty
        if self.consensus.default_timeout_seconds < self.consensus.min_timeout_seconds:
            errors.append("default_timeout < min_timeout")
            
        # Sprawdź rate limits
        if self.rate_limit.enabled:
            if self.rate_limit.max_messages_per_second <= 0:
                errors.append("max_messages_per_second must be > 0")
                
        return errors
        
    @classmethod
    def from_env(cls) -> "BusConfig":
        """Utwórz config z env variables"""
        config = cls()
        
        # Connection
        config.connection.dragonfly_url = os.getenv("DRAGONFLY_URL", "")
        
        # Mode
        mode_str = os.getenv("BUS_MODE", "hybrid").lower()
        if mode_str == "network":
            config.mode = BusMode.NETWORK
        elif mode_str == "local":
            config.mode = BusMode.LOCAL
        else:
            config.mode = BusMode.HYBRID
            
        # Rate limiting
        config.rate_limit.enabled = os.getenv("BUS_RATE_LIMIT", "true").lower() == "true"
        
        # Validation
        config.validation.strict = os.getenv("BUS_STRICT_VALIDATION", "true").lower() == "true"
        
        # ACL
        config.acl.enabled = os.getenv("BUS_ACL_ENABLED", "true").lower() == "true"
        
        return config


# Singleton
_config: Optional[BusConfig] = None

def get_config() -> BusConfig:
    """Pobierz singleton konfiguracji"""
    global _config
    if _config is None:
        _config = BusConfig.from_env()
    return _config

def set_config(config: BusConfig):
    """Ustaw konfigurację (np. dla testów)"""
    global _config
    _config = config

def reset_config():
    """Resetuj konfigurację do defaults (dla testów)"""
    global _config
    _config = BusConfig()
