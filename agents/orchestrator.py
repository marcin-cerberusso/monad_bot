"""
🎭 ORCHESTRATOR - Uruchamia i zarządza wszystkimi agentami
"""
import asyncio
import signal
import sys
import os
from datetime import datetime
from dotenv import load_dotenv

from .whale_agent import WhaleAgent
from .risk_agent import RiskAgent
from .ai_agent import AIAgent
from .trader_agent import TraderAgent
from .position_agent import PositionAgent
from .leverage_agent import LeverageAgent, LeverageConfig

load_dotenv()

REDIS_URL = os.getenv("DRAGONFLY_URL", "redis://localhost:6379")


class Orchestrator:
    """Zarządza wszystkimi agentami"""
    
    def __init__(self):
        self.agents = []
        self.running = False
        
    def setup_agents(self):
        """Inicjalizuj agentów"""
        self.agents = [
            WhaleAgent(REDIS_URL),
            RiskAgent(REDIS_URL),
            AIAgent(REDIS_URL),
            TraderAgent(REDIS_URL),
            PositionAgent(REDIS_URL),
        ]
        
        # LeverageAgent - opcjonalny, tylko gdy skonfigurowany
        if LeverageConfig.ENABLED:
            # LeverageAgent używa własnego message bus - dodamy go osobno
            pass  # Na razie działa standalone przez AI signals
        
    async def start(self):
        """Uruchom wszystkich agentów"""
        from .notifications import notifier
        await notifier.start()
        
        self.running = True
        self.setup_agents()
        
        print("="*60)
        print("🤖 MONAD BOT - AI AGENT SYSTEM")
        print("="*60)
        print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Redis: {REDIS_URL}")
        print(f"Agents: {len(self.agents)}")
        print(f"Leverage: {'ENABLED (3x, min 85% confidence)' if LeverageConfig.ENABLED else 'DISABLED'}")
        print()
        
        for agent in self.agents:
            print(f"  ✅ {agent.name}")
        if LeverageConfig.ENABLED:
            print(f"  🔥 LeverageAgent (LeverUp {LeverageConfig.DEFAULT_LEVERAGE}x)")
        print()
        print("Flow: Whale -> Risk -> AI -> Trader -> Position")
        print("      AI (85%+) -> Leverage (3x long)")
        print("="*60)
        print()
        
        # Start all agents
        tasks = [asyncio.create_task(agent.start()) for agent in self.agents]
        
        # Wait for all
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
    
    async def stop(self):
        """Zatrzymaj wszystkich agentów"""
        self.running = False
        print("\n🛑 Stopping agents...")
        
        for agent in self.agents:
            await agent.stop()
            
        from .notifications import notifier
        await notifier.stop()
        
        print("All agents stopped.")


async def main():
    """Main entry point"""
    orchestrator = Orchestrator()
    
    # Handle Ctrl+C
    def signal_handler(sig, frame):
        print("\nReceived shutdown signal...")
        asyncio.create_task(orchestrator.stop())
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    try:
        await orchestrator.start()
    except KeyboardInterrupt:
        await orchestrator.stop()


if __name__ == "__main__":
    asyncio.run(main())
