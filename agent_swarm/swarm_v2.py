#!/usr/bin/env python3
"""
🐝 SWARM V2 - Unified Entry Point for Agent Swarm System

Uruchamia wszystkie komponenty:
1. Dragonfly Message Bus
2. Orchestrator V2
3. Sell Executor V2
4. Launcher V2 (price feed, signal watchers)

Usage:
    python swarm_v2.py                    # Start all
    python swarm_v2.py --orchestrator     # Only orchestrator
    python swarm_v2.py --sell-executor    # Only sell executor
    python swarm_v2.py --launcher         # Only launcher
"""

import argparse
import asyncio
import os
import signal
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

# Components
from agent_swarm.message_bus import get_bus, shutdown_all
from agent_swarm.message_types import MessageType


async def run_orchestrator():
    """Run only orchestrator"""
    from agent_swarm.orchestrator_v2 import OrchestratorV2
    
    print("🎭 Starting Orchestrator V2...")
    orchestrator = OrchestratorV2()
    
    def shutdown():
        asyncio.create_task(orchestrator.stop())
        
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown)
        
    await orchestrator.start()


async def run_sell_executor():
    """Run only sell executor"""
    from agent_swarm.sell_executor_v2 import SellExecutorV2
    
    print("💰 Starting Sell Executor V2...")
    executor = SellExecutorV2()
    
    def shutdown():
        asyncio.create_task(executor.stop())
        
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown)
        
    await executor.start()


async def run_launcher():
    """Run only launcher"""
    from agent_swarm.launcher_v2 import AgentSwarmLauncherV2
    
    print("🚀 Starting Launcher V2...")
    launcher = AgentSwarmLauncherV2()
    
    def shutdown():
        asyncio.create_task(launcher.stop())
        
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown)
        
    await launcher.run()


async def run_all():
    """Run all components together"""
    print("=" * 70)
    print("🐝 AGENT SWARM V2 - FULL SYSTEM")
    print("   Orchestrator + Sell Executor + Launcher")
    print("=" * 70)
    print(f"\n📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    # Import components
    from agent_swarm.orchestrator_v2 import OrchestratorV2
    from agent_swarm.sell_executor_v2 import SellExecutorV2
    from agent_swarm.launcher_v2 import AgentSwarmLauncherV2
    
    # Create instances
    orchestrator = OrchestratorV2()
    sell_executor = SellExecutorV2()
    launcher = AgentSwarmLauncherV2()
    
    # Shutdown handler
    async def shutdown():
        print("\n🛑 Shutting down all components...")
        await launcher.stop()
        await sell_executor.stop()
        await orchestrator.stop()
        await shutdown_all()
        
    loop = asyncio.get_event_loop()
    
    def handle_signal():
        asyncio.create_task(shutdown())
        
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_signal)
        
    # Start all components
    print("\n🚀 Starting components...")
    
    try:
        # Run concurrently
        await asyncio.gather(
            orchestrator.start(),
            sell_executor.start(),
            launcher.run(),
            return_exceptions=True
        )
    except asyncio.CancelledError:
        pass
    finally:
        await shutdown()


async def test_connection():
    """Test Dragonfly connection"""
    print("🐉 Testing Dragonfly connection...")
    
    bus = await get_bus("test")
    
    if bus.connected:
        print("✅ Dragonfly connected successfully!")
        
        # Test pub/sub
        print("\n📤 Testing pub/sub...")
        await bus.subscribe("test")
        
        # Send test message
        from agent_swarm.message_types import Message, MessageType, Priority
        test_msg = Message(
            type=MessageType.SYSTEM_STATUS,
            sender="test",
            payload={"status": "test", "timestamp": datetime.now().isoformat()},
            priority=Priority.LOW
        )
        await bus.broadcast(test_msg)
        print("✅ Test message sent!")
        
        # Test state
        print("\n💾 Testing state storage...")
        await bus.set_state("test_key", {"value": 42})
        result = await bus.get_state("test_key")
        print(f"   Stored: {{'value': 42}}")
        print(f"   Retrieved: {result}")
        
        if result and result.get("value") == 42:
            print("✅ State storage working!")
        else:
            print("❌ State storage issue")
            
    else:
        print("⚠️ Dragonfly not connected - using in-memory bus")
        
    await bus.disconnect()
    print("\n✅ Test complete!")


def main():
    parser = argparse.ArgumentParser(description="Agent Swarm V2")
    parser.add_argument("--orchestrator", action="store_true", help="Run only orchestrator")
    parser.add_argument("--sell-executor", action="store_true", help="Run only sell executor")
    parser.add_argument("--launcher", action="store_true", help="Run only launcher")
    parser.add_argument("--test", action="store_true", help="Test Dragonfly connection")
    
    args = parser.parse_args()
    
    if args.test:
        asyncio.run(test_connection())
    elif args.orchestrator:
        asyncio.run(run_orchestrator())
    elif args.sell_executor:
        asyncio.run(run_sell_executor())
    elif args.launcher:
        asyncio.run(run_launcher())
    else:
        asyncio.run(run_all())


if __name__ == "__main__":
    main()
