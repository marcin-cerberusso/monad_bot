"""
🧠 SHORT-TERM MEMORY - Agent's Working Memory
Fast, in-memory storage for immediate context
"""
import json
import time
from collections import deque
from dataclasses import dataclass, asdict
from typing import Optional, List, Dict, Any
from datetime import datetime
import threading


@dataclass
class MemoryItem:
    """Single memory item"""
    timestamp: float
    type: str  # 'trade', 'signal', 'decision', 'observation'
    content: Dict[str, Any]
    importance: float = 0.5  # 0-1, higher = more important
    ttl: int = 3600  # Time to live in seconds


class ShortTermMemory:
    """
    Fast working memory for agents
    - Recent trades and decisions
    - Active signals being processed
    - Current market context
    - Last N interactions
    """
    
    def __init__(self, max_items: int = 1000, default_ttl: int = 3600):
        self.max_items = max_items
        self.default_ttl = default_ttl
        self._memory: deque = deque(maxlen=max_items)
        self._index: Dict[str, List[MemoryItem]] = {}  # Type-based index
        self._lock = threading.Lock()
        
        # Special registers for quick access
        self._active_positions: Dict[str, Dict] = {}
        self._pending_signals: Dict[str, Dict] = {}
        self._last_decisions: deque = deque(maxlen=50)
        self._whale_activity: deque = deque(maxlen=100)
        
    def remember(self, 
                 type: str, 
                 content: Dict[str, Any], 
                 importance: float = 0.5,
                 ttl: Optional[int] = None) -> str:
        """Store a new memory"""
        item = MemoryItem(
            timestamp=time.time(),
            type=type,
            content=content,
            importance=importance,
            ttl=ttl or self.default_ttl
        )
        
        with self._lock:
            self._memory.append(item)
            
            # Index by type
            if type not in self._index:
                self._index[type] = []
            self._index[type].append(item)
            
            # Special handling for different types
            if type == 'position':
                token = content.get('token')
                if token:
                    self._active_positions[token] = content
                    
            elif type == 'signal':
                signal_id = content.get('id', str(time.time()))
                self._pending_signals[signal_id] = content
                
            elif type == 'decision':
                self._last_decisions.append(item)
                
            elif type == 'whale':
                self._whale_activity.append(item)
        
        return f"{type}_{item.timestamp}"
    
    def recall(self, 
               type: Optional[str] = None, 
               limit: int = 10,
               min_importance: float = 0.0,
               since: Optional[float] = None) -> List[Dict]:
        """Recall memories with optional filters"""
        now = time.time()
        results = []
        
        with self._lock:
            # Get items from specific type or all
            if type and type in self._index:
                items = self._index[type]
            else:
                items = list(self._memory)
            
            for item in reversed(items):
                # Check TTL
                if now - item.timestamp > item.ttl:
                    continue
                    
                # Check importance
                if item.importance < min_importance:
                    continue
                    
                # Check time filter
                if since and item.timestamp < since:
                    continue
                    
                results.append({
                    'timestamp': item.timestamp,
                    'type': item.type,
                    'content': item.content,
                    'importance': item.importance,
                    'age_seconds': now - item.timestamp
                })
                
                if len(results) >= limit:
                    break
                    
        return results
    
    def get_active_positions(self) -> Dict[str, Dict]:
        """Get all active trading positions"""
        return self._active_positions.copy()
    
    def get_pending_signals(self) -> Dict[str, Dict]:
        """Get signals waiting to be processed"""
        return self._pending_signals.copy()
    
    def get_recent_decisions(self, limit: int = 10) -> List[Dict]:
        """Get most recent trading decisions"""
        with self._lock:
            decisions = list(self._last_decisions)[-limit:]
            return [asdict(d) for d in decisions]
    
    def get_whale_activity(self, limit: int = 20) -> List[Dict]:
        """Get recent whale transactions"""
        with self._lock:
            activity = list(self._whale_activity)[-limit:]
            return [asdict(a) for a in activity]
    
    def update_position(self, token: str, updates: Dict):
        """Update an active position"""
        with self._lock:
            if token in self._active_positions:
                self._active_positions[token].update(updates)
    
    def close_position(self, token: str) -> Optional[Dict]:
        """Remove a position when closed"""
        with self._lock:
            return self._active_positions.pop(token, None)
    
    def resolve_signal(self, signal_id: str, result: str):
        """Mark a signal as processed"""
        with self._lock:
            if signal_id in self._pending_signals:
                signal = self._pending_signals.pop(signal_id)
                signal['resolved'] = True
                signal['result'] = result
                signal['resolved_at'] = time.time()
                
                # Remember the resolution
                self.remember('signal_resolved', signal, importance=0.7)
    
    def get_context_summary(self) -> Dict:
        """Get a summary of current context for agent reasoning"""
        now = time.time()
        
        return {
            'active_positions': len(self._active_positions),
            'pending_signals': len(self._pending_signals),
            'recent_decisions_1h': len([
                d for d in self._last_decisions 
                if now - d.timestamp < 3600
            ]),
            'whale_activity_1h': len([
                w for w in self._whale_activity 
                if now - w.timestamp < 3600
            ]),
            'memory_usage': len(self._memory),
            'positions': list(self._active_positions.keys()),
            'last_trade_age': self._get_last_trade_age(),
        }
    
    def _get_last_trade_age(self) -> Optional[float]:
        """Time since last trade in seconds"""
        trades = self.recall(type='trade', limit=1)
        if trades:
            return time.time() - trades[0]['timestamp']
        return None
    
    def cleanup(self):
        """Remove expired memories"""
        now = time.time()
        
        with self._lock:
            # Clean main memory
            valid = [m for m in self._memory if now - m.timestamp <= m.ttl]
            self._memory = deque(valid, maxlen=self.max_items)
            
            # Clean indices
            for type_name in list(self._index.keys()):
                self._index[type_name] = [
                    m for m in self._index[type_name] 
                    if now - m.timestamp <= m.ttl
                ]
    
    def save_to_file(self, path: str):
        """Persist memory to file"""
        with self._lock:
            data = {
                'memory': [asdict(m) for m in self._memory],
                'positions': self._active_positions,
                'signals': self._pending_signals,
                'saved_at': time.time()
            }
            with open(path, 'w') as f:
                json.dump(data, f, indent=2)
    
    def load_from_file(self, path: str):
        """Restore memory from file"""
        try:
            with open(path) as f:
                data = json.load(f)
                
            with self._lock:
                for item in data.get('memory', []):
                    self._memory.append(MemoryItem(**item))
                self._active_positions = data.get('positions', {})
                self._pending_signals = data.get('signals', {})
                
        except FileNotFoundError:
            pass  # Fresh start
