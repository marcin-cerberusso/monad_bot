#!/usr/bin/env python3
"""
🔗 AGENT ISOLATION - Izolowane środowiska dla agentów

Zapobiega:
- Cross-contamination kontekstu
- Halucynacjom między agentami
- Memory leaks
- Context overflow

Każdy agent:
- Własny izolowany sandbox
- Własna pamięć (nie dzielona)
- Własny rate limiter
- Własny error boundary
"""

import asyncio
import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Callable
from enum import Enum
import hashlib
import pickle


class IsolationLevel(Enum):
    """Poziomy izolacji"""
    NONE = "none"          # Brak izolacji (nie używać!)
    SOFT = "soft"          # Osobny context, wspólna pamięć
    HARD = "hard"          # Całkowita izolacja
    STRICT = "strict"      # Izolacja + audyt komunikacji


@dataclass
class AgentMemory:
    """
    Izolowana pamięć agenta
    
    Każdy agent ma własną pamięć:
    - Short-term (conversation context)
    - Long-term (persistent storage)
    - Working (current task)
    """
    agent_id: str
    short_term: List[Dict] = field(default_factory=list)
    long_term: Dict[str, Any] = field(default_factory=dict)
    working: Dict[str, Any] = field(default_factory=dict)
    
    # Limits
    max_short_term: int = 50
    max_long_term_keys: int = 1000
    max_working_size_kb: int = 100
    
    def add_to_short_term(self, message: Dict):
        """Dodaj do short-term memory"""
        self.short_term.append({
            **message,
            "timestamp": datetime.now().isoformat()
        })
        
        # Trim if needed
        if len(self.short_term) > self.max_short_term:
            self.short_term = self.short_term[-self.max_short_term:]
            
    def store_long_term(self, key: str, value: Any):
        """Zapisz do long-term memory"""
        if len(self.long_term) >= self.max_long_term_keys:
            # Remove oldest
            oldest_key = min(self.long_term.keys(), 
                           key=lambda k: self.long_term[k].get("stored_at", ""))
            del self.long_term[oldest_key]
            
        self.long_term[key] = {
            "value": value,
            "stored_at": datetime.now().isoformat()
        }
        
    def get_long_term(self, key: str) -> Optional[Any]:
        """Pobierz z long-term memory"""
        entry = self.long_term.get(key)
        return entry.get("value") if entry else None
        
    def set_working(self, key: str, value: Any):
        """Ustaw working memory"""
        self.working[key] = value
        
        # Check size
        size = len(pickle.dumps(self.working))
        if size > self.max_working_size_kb * 1024:
            raise MemoryError(f"Working memory exceeds {self.max_working_size_kb}KB limit")
            
    def clear_working(self):
        """Wyczyść working memory"""
        self.working = {}
        
    def get_context_for_ai(self) -> List[Dict]:
        """Przygotuj context do wysłania do AI"""
        # Return last N messages from short-term
        return [
            {"role": m.get("role", "user"), "content": m.get("content", "")}
            for m in self.short_term[-20:]
        ]
        
    def summarize(self) -> str:
        """Podsumowanie pamięci"""
        return f"""
Agent: {self.agent_id}
Short-term: {len(self.short_term)} messages
Long-term: {len(self.long_term)} entries
Working: {len(self.working)} items
"""


@dataclass
class CommunicationLog:
    """Log komunikacji między agentami (dla STRICT isolation)"""
    from_agent: str
    to_agent: str
    message_type: str
    payload_hash: str  # Hash payload (nie sam payload!)
    timestamp: str
    approved: bool = True
    rejection_reason: str = ""


class AgentSandbox:
    """
    Sandbox dla izolowanego agenta
    
    Zapewnia:
    - Izolowaną pamięć
    - Rate limiting
    - Error boundary
    - Communication audit
    """
    
    def __init__(self, 
                 agent_id: str, 
                 isolation_level: IsolationLevel = IsolationLevel.HARD):
        self.agent_id = agent_id
        self.isolation_level = isolation_level
        self.memory = AgentMemory(agent_id=agent_id)
        
        # Rate limiting
        self.api_calls_per_minute = 20
        self.api_call_times: List[datetime] = []
        
        # Error boundary
        self.error_count = 0
        self.max_errors = 10
        self.error_cooldown_seconds = 60
        self.last_error_time: Optional[datetime] = None
        
        # Communication log (STRICT mode)
        self.comm_log: List[CommunicationLog] = []
        self.blocked_agents: List[str] = []
        
    async def execute(self, func: Callable, *args, **kwargs) -> Any:
        """Execute function within sandbox"""
        
        # Check error boundary
        if self.error_count >= self.max_errors:
            if self.last_error_time:
                elapsed = (datetime.now() - self.last_error_time).total_seconds()
                if elapsed < self.error_cooldown_seconds:
                    raise RuntimeError(f"Agent {self.agent_id} in cooldown ({self.error_cooldown_seconds - elapsed:.0f}s remaining)")
                else:
                    # Reset after cooldown
                    self.error_count = 0
                    
        try:
            result = await func(*args, **kwargs)
            return result
        except Exception as e:
            self.error_count += 1
            self.last_error_time = datetime.now()
            raise
            
    async def check_rate_limit(self) -> bool:
        """Check if API call is allowed"""
        now = datetime.now()
        
        # Remove old calls
        self.api_call_times = [
            t for t in self.api_call_times 
            if (now - t).total_seconds() < 60
        ]
        
        if len(self.api_call_times) >= self.api_calls_per_minute:
            return False
            
        self.api_call_times.append(now)
        return True
        
    def can_communicate_with(self, other_agent: str) -> bool:
        """Check if communication is allowed"""
        if self.isolation_level == IsolationLevel.NONE:
            return True
            
        if other_agent in self.blocked_agents:
            return False
            
        return True
        
    def log_communication(self, 
                         to_agent: str, 
                         message_type: str, 
                         payload: Any,
                         approved: bool = True,
                         rejection_reason: str = ""):
        """Log communication (STRICT mode)"""
        if self.isolation_level != IsolationLevel.STRICT:
            return
            
        # Hash payload (don't store actual content)
        payload_hash = hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode()
        ).hexdigest()[:16]
        
        self.comm_log.append(CommunicationLog(
            from_agent=self.agent_id,
            to_agent=to_agent,
            message_type=message_type,
            payload_hash=payload_hash,
            timestamp=datetime.now().isoformat(),
            approved=approved,
            rejection_reason=rejection_reason
        ))
        
        # Keep only last 1000 entries
        if len(self.comm_log) > 1000:
            self.comm_log = self.comm_log[-1000:]
            
    def get_health_status(self) -> Dict:
        """Get sandbox health status"""
        return {
            "agent_id": self.agent_id,
            "isolation_level": self.isolation_level.value,
            "memory": {
                "short_term": len(self.memory.short_term),
                "long_term": len(self.memory.long_term),
                "working": len(self.memory.working)
            },
            "rate_limit": {
                "calls_in_last_minute": len(self.api_call_times),
                "limit": self.api_calls_per_minute
            },
            "errors": {
                "count": self.error_count,
                "max": self.max_errors,
                "in_cooldown": self.error_count >= self.max_errors
            }
        }


class IsolationManager:
    """
    Manager izolacji dla wszystkich agentów
    
    Zarządza:
    - Tworzeniem sandboxów
    - Routingiem komunikacji
    - Audytem
    """
    
    def __init__(self):
        self.sandboxes: Dict[str, AgentSandbox] = {}
        self.default_isolation = IsolationLevel.HARD
        
        # Communication rules
        self.allowed_communications: Dict[str, List[str]] = {
            # agent -> [allowed_recipients]
            "scanner": ["analyst", "orchestrator"],
            "analyst": ["trader", "risk", "orchestrator"],
            "trader": ["risk", "orchestrator"],
            "risk": ["trader", "orchestrator"],
            "orchestrator": ["scanner", "analyst", "trader", "risk"]
        }
        
    def create_sandbox(self, 
                      agent_id: str, 
                      isolation_level: Optional[IsolationLevel] = None) -> AgentSandbox:
        """Create isolated sandbox for agent"""
        level = isolation_level or self.default_isolation
        sandbox = AgentSandbox(agent_id, level)
        self.sandboxes[agent_id] = sandbox
        print(f"🔒 Created {level.value} sandbox for {agent_id}")
        return sandbox
        
    def get_sandbox(self, agent_id: str) -> Optional[AgentSandbox]:
        """Get sandbox for agent"""
        return self.sandboxes.get(agent_id)
        
    def validate_communication(self, 
                              from_agent: str, 
                              to_agent: str, 
                              message_type: str) -> tuple[bool, str]:
        """Validate if communication is allowed"""
        
        # Get sandboxes
        from_sandbox = self.sandboxes.get(from_agent)
        to_sandbox = self.sandboxes.get(to_agent)
        
        if not from_sandbox:
            return False, f"Unknown sender: {from_agent}"
            
        if not to_sandbox and to_agent != "all":
            return False, f"Unknown recipient: {to_agent}"
            
        # Check if communication is allowed
        if from_agent in self.allowed_communications:
            allowed = self.allowed_communications[from_agent]
            if to_agent not in allowed and to_agent != "all":
                return False, f"{from_agent} cannot communicate with {to_agent}"
                
        # Check sandbox rules
        if not from_sandbox.can_communicate_with(to_agent):
            return False, f"{from_agent} blocked from communicating with {to_agent}"
            
        return True, ""
        
    def get_all_health_status(self) -> Dict[str, Dict]:
        """Get health status for all sandboxes"""
        return {
            agent_id: sandbox.get_health_status()
            for agent_id, sandbox in self.sandboxes.items()
        }
        
    def reset_all(self):
        """Reset all sandboxes (emergency)"""
        for sandbox in self.sandboxes.values():
            sandbox.memory.short_term = []
            sandbox.memory.working = {}
            sandbox.error_count = 0
        print("🔄 All sandboxes reset")


class ContextGuard:
    """
    Guardian zapobiegający cross-contamination kontekstu
    
    Sprawdza:
    - Czy context nie zawiera danych z innego agenta
    - Czy nie ma halucynacji
    - Czy nie ma memory leaks
    """
    
    def __init__(self):
        self.agent_signatures: Dict[str, str] = {}
        
    def sign_context(self, agent_id: str, context: List[Dict]) -> str:
        """Create signature for agent context"""
        content = json.dumps(context, sort_keys=True)
        signature = hashlib.sha256(f"{agent_id}:{content}".encode()).hexdigest()
        self.agent_signatures[agent_id] = signature
        return signature
        
    def verify_context(self, agent_id: str, context: List[Dict]) -> bool:
        """Verify context belongs to agent"""
        expected_sig = self.agent_signatures.get(agent_id)
        if not expected_sig:
            return True  # First context
            
        # Check for foreign content
        for msg in context:
            content = msg.get("content", "")
            
            # Check for other agent signatures
            for other_agent, other_sig in self.agent_signatures.items():
                if other_agent != agent_id and other_sig[:8] in content:
                    print(f"⚠️ Context contamination detected: {agent_id} contains {other_agent} data")
                    return False
                    
        return True
        
    def detect_hallucination(self, response: str, facts: List[str]) -> bool:
        """
        Detect potential hallucination
        
        Basic check - response should relate to provided facts
        """
        if not facts:
            return False
            
        # Simple keyword overlap check
        response_words = set(response.lower().split())
        fact_words = set()
        for fact in facts:
            fact_words.update(fact.lower().split())
            
        overlap = len(response_words.intersection(fact_words))
        
        # If very low overlap, might be hallucinating
        if overlap < len(fact_words) * 0.1:
            print(f"⚠️ Potential hallucination detected (low fact overlap: {overlap})")
            return True
            
        return False


# Singleton instance
_isolation_manager: Optional[IsolationManager] = None


def get_isolation_manager() -> IsolationManager:
    """Get singleton isolation manager"""
    global _isolation_manager
    if _isolation_manager is None:
        _isolation_manager = IsolationManager()
    return _isolation_manager


def test_isolation():
    """Test isolation functionality"""
    manager = get_isolation_manager()
    
    # Create sandboxes
    scanner_sandbox = manager.create_sandbox("scanner", IsolationLevel.HARD)
    analyst_sandbox = manager.create_sandbox("analyst", IsolationLevel.HARD)
    trader_sandbox = manager.create_sandbox("trader", IsolationLevel.STRICT)
    risk_sandbox = manager.create_sandbox("risk", IsolationLevel.STRICT)
    
    # Test memory isolation
    scanner_sandbox.memory.add_to_short_term({
        "role": "user",
        "content": "Found whale buying token X"
    })
    
    analyst_sandbox.memory.add_to_short_term({
        "role": "user",
        "content": "Analyze token X"
    })
    
    # Verify isolation
    print("\n📊 Memory Status:")
    print(f"Scanner: {len(scanner_sandbox.memory.short_term)} messages")
    print(f"Analyst: {len(analyst_sandbox.memory.short_term)} messages")
    
    assert len(scanner_sandbox.memory.short_term) == 1
    assert len(analyst_sandbox.memory.short_term) == 1
    assert scanner_sandbox.memory.short_term[0]["content"] != analyst_sandbox.memory.short_term[0]["content"]
    
    # Test communication validation
    print("\n🔗 Communication Validation:")
    
    allowed, reason = manager.validate_communication("scanner", "analyst", "whale_alert")
    print(f"scanner -> analyst: {'✅' if allowed else '❌'} {reason}")
    
    allowed, reason = manager.validate_communication("scanner", "trader", "whale_alert")
    print(f"scanner -> trader: {'✅' if allowed else '❌'} {reason}")
    
    # Test health status
    print("\n🏥 Health Status:")
    status = manager.get_all_health_status()
    for agent_id, health in status.items():
        print(f"  {agent_id}: {health['isolation_level']}, errors={health['errors']['count']}")
        
    print("\n✅ Isolation test passed!")


if __name__ == "__main__":
    test_isolation()
