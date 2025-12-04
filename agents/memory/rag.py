"""
🔍 TRADING RAG - Retrieval Augmented Generation for Trading Decisions
Uses embeddings to find similar past situations and learn from them
"""
import json
import time
import hashlib
from typing import Optional, List, Dict, Any, Tuple
from dataclasses import dataclass
import numpy as np
from pathlib import Path
import sqlite3


@dataclass
class TradingContext:
    """Context for a trading decision"""
    token: str
    token_name: Optional[str] = None
    mcap_usd: Optional[float] = None
    volume_24h: Optional[float] = None
    holders: Optional[int] = None
    liquidity_usd: Optional[float] = None
    price_change_1h: Optional[float] = None
    price_change_24h: Optional[float] = None
    whale_address: Optional[str] = None
    whale_amount_mon: Optional[float] = None
    ai_score: Optional[float] = None
    trigger_type: str = 'unknown'
    
    def to_text(self) -> str:
        """Convert to text for embedding"""
        parts = [f"Token: {self.token}"]
        if self.token_name:
            parts.append(f"Name: {self.token_name}")
        if self.mcap_usd:
            parts.append(f"MCap: ${self.mcap_usd:,.0f}")
        if self.volume_24h:
            parts.append(f"Volume 24h: ${self.volume_24h:,.0f}")
        if self.holders:
            parts.append(f"Holders: {self.holders}")
        if self.liquidity_usd:
            parts.append(f"Liquidity: ${self.liquidity_usd:,.0f}")
        if self.price_change_1h:
            parts.append(f"1h change: {self.price_change_1h:+.1f}%")
        if self.price_change_24h:
            parts.append(f"24h change: {self.price_change_24h:+.1f}%")
        if self.whale_address:
            parts.append(f"Whale: {self.whale_address[:10]}...")
        if self.whale_amount_mon:
            parts.append(f"Whale amount: {self.whale_amount_mon:.1f} MON")
        if self.ai_score:
            parts.append(f"AI Score: {self.ai_score}")
        parts.append(f"Trigger: {self.trigger_type}")
        return " | ".join(parts)
    
    def to_vector(self) -> np.ndarray:
        """Convert to numerical vector for similarity search"""
        # Normalize values to 0-1 range
        features = [
            min((self.mcap_usd or 0) / 10_000_000, 1.0),  # Max 10M mcap
            min((self.volume_24h or 0) / 1_000_000, 1.0),  # Max 1M volume
            min((self.holders or 0) / 10000, 1.0),  # Max 10k holders
            min((self.liquidity_usd or 0) / 500_000, 1.0),  # Max 500k liq
            (self.price_change_1h or 0) / 200 + 0.5,  # -100% to +100% -> 0 to 1
            (self.price_change_24h or 0) / 500 + 0.5,  # -250% to +250% -> 0 to 1
            min((self.whale_amount_mon or 0) / 1000, 1.0),  # Max 1000 MON
            (self.ai_score or 50) / 100,  # 0-100 -> 0-1
            # Trigger type encoding
            1.0 if self.trigger_type == 'whale_copy' else 0.0,
            1.0 if self.trigger_type == 'snipe' else 0.0,
            1.0 if self.trigger_type == 'ai_signal' else 0.0,
        ]
        return np.array(features, dtype=np.float32)


class TradingRAG:
    """
    RAG system for trading decisions
    - Stores past trading contexts with outcomes
    - Finds similar situations from history
    - Provides relevant lessons and patterns
    """
    
    def __init__(self, db_path: str = "data/trading_rag.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        
        # Cache for faster similarity search
        self._vector_cache: List[Tuple[str, np.ndarray]] = []
        self._load_vectors()
        
    def _init_db(self):
        """Initialize RAG database"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        # Trading contexts with outcomes
        c.execute('''CREATE TABLE IF NOT EXISTS contexts (
            id TEXT PRIMARY KEY,
            timestamp REAL NOT NULL,
            context_text TEXT NOT NULL,
            context_vector BLOB NOT NULL,
            outcome TEXT,  -- 'profit', 'loss', 'breakeven', 'pending'
            pnl_percent REAL,
            hold_time_hours REAL,
            exit_reason TEXT,
            lessons_learned TEXT,
            trade_id TEXT
        )''')
        
        # Knowledge chunks for RAG
        c.execute('''CREATE TABLE IF NOT EXISTS knowledge (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            vector BLOB,
            source TEXT,
            relevance_score REAL DEFAULT 0.5,
            created_at REAL
        )''')
        
        conn.commit()
        conn.close()
        
        # Seed with trading knowledge if empty
        self._seed_knowledge()
        
    def _seed_knowledge(self):
        """Add base trading knowledge"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        c.execute('SELECT COUNT(*) FROM knowledge')
        if c.fetchone()[0] > 0:
            conn.close()
            return
            
        knowledge = [
            # Whale following
            ("whale_strategy", "Copy whale with history", 
             "Only copy whales that have 3+ profitable trades in our database. "
             "New unknown whales need smaller position sizes."),
            ("whale_strategy", "Whale exit = our exit",
             "When the whale we copied sells, we should sell too. "
             "They likely have alpha we don't."),
            ("whale_strategy", "Big whale = small copy",
             "If whale buys 500+ MON, copy with smaller amount (10-20 MON). "
             "They can afford losses we can't."),
             
            # Token analysis
            ("token_analysis", "Low mcap = high risk",
             "Tokens under $10k mcap are extremely risky. Max 5 MON position. "
             "Tokens under $50k need careful analysis."),
            ("token_analysis", "Holder concentration",
             "If top 10 holders own >60% of supply, be very careful. "
             "High concentration = rug risk."),
            ("token_analysis", "Volume/MCap ratio",
             "Healthy tokens have 24h volume at least 10% of mcap. "
             "Very low volume = hard to exit."),
             
            # Entry timing
            ("entry_timing", "Avoid FOMO entries",
             "Don't enter tokens pumping >100% in 1h. "
             "Wait for pullback or skip entirely."),
            ("entry_timing", "First hour after launch",
             "New token launches are highest risk and highest reward. "
             "Use sniper with small amounts only."),
             
            # Exit strategy
            ("exit_strategy", "Take profits early",
             "Sell 30-50% at 50-100% profit. "
             "Never let a winning trade become a loser."),
            ("exit_strategy", "Cut losses fast",
             "If down 15-20% and no catalyst, exit. "
             "Preservation of capital is priority."),
            ("exit_strategy", "Trailing stop after 30%",
             "Once 30%+ profit, use 15% trailing stop. "
             "Let winners run but protect gains."),
             
            # Risk management
            ("risk_management", "Position sizing",
             "Never more than 5% of portfolio in single trade. "
             "Lower for untested whales or new tokens."),
            ("risk_management", "Daily loss limit",
             "Stop trading if down 10% on the day. "
             "Come back tomorrow with fresh perspective."),
            ("risk_management", "Correlation risk",
             "Don't hold 5 meme coins at once. "
             "They all dump together when market turns."),
        ]
        
        for category, title, content in knowledge:
            c.execute('''INSERT INTO knowledge (category, title, content, created_at)
                         VALUES (?, ?, ?, ?)''',
                      (category, title, content, time.time()))
        
        conn.commit()
        conn.close()
        
    def _load_vectors(self):
        """Load context vectors into memory for fast search"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        c.execute('SELECT id, context_vector FROM contexts')
        for row in c.fetchall():
            vector = np.frombuffer(row[1], dtype=np.float32)
            self._vector_cache.append((row[0], vector))
            
        conn.close()
        
    def store_context(self, 
                      context: TradingContext,
                      trade_id: Optional[str] = None) -> str:
        """Store a trading context for later retrieval"""
        context_id = hashlib.md5(
            f"{context.token}_{time.time()}".encode()
        ).hexdigest()[:16]
        
        text = context.to_text()
        vector = context.to_vector()
        
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        c.execute('''INSERT INTO contexts 
            (id, timestamp, context_text, context_vector, outcome, trade_id)
            VALUES (?, ?, ?, ?, 'pending', ?)''',
            (context_id, time.time(), text, vector.tobytes(), trade_id))
        
        conn.commit()
        conn.close()
        
        # Update cache
        self._vector_cache.append((context_id, vector))
        
        return context_id
    
    def update_outcome(self,
                       context_id: str,
                       outcome: str,
                       pnl_percent: float,
                       hold_time_hours: float,
                       exit_reason: str,
                       lessons: Optional[str] = None):
        """Update context with trade outcome"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        c.execute('''UPDATE contexts SET 
            outcome = ?, pnl_percent = ?, hold_time_hours = ?,
            exit_reason = ?, lessons_learned = ?
            WHERE id = ?''',
            (outcome, pnl_percent, hold_time_hours, exit_reason, lessons, context_id))
        
        conn.commit()
        conn.close()
    
    def find_similar(self, 
                     context: TradingContext,
                     limit: int = 5,
                     min_similarity: float = 0.7) -> List[Dict]:
        """Find similar past trading contexts"""
        query_vector = context.to_vector()
        
        similarities = []
        for ctx_id, vector in self._vector_cache:
            # Cosine similarity
            sim = np.dot(query_vector, vector) / (
                np.linalg.norm(query_vector) * np.linalg.norm(vector) + 1e-8
            )
            if sim >= min_similarity:
                similarities.append((ctx_id, sim))
        
        # Sort by similarity
        similarities.sort(key=lambda x: x[1], reverse=True)
        top_ids = [s[0] for s in similarities[:limit]]
        
        if not top_ids:
            return []
            
        # Fetch full contexts
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        placeholders = ','.join('?' * len(top_ids))
        c.execute(f'''SELECT id, context_text, outcome, pnl_percent, 
                      hold_time_hours, exit_reason, lessons_learned
                      FROM contexts WHERE id IN ({placeholders})''', top_ids)
        
        rows = c.fetchall()
        conn.close()
        
        # Build result with similarities
        sim_map = {s[0]: s[1] for s in similarities}
        results = []
        for row in rows:
            results.append({
                'id': row[0],
                'context': row[1],
                'outcome': row[2],
                'pnl_percent': row[3],
                'hold_time_hours': row[4],
                'exit_reason': row[5],
                'lessons': row[6],
                'similarity': sim_map.get(row[0], 0)
            })
        
        results.sort(key=lambda x: x['similarity'], reverse=True)
        return results
    
    def get_relevant_knowledge(self, 
                               query: str,
                               category: Optional[str] = None,
                               limit: int = 5) -> List[Dict]:
        """Get relevant knowledge chunks"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        if category:
            c.execute('''SELECT category, title, content, relevance_score
                         FROM knowledge WHERE category = ?
                         ORDER BY relevance_score DESC LIMIT ?''',
                      (category, limit))
        else:
            # Simple keyword matching for now
            # TODO: Add proper embedding-based search
            keywords = query.lower().split()
            c.execute('''SELECT category, title, content, relevance_score
                         FROM knowledge 
                         ORDER BY relevance_score DESC''')
        
        rows = c.fetchall()
        conn.close()
        
        results = []
        for row in rows:
            score = row[3]
            # Boost score if keywords match
            content_lower = row[2].lower()
            for kw in query.lower().split():
                if kw in content_lower:
                    score += 0.1
                    
            results.append({
                'category': row[0],
                'title': row[1],
                'content': row[2],
                'relevance': min(score, 1.0)
            })
        
        results.sort(key=lambda x: x['relevance'], reverse=True)
        return results[:limit]
    
    def generate_advice(self, context: TradingContext) -> Dict:
        """Generate trading advice based on similar past situations"""
        # Find similar past contexts
        similar = self.find_similar(context, limit=5)
        
        # Get relevant knowledge
        knowledge = self.get_relevant_knowledge(
            f"{context.trigger_type} {context.token_name or ''}"
        )
        
        # Analyze outcomes of similar trades
        outcomes = {'profit': 0, 'loss': 0, 'breakeven': 0}
        avg_pnl = []
        lessons = []
        
        for ctx in similar:
            if ctx['outcome'] in outcomes:
                outcomes[ctx['outcome']] += 1
            if ctx['pnl_percent']:
                avg_pnl.append(ctx['pnl_percent'])
            if ctx['lessons']:
                lessons.append(ctx['lessons'])
        
        # Calculate historical success rate
        total = sum(outcomes.values())
        success_rate = outcomes['profit'] / total if total > 0 else 0.5
        
        # Generate recommendation
        if success_rate >= 0.7 and len(similar) >= 3:
            recommendation = "STRONG BUY"
            confidence = 0.8
        elif success_rate >= 0.5:
            recommendation = "CAUTIOUS BUY"
            confidence = 0.6
        elif success_rate < 0.3 and len(similar) >= 3:
            recommendation = "AVOID"
            confidence = 0.7
        else:
            recommendation = "NEUTRAL - Limited data"
            confidence = 0.4
        
        return {
            'recommendation': recommendation,
            'confidence': confidence,
            'similar_trades': len(similar),
            'historical_success_rate': success_rate,
            'avg_historical_pnl': np.mean(avg_pnl) if avg_pnl else None,
            'relevant_lessons': lessons[:3],
            'knowledge_tips': [k['content'] for k in knowledge[:3]],
            'similar_contexts': similar[:3]
        }
