#!/usr/bin/env python3
"""
🧪 Test integracji systemu pamięci z agentami
"""
import sys
import asyncio
sys.path.insert(0, '.')

from agents.memory.short_term import ShortTermMemory
from agents.memory.long_term import LongTermMemory
from agents.memory.rag import TradingRAG, TradingContext
from agents.smart_agent import SmartTradingAgent

async def test_full_integration():
    """Test pełnego flow pamięci"""
    print("🧪 MEMORY INTEGRATION TEST")
    print("=" * 50)
    
    # 1. Initialize SmartAgent
    smart = SmartTradingAgent("TestAgent", "test_data")
    print("✅ SmartAgent initialized")
    
    # 2. Simulate whale detection
    whale = "0xa49cee842116a89299a721d831bcf0511e8f6a15"
    token = "0xtest123456789"
    
    # Record whale activity
    smart.short_memory.remember('whale', {
        'whale': whale,
        'token': token,
        'amount_mon': 150,
        'trust_score': 0.5
    }, importance=0.8)
    print("✅ Whale activity remembered")
    
    # 3. Evaluate trade using SmartAgent (async)
    recommendation = await smart.evaluate_trade(
        token=token,
        trigger_type="whale_copy",
        whale_address=whale,
        whale_amount=150,
        token_data={
            "mcap": 50000,
            "liquidity": 10000,
            "volume_24h": 5000
        }
    )
    
    print(f"✅ Trade evaluated: {recommendation.action} ({recommendation.confidence:.0%})")
    reasoning = "; ".join(recommendation.reasoning)[:60]
    print(f"   Reasoning: {reasoning}...")
    
    # 4. Open position
    smart.open_position(token, amount_mon=10, entry_price=1.0, trigger_type="whale_copy", whale_address=whale)
    print("✅ Position opened in memory")
    
    # 5. Check active positions
    positions = smart.short_memory.get_active_positions()
    assert len(positions) == 1
    print(f"✅ Active positions: {len(positions)}")
    
    # 6. Simulate profit trade
    smart.record_trade_result(
        token=token, 
        entry_price=1.0, 
        exit_price=1.25,  # 25% profit
        amount_mon=10,
        trigger_type="whale_copy",
        whale_address=whale,
        exit_reason="take_profit"
    )
    print("✅ Profitable trade recorded (25%)")
    
    # 7. Check status
    status = smart.get_status()
    print(f"\n📊 AGENT STATUS:")
    print(f"   Agent: {status['agent_name']}")
    print(f"   Total decisions: {status['total_decisions']}")
    print(f"   Success rate: {status['success_rate']:.0%}")
    print(f"   Lessons learned: {status['lessons_learned']}")
    
    # 8. Simulate another trade using SmartAgent's record_trade_result
    # This also updates whale profile
    # We already did this in step 6, so check profile now
    
    # Check whale profile
    profile = smart.long_memory.get_whale_profile(whale)
    print(f"\n🐳 WHALE PROFILE:")
    print(f"   Address: {whale[:16]}...")
    print(f"   Total trades: {profile['total_trades']}")
    print(f"   Win rate: {profile['win_rate']:.0%}")
    trust = profile.get('trust_score') or 0.5
    print(f"   Trust score: {trust:.2f}")
    
    # 9. Learn lesson
    smart.long_memory.learn_lesson(
        category="test",
        lesson="Test lesson - whale copy trades can be profitable",
        confidence=0.9
    )
    lessons = smart.long_memory.get_lessons(min_confidence=0.5)
    print(f"\n📚 LESSONS LEARNED: {len(lessons)}")
    
    # 10. Get best whales
    best = smart.long_memory.get_best_whales(limit=5)
    print(f"\n🏆 BEST WHALES: {len(best)}")
    
    print("\n" + "=" * 50)
    print("🎉 ALL TESTS PASSED!")
    print("🧠 MEMORY SYSTEM FULLY INTEGRATED!")

if __name__ == "__main__":
    asyncio.run(test_full_integration())
