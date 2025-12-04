"""
🧠 SMART TRADING AGENT - Agent with Memory and Learning
Uses short-term, long-term memory and RAG for intelligent trading decisions
"""
import asyncio
import time
from typing import Optional, Dict, List, Any
from dataclasses import dataclass

from .memory.short_term import ShortTermMemory
from .memory.long_term import LongTermMemory, TradeRecord
from .memory.rag import TradingRAG, TradingContext


@dataclass
class TradingDecision:
    """A trading decision with full context"""
    action: str  # 'buy', 'sell', 'hold', 'skip'
    token: str
    amount_mon: float
    confidence: float
    reasoning: List[str]
    historical_context: Optional[Dict] = None
    warnings: List[str] = None
    

class SmartTradingAgent:
    """
    Intelligent trading agent with memory systems:
    
    1. SHORT-TERM MEMORY (ShortTermMemory)
       - Active positions
       - Recent whale activity
       - Pending signals
       - Last 1 hour of context
       
    2. LONG-TERM MEMORY (LongTermMemory)  
       - Complete trade history
       - Whale profiles (trust scores)
       - Token patterns
       - Learned lessons
       
    3. RAG SYSTEM (TradingRAG)
       - Find similar past situations
       - Trading knowledge base
       - Generate contextual advice
    """
    
    def __init__(self, 
                 agent_name: str = "SmartTrader",
                 data_dir: str = "data"):
        self.name = agent_name
        self.data_dir = data_dir
        
        # Initialize memory systems
        self.short_memory = ShortTermMemory(max_items=1000, default_ttl=7200)
        self.long_memory = LongTermMemory(f"{data_dir}/agent_memory.db")
        self.rag = TradingRAG(f"{data_dir}/trading_rag.db")
        
        # Load any persisted short-term memory
        try:
            self.short_memory.load_from_file(f"{data_dir}/short_memory.json")
        except:
            pass
            
        # Agent state
        self.is_running = False
        self.total_decisions = 0
        self.successful_decisions = 0
        
    async def evaluate_trade(self, 
                             token: str,
                             trigger_type: str,
                             whale_address: Optional[str] = None,
                             whale_amount: Optional[float] = None,
                             token_data: Optional[Dict] = None) -> TradingDecision:
        """
        Evaluate a potential trade using all memory systems
        """
        self.total_decisions += 1
        reasoning = []
        warnings = []
        
        # 1. BUILD CONTEXT
        context = TradingContext(
            token=token,
            token_name=token_data.get('name') if token_data else None,
            mcap_usd=token_data.get('mcap') if token_data else None,
            volume_24h=token_data.get('volume_24h') if token_data else None,
            holders=token_data.get('holders') if token_data else None,
            liquidity_usd=token_data.get('liquidity') if token_data else None,
            price_change_1h=token_data.get('price_change_1h') if token_data else None,
            price_change_24h=token_data.get('price_change_24h') if token_data else None,
            whale_address=whale_address,
            whale_amount_mon=whale_amount,
            ai_score=token_data.get('ai_score') if token_data else None,
            trigger_type=trigger_type
        )
        
        # 2. CHECK SHORT-TERM MEMORY
        # Already have position in this token?
        active_positions = self.short_memory.get_active_positions()
        if token in active_positions:
            reasoning.append(f"Already have position in {token}")
            return TradingDecision(
                action='skip',
                token=token,
                amount_mon=0,
                confidence=1.0,
                reasoning=reasoning,
                warnings=["Duplicate position prevented"]
            )
        
        # Recent activity on this token?
        recent_trades = self.short_memory.recall(type='trade', limit=20)
        recent_on_token = [t for t in recent_trades if t['content'].get('token') == token]
        if recent_on_token:
            last_trade = recent_on_token[0]
            age_min = last_trade['age_seconds'] / 60
            if age_min < 30:
                warnings.append(f"Traded this token {age_min:.0f} min ago")
        
        # 3. CHECK LONG-TERM MEMORY
        base_amount = 10.0  # Default position size
        
        # Whale profile check
        if whale_address:
            whale_profile = self.long_memory.get_whale_profile(whale_address)
            if whale_profile:
                trust = whale_profile.get('trust_score') or 0.5
                win_rate = whale_profile.get('win_rate') or 0
                trades = whale_profile.get('total_trades') or 0
                
                reasoning.append(
                    f"Whale {whale_address[:10]}: {trades} trades, "
                    f"{win_rate:.0%} win rate, trust: {trust:.2f}"
                )
                
                # Adjust position based on trust
                if trust >= 0.7 and trades >= 5:
                    base_amount *= 1.5
                    reasoning.append("Increasing position - trusted whale")
                elif trust < 0.4:
                    base_amount *= 0.5
                    warnings.append("Reducing position - low trust whale")
                elif trades < 3:
                    base_amount *= 0.7
                    warnings.append("Reducing position - new whale, limited history")
            else:
                reasoning.append(f"New whale - no history")
                base_amount *= 0.5
                warnings.append("First trade with this whale - small position")
        
        # Token history check
        similar_trades = self.long_memory.get_similar_trades(token=token, limit=5)
        if similar_trades:
            avg_pnl = sum(t.get('pnl_percent', 0) or 0 for t in similar_trades) / len(similar_trades)
            reasoning.append(f"Token history: {len(similar_trades)} trades, avg PnL: {avg_pnl:.1f}%")
            
            if avg_pnl < -20:
                warnings.append(f"Token has poor history (avg {avg_pnl:.1f}%)")
                base_amount *= 0.5
        
        # 4. RAG ADVICE
        rag_advice = self.rag.generate_advice(context)
        reasoning.append(f"RAG: {rag_advice['recommendation']} ({rag_advice['confidence']:.0%})")
        
        if rag_advice['similar_trades'] >= 3:
            reasoning.append(
                f"Based on {rag_advice['similar_trades']} similar trades: "
                f"{rag_advice['historical_success_rate']:.0%} success rate"
            )
        
        for tip in rag_advice.get('knowledge_tips', [])[:2]:
            reasoning.append(f"💡 {tip[:100]}")
        
        # Adjust based on RAG
        if rag_advice['recommendation'] == 'AVOID':
            return TradingDecision(
                action='skip',
                token=token,
                amount_mon=0,
                confidence=rag_advice['confidence'],
                reasoning=reasoning,
                warnings=warnings + ["RAG recommends avoiding based on history"],
                historical_context=rag_advice
            )
        elif rag_advice['recommendation'] == 'STRONG BUY':
            base_amount *= 1.3
            
        # 5. GET LEARNED LESSONS
        lessons = self.long_memory.get_lessons(min_confidence=0.6, limit=5)
        relevant_lessons = []
        for lesson in lessons:
            if trigger_type in lesson['lesson'].lower() or 'whale' in lesson['lesson'].lower():
                relevant_lessons.append(lesson['lesson'])
                
        if relevant_lessons:
            reasoning.append(f"Applying {len(relevant_lessons)} learned lessons")
            
        # 6. FINAL DECISION
        # Confidence calculation
        confidence = 0.5
        whale_trust = (whale_profile.get('trust_score') or 0.5) if whale_profile else 0.5
        if whale_trust > 0.6:
            confidence += 0.2
        if rag_advice['confidence'] > 0.6:
            confidence += 0.15
        if len(similar_trades) >= 3 and avg_pnl > 10:
            confidence += 0.15
            
        confidence = min(confidence, 0.95)
        
        # Cap amount based on risk
        max_amount = 50.0  # Never more than 50 MON per trade
        final_amount = min(base_amount, max_amount)
        
        # Determine action
        if confidence >= 0.5:
            action = 'buy'
        elif confidence >= 0.3:
            action = 'hold'  # Wait for more info
            final_amount = 0
        else:
            action = 'skip'
            final_amount = 0
            
        decision = TradingDecision(
            action=action,
            token=token,
            amount_mon=final_amount,
            confidence=confidence,
            reasoning=reasoning,
            warnings=warnings,
            historical_context=rag_advice
        )
        
        # Remember this decision
        self.short_memory.remember('decision', {
            'token': token,
            'action': action,
            'amount': final_amount,
            'confidence': confidence,
            'trigger': trigger_type,
            'whale': whale_address
        }, importance=0.7)
        
        # Store context for future RAG
        self.rag.store_context(context)
        
        return decision
    
    def record_trade_result(self,
                            token: str,
                            entry_price: float,
                            exit_price: float,
                            amount_mon: float,
                            trigger_type: str,
                            whale_address: Optional[str] = None,
                            exit_reason: str = 'manual',
                            market_context: Optional[Dict] = None):
        """Record a completed trade for learning"""
        
        pnl_percent = ((exit_price - entry_price) / entry_price) * 100
        pnl_mon = amount_mon * (pnl_percent / 100)
        
        # Create trade record
        trade = TradeRecord(
            id=f"{token}_{int(time.time())}",
            token=token,
            token_name=market_context.get('name') if market_context else None,
            entry_time=time.time() - 3600,  # Approximate
            exit_time=time.time(),
            entry_price=entry_price,
            exit_price=exit_price,
            amount_mon=amount_mon,
            pnl_percent=pnl_percent,
            pnl_mon=pnl_mon,
            trigger_type=trigger_type,
            whale_address=whale_address,
            ai_score=market_context.get('ai_score') if market_context else None,
            market_context=market_context or {},
            exit_reason=exit_reason,
            notes=None
        )
        
        # Store in long-term memory
        self.long_memory.record_trade(trade)
        
        # Update short-term memory
        self.short_memory.remember('trade_complete', {
            'token': token,
            'pnl_percent': pnl_percent,
            'pnl_mon': pnl_mon,
            'exit_reason': exit_reason
        }, importance=0.8)
        
        # Close position in short-term memory
        self.short_memory.close_position(token)
        
        # Learn lessons
        if pnl_percent > 50:
            self.long_memory.learn_lesson(
                'success_pattern',
                f"Profitable {trigger_type} trade on token similar to {token}",
                confidence=0.6,
                example_trade_id=trade.id
            )
        elif pnl_percent < -20:
            self.long_memory.learn_lesson(
                'avoid_pattern',
                f"Loss on {trigger_type} - {exit_reason}",
                confidence=0.5,
                example_trade_id=trade.id
            )
            
        # Update successful decisions count
        if pnl_percent > 0:
            self.successful_decisions += 1
            
    def open_position(self, 
                      token: str,
                      amount_mon: float,
                      entry_price: float,
                      trigger_type: str,
                      whale_address: Optional[str] = None):
        """Record opening a new position"""
        self.short_memory.remember('position', {
            'token': token,
            'amount_mon': amount_mon,
            'entry_price': entry_price,
            'entry_time': time.time(),
            'trigger_type': trigger_type,
            'whale_address': whale_address,
            'current_price': entry_price,
            'pnl_percent': 0
        }, importance=0.9, ttl=86400)  # Keep for 24h
        
    def get_status(self) -> Dict:
        """Get agent status summary"""
        context = self.short_memory.get_context_summary()
        stats = self.long_memory.get_trading_stats(days=7)
        best_whales = self.long_memory.get_best_whales(limit=5)
        
        return {
            'agent_name': self.name,
            'total_decisions': self.total_decisions,
            'success_rate': self.successful_decisions / max(self.total_decisions, 1),
            'short_term_context': context,
            'weekly_stats': stats,
            'top_whales': best_whales,
            'lessons_learned': len(self.long_memory.get_lessons(min_confidence=0.5))
        }
        
    def save_state(self):
        """Persist agent state"""
        self.short_memory.save_to_file(f"{self.data_dir}/short_memory.json")
        
    def cleanup(self):
        """Clean up expired memories"""
        self.short_memory.cleanup()
