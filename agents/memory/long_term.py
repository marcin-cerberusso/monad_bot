"""
🧠 LONG-TERM MEMORY - Agent's Persistent Knowledge Base
SQLite + Vector embeddings for learning from history
"""
import sqlite3
import json
import time
import hashlib
from typing import Optional, List, Dict, Any, Tuple
from dataclasses import dataclass
from datetime import datetime
import numpy as np
from pathlib import Path


@dataclass
class TradeRecord:
    """Complete trade record for learning"""
    id: str
    token: str
    token_name: Optional[str]
    entry_time: float
    exit_time: Optional[float]
    entry_price: float
    exit_price: Optional[float]
    amount_mon: float
    pnl_percent: Optional[float]
    pnl_mon: Optional[float]
    trigger_type: str  # 'whale_copy', 'snipe', 'ai_signal'
    whale_address: Optional[str]
    ai_score: Optional[float]
    market_context: Dict  # mcap, volume, holders at entry
    exit_reason: Optional[str]  # 'tp1', 'tp2', 'sl', 'trailing', 'whale_exit'
    notes: Optional[str]


class LongTermMemory:
    """
    Persistent memory with SQLite + vector search
    - Trade history with full context
    - Token performance patterns
    - Whale behavior profiles
    - Market condition snapshots
    """
    
    def __init__(self, db_path: str = "data/agent_memory.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        
        # In-memory cache for frequently accessed data
        self._whale_profiles: Dict[str, Dict] = {}
        self._token_history: Dict[str, List] = {}
        
    def _init_db(self):
        """Initialize database schema"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        # Trades table - full history
        c.execute('''CREATE TABLE IF NOT EXISTS trades (
            id TEXT PRIMARY KEY,
            token TEXT NOT NULL,
            token_name TEXT,
            entry_time REAL NOT NULL,
            exit_time REAL,
            entry_price REAL NOT NULL,
            exit_price REAL,
            amount_mon REAL NOT NULL,
            pnl_percent REAL,
            pnl_mon REAL,
            trigger_type TEXT NOT NULL,
            whale_address TEXT,
            ai_score REAL,
            market_context TEXT,
            exit_reason TEXT,
            notes TEXT,
            embedding BLOB,
            created_at REAL DEFAULT (strftime('%s', 'now'))
        )''')
        
        # Whale profiles - behavior patterns
        c.execute('''CREATE TABLE IF NOT EXISTS whale_profiles (
            address TEXT PRIMARY KEY,
            first_seen REAL,
            last_seen REAL,
            total_trades INTEGER DEFAULT 0,
            winning_trades INTEGER DEFAULT 0,
            avg_hold_time REAL,
            avg_pnl_percent REAL,
            preferred_tokens TEXT,
            trading_style TEXT,
            trust_score REAL DEFAULT 0.5,
            notes TEXT,
            updated_at REAL
        )''')
        
        # Token patterns - what we learned about tokens
        c.execute('''CREATE TABLE IF NOT EXISTS token_patterns (
            token TEXT PRIMARY KEY,
            name TEXT,
            first_trade REAL,
            last_trade REAL,
            times_traded INTEGER DEFAULT 0,
            total_pnl_mon REAL DEFAULT 0,
            avg_pnl_percent REAL,
            best_entry_conditions TEXT,
            worst_entry_conditions TEXT,
            typical_hold_time REAL,
            rugged INTEGER DEFAULT 0,
            notes TEXT,
            updated_at REAL
        )''')
        
        # Market snapshots - for pattern recognition
        c.execute('''CREATE TABLE IF NOT EXISTS market_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            btc_price REAL,
            eth_price REAL,
            mon_price REAL,
            gas_gwei REAL,
            active_tokens INTEGER,
            total_volume_24h REAL,
            whale_activity_score REAL,
            market_sentiment TEXT,
            notes TEXT
        )''')
        
        # Lessons learned - explicit knowledge
        c.execute('''CREATE TABLE IF NOT EXISTS lessons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            category TEXT NOT NULL,
            lesson TEXT NOT NULL,
            confidence REAL DEFAULT 0.5,
            times_confirmed INTEGER DEFAULT 1,
            last_confirmed REAL,
            example_trade_id TEXT,
            embedding BLOB
        )''')
        
        # Create indices
        c.execute('CREATE INDEX IF NOT EXISTS idx_trades_token ON trades(token)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_trades_whale ON trades(whale_address)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_trades_time ON trades(entry_time)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_trades_pnl ON trades(pnl_percent)')
        
        conn.commit()
        conn.close()
        
    def record_trade(self, trade: TradeRecord) -> str:
        """Store a completed trade"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        c.execute('''INSERT OR REPLACE INTO trades 
            (id, token, token_name, entry_time, exit_time, entry_price, exit_price,
             amount_mon, pnl_percent, pnl_mon, trigger_type, whale_address, ai_score,
             market_context, exit_reason, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (trade.id, trade.token, trade.token_name, trade.entry_time, trade.exit_time,
             trade.entry_price, trade.exit_price, trade.amount_mon, trade.pnl_percent,
             trade.pnl_mon, trade.trigger_type, trade.whale_address, trade.ai_score,
             json.dumps(trade.market_context), trade.exit_reason, trade.notes))
        
        conn.commit()
        conn.close()
        
        # Update related profiles
        if trade.whale_address:
            self._update_whale_profile(trade)
        self._update_token_pattern(trade)
        
        return trade.id
    
    def _update_whale_profile(self, trade: TradeRecord):
        """Update whale's behavior profile based on trade outcome"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        # Get existing profile
        c.execute('SELECT * FROM whale_profiles WHERE address = ?', 
                  (trade.whale_address,))
        row = c.fetchone()
        
        if row:
            # Update existing
            total = row[3] + 1  # total_trades
            winning = row[4] + (1 if (trade.pnl_percent or 0) > 0 else 0)
            win_rate = winning / total
            
            # Calculate new trust score based on performance
            old_trust = row[10] or 0.5  # Default to 0.5 if None
            performance_factor = min(max((trade.pnl_percent or 0) / 100, -0.1), 0.1)
            new_trust = min(max(old_trust + performance_factor, 0), 1)
            
            c.execute('''UPDATE whale_profiles SET
                last_seen = ?, total_trades = ?, winning_trades = ?,
                trust_score = ?, updated_at = ?
                WHERE address = ?''',
                (time.time(), total, winning, new_trust, time.time(), 
                 trade.whale_address))
        else:
            # Create new profile
            c.execute('''INSERT INTO whale_profiles 
                (address, first_seen, last_seen, total_trades, winning_trades,
                 trust_score, updated_at)
                VALUES (?, ?, ?, 1, ?, 0.5, ?)''',
                (trade.whale_address, trade.entry_time, time.time(),
                 1 if (trade.pnl_percent or 0) > 0 else 0, time.time()))
        
        conn.commit()
        conn.close()
    
    def _update_token_pattern(self, trade: TradeRecord):
        """Update token pattern data"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        c.execute('SELECT * FROM token_patterns WHERE token = ?', (trade.token,))
        row = c.fetchone()
        
        if row:
            times = row[4] + 1
            total_pnl = row[5] + (trade.pnl_mon or 0)
            avg_pnl = total_pnl / times
            
            c.execute('''UPDATE token_patterns SET
                last_trade = ?, times_traded = ?, total_pnl_mon = ?,
                avg_pnl_percent = ?, updated_at = ?
                WHERE token = ?''',
                (time.time(), times, total_pnl, avg_pnl, time.time(), trade.token))
        else:
            c.execute('''INSERT INTO token_patterns
                (token, name, first_trade, last_trade, times_traded, 
                 total_pnl_mon, avg_pnl_percent, updated_at)
                VALUES (?, ?, ?, ?, 1, ?, ?, ?)''',
                (trade.token, trade.token_name, trade.entry_time, time.time(),
                 trade.pnl_mon or 0, trade.pnl_percent or 0, time.time()))
        
        conn.commit()
        conn.close()
    
    def get_whale_profile(self, address: str) -> Optional[Dict]:
        """Get whale's trading profile"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        c.execute('SELECT * FROM whale_profiles WHERE address = ?', (address.lower(),))
        row = c.fetchone()
        conn.close()
        
        if row:
            return {
                'address': row[0],
                'first_seen': row[1],
                'last_seen': row[2],
                'total_trades': row[3],
                'winning_trades': row[4],
                'win_rate': row[4] / row[3] if row[3] > 0 else 0,
                'avg_hold_time': row[5],
                'avg_pnl_percent': row[6],
                'trust_score': row[10],
                'notes': row[11]
            }
        return None
    
    def get_similar_trades(self, 
                           token: Optional[str] = None,
                           whale: Optional[str] = None,
                           trigger_type: Optional[str] = None,
                           min_pnl: Optional[float] = None,
                           limit: int = 10) -> List[Dict]:
        """Find similar historical trades"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        query = 'SELECT * FROM trades WHERE 1=1'
        params = []
        
        if token:
            query += ' AND token = ?'
            params.append(token)
        if whale:
            query += ' AND whale_address = ?'
            params.append(whale.lower())
        if trigger_type:
            query += ' AND trigger_type = ?'
            params.append(trigger_type)
        if min_pnl is not None:
            query += ' AND pnl_percent >= ?'
            params.append(min_pnl)
            
        query += ' ORDER BY entry_time DESC LIMIT ?'
        params.append(limit)
        
        c.execute(query, params)
        rows = c.fetchall()
        conn.close()
        
        return [self._row_to_trade_dict(row) for row in rows]
    
    def _row_to_trade_dict(self, row) -> Dict:
        """Convert DB row to trade dict"""
        return {
            'id': row[0],
            'token': row[1],
            'token_name': row[2],
            'entry_time': row[3],
            'exit_time': row[4],
            'entry_price': row[5],
            'exit_price': row[6],
            'amount_mon': row[7],
            'pnl_percent': row[8],
            'pnl_mon': row[9],
            'trigger_type': row[10],
            'whale_address': row[11],
            'ai_score': row[12],
            'market_context': json.loads(row[13]) if row[13] else {},
            'exit_reason': row[14],
            'notes': row[15]
        }
    
    def learn_lesson(self, 
                     category: str, 
                     lesson: str, 
                     confidence: float = 0.5,
                     example_trade_id: Optional[str] = None):
        """Store an explicit lesson learned"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        # Check if similar lesson exists
        c.execute('''SELECT id, times_confirmed, confidence FROM lessons 
                     WHERE category = ? AND lesson = ?''', (category, lesson))
        row = c.fetchone()
        
        if row:
            # Reinforce existing lesson
            new_conf = min(row[2] + 0.1, 1.0)
            c.execute('''UPDATE lessons SET 
                times_confirmed = ?, confidence = ?, last_confirmed = ?
                WHERE id = ?''', 
                (row[1] + 1, new_conf, time.time(), row[0]))
        else:
            # New lesson
            c.execute('''INSERT INTO lessons 
                (timestamp, category, lesson, confidence, example_trade_id)
                VALUES (?, ?, ?, ?, ?)''',
                (time.time(), category, lesson, confidence, example_trade_id))
        
        conn.commit()
        conn.close()
    
    def get_lessons(self, 
                    category: Optional[str] = None,
                    min_confidence: float = 0.3,
                    limit: int = 20) -> List[Dict]:
        """Retrieve learned lessons"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        query = 'SELECT * FROM lessons WHERE confidence >= ?'
        params = [min_confidence]
        
        if category:
            query += ' AND category = ?'
            params.append(category)
            
        query += ' ORDER BY confidence DESC, times_confirmed DESC LIMIT ?'
        params.append(limit)
        
        c.execute(query, params)
        rows = c.fetchall()
        conn.close()
        
        return [{
            'id': row[0],
            'timestamp': row[1],
            'category': row[2],
            'lesson': row[3],
            'confidence': row[4],
            'times_confirmed': row[5],
            'example_trade_id': row[7]
        } for row in rows]
    
    def get_trading_stats(self, days: int = 30) -> Dict:
        """Get overall trading statistics"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        since = time.time() - (days * 86400)
        
        c.execute('''SELECT 
            COUNT(*) as total_trades,
            SUM(CASE WHEN pnl_percent > 0 THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN pnl_percent < 0 THEN 1 ELSE 0 END) as losses,
            AVG(pnl_percent) as avg_pnl,
            SUM(pnl_mon) as total_pnl_mon,
            MAX(pnl_percent) as best_trade,
            MIN(pnl_percent) as worst_trade
            FROM trades WHERE entry_time > ?''', (since,))
        
        row = c.fetchone()
        conn.close()
        
        if row and row[0]:
            return {
                'total_trades': row[0],
                'wins': row[1] or 0,
                'losses': row[2] or 0,
                'win_rate': (row[1] or 0) / row[0] if row[0] > 0 else 0,
                'avg_pnl_percent': row[3] or 0,
                'total_pnl_mon': row[4] or 0,
                'best_trade_pct': row[5] or 0,
                'worst_trade_pct': row[6] or 0,
                'period_days': days
            }
        return {'total_trades': 0, 'period_days': days}
    
    def get_best_whales(self, limit: int = 10) -> List[Dict]:
        """Get highest performing whales to follow"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        c.execute('''SELECT address, total_trades, winning_trades, 
                     avg_pnl_percent, trust_score
                     FROM whale_profiles 
                     WHERE total_trades >= 3
                     ORDER BY trust_score DESC, avg_pnl_percent DESC
                     LIMIT ?''', (limit,))
        
        rows = c.fetchall()
        conn.close()
        
        return [{
            'address': row[0],
            'total_trades': row[1],
            'win_rate': row[2] / row[1] if row[1] > 0 else 0,
            'avg_pnl_percent': row[3],
            'trust_score': row[4]
        } for row in rows]
