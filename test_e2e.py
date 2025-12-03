#!/usr/bin/env python3
"""
🧪 E2E TEST - Symuluj whale signal i przetestuj cały przepływ
"""
import asyncio
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agents.base_agent import Message, MessageTypes, Channels
from agents import decision_logger

# Przykładowy token (fake dla testu)
TEST_TOKEN = "0x1234567890123456789012345678901234567890"
TEST_WHALE = "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd"


async def test_e2e():
    """Test end-to-end flow"""
    print("\n" + "="*60)
    print("🧪 E2E TEST - Monad Trading Bot")
    print("="*60 + "\n")
    
    # 1. Test Decision Logger
    print("📊 Testing Decision Logger...")
    
    # Log test whale signal
    decision_logger.log_whale_signal({
        "token": TEST_TOKEN,
        "whale": TEST_WHALE,
        "amount_mon": 500.0,
        "tx_hash": "0x" + "a"*64
    })
    print("  ✅ Logged whale signal")
    
    # Log test risk check (pass)
    decision_logger.log_risk_check(
        TEST_TOKEN, 
        True, 
        "All checks passed",
        {"tax_percent": 5.0, "liquidity_usd": 50000, "is_honeypot": False}
    )
    print("  ✅ Logged risk check (passed)")
    
    # Log test AI decision
    decision_logger.log_ai_decision(
        TEST_TOKEN,
        {"action": "BUY", "confidence": 85, "amount_mon": 15, "reason": "Strong whale signal, good liquidity"},
        {"amount_mon": 500, "tax_percent": 5.0, "liquidity_usd": 50000, "pump_1h": 20}
    )
    print("  ✅ Logged AI decision (BUY)")
    
    # Log test trade
    decision_logger.log_trade(
        token=TEST_TOKEN,
        action="BUY",
        amount_mon=15.0,
        tx_hash="0x" + "b"*64,
        success=True,
        whale_amount=500,
        ai_confidence=85
    )
    print("  ✅ Logged trade (success)")
    
    # 2. Get stats
    print("\n📈 Decision Stats:")
    stats = decision_logger.get_stats()
    for key, value in stats.items():
        print(f"  {key}: {value}")
    
    # 3. Test Message classes
    print("\n📨 Testing Message System...")
    
    msg = Message(
        type=MessageTypes.WHALE_BUY,
        data={"token": TEST_TOKEN, "amount_mon": 300},
        sender="TestAgent"
    )
    print(f"  ✅ Created message: {msg.type}")
    
    json_str = msg.to_json()
    msg2 = Message.from_json(json_str)
    print(f"  ✅ Serialization works: {msg2.type}")
    
    # 4. Export ML data
    print("\n🤖 ML Export Test:")
    ml_data = decision_logger.export_for_ml()
    print(f"  Total entries: {len(ml_data)}")
    if ml_data:
        print(f"  Latest entry type: {ml_data[-1].get('type')}")
        print(f"\n  📋 Sample entries:")
        for entry in ml_data[-3:]:
            print(f"    - {entry['type']}: {entry.get('token', '')[:16]}...")
    
    print("\n" + "="*60)
    print("✅ E2E TEST COMPLETE")
    print("="*60 + "\n")
    
    return True


if __name__ == "__main__":
    success = asyncio.run(test_e2e())
    sys.exit(0 if success else 1)
