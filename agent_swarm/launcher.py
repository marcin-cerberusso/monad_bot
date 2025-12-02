#!/usr/bin/env python3
"""
🚀 AGENT SWARM LAUNCHER - Uruchamia cały system Multi-Agent

Startuje:
1. CDN Price Feed (real-time prices)
2. Isolation Manager (sandbox dla każdego agenta)
3. Message Bus (komunikacja)
4. 4 wyspecjalizowanych agentów:
   - Scanner (monitoring 24/7)
   - Analyst (analiza, scoring)
   - Trader (wykonywanie transakcji)
   - Risk (zarządzanie ryzykiem)
5. Integration Bridge - połączenie z whale_follower
"""

import asyncio
import json
import os
import signal
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Add parent dir to path
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator import Orchestrator, Message, MessageType
from cdn_price_feed import PriceFeed, get_price_feed
from agent_isolation import get_isolation_manager, IsolationLevel
from sell_executor import SellExecutor, get_sell_executor
from config_validator import validate_and_exit_on_error, ConfigStatus
from dotenv import load_dotenv
from file_utils import safe_load_json, safe_save_json

load_dotenv()

# Config
WATCHED_WHALES = [
    "0x37556b2c49bebf840f2bec6e3c066fb93aee7f9e",
    "0xce4ac5d91f52a3e3099f80e3d3088e",
    "0x6c9eea5270",
]

WATCHED_TOKENS = [
    "0x5E1b1A14c8758104B8560514e94ab8320e587777",  # MonadMeme (portfolio)
]

# Integration file for whale_follower
SWARM_SIGNAL_FILE = Path(__file__).parent.parent / "swarm_signals.json"
SWARM_APPROVED_FILE = Path(__file__).parent.parent / "swarm_approved.json"
SELL_SIGNAL_FILE = Path(__file__).parent.parent / "sell_signals.json"


class AgentSwarmLauncher:
    """Main launcher for Agent Swarm system"""
    
    def __init__(self):
        self.orchestrator: Orchestrator = None
        self.price_feed: PriceFeed = None
        self.sell_executor: SellExecutor = None
        self.running = False
        self.config_status: ConfigStatus = None
        self._price_rate_limit = 5.0  # seconds between price fetches
        self._last_price_fetch = 0.0
        
    async def start(self):
        """Start everything"""
        print("=" * 70)
        print("🐝 AGENT SWARM LAUNCHER")
        print("   Multi-Agent Trading System for Monad")
        print("=" * 70)
        print(f"\n📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        
        # 0. Validate config (fail-fast)
        print("\n🔐 Validating configuration...")
        self.config_status = await validate_and_exit_on_error(
            require_trading=True,
            require_ai=True
        )
        print(f"   ✅ Config valid, chain: {self.config_status.chain_name}")
        
        # 1. Initialize Isolation Manager
        print("\n🔒 Initializing Isolation Manager...")
        isolation = get_isolation_manager()
        isolation.create_sandbox("scanner", IsolationLevel.HARD)
        isolation.create_sandbox("analyst", IsolationLevel.HARD)
        isolation.create_sandbox("trader", IsolationLevel.STRICT)
        isolation.create_sandbox("risk", IsolationLevel.STRICT)
        print("   ✅ 4 isolated sandboxes created")
        
        # 2. Start Price Feed (CDN)
        print("\n📡 Starting CDN Price Feed...")
        self.price_feed = get_price_feed()
        await self.price_feed.start()
        
        # Register callbacks
        self.price_feed.on_price_update(self._on_price_update)
        self.price_feed.on_whale_activity(self._on_whale_activity)
        print("   ✅ Price feed active")
        
        # 3. Start Orchestrator
        print("\n🧠 Starting Orchestrator...")
        self.orchestrator = Orchestrator(use_redis=False)  # In-memory for now
        await self.orchestrator.start()
        
        self.running = True
        
        # 4. Start background tasks
        # 4. Start Sell Executor
        print("\n💰 Starting Sell Executor...")
        self.sell_executor = get_sell_executor()
        await self.sell_executor.start()
        print("   ✅ Sell executor ready")
        
        # 5. Start background tasks
        print("\n🔄 Starting background tasks...")
        asyncio.create_task(self._price_monitor_loop())
        asyncio.create_task(self._health_check_loop())
        asyncio.create_task(self._watch_signal_file())  # Integration with whale_follower
        asyncio.create_task(self._watch_sell_signals())  # Sell signal monitoring
        asyncio.create_task(self.sell_executor.process_queue())  # Sell queue processor
        
        print("\n" + "=" * 70)
        print("✅ AGENT SWARM FULLY OPERATIONAL")
        print("=" * 70)
        
        # Show status
        await self._print_status()
        
    async def stop(self):
        """Stop everything"""
        print("\n🛑 Shutting down Agent Swarm...")
        self.running = False
        
        if self.orchestrator:
            await self.orchestrator.stop()
            
        if self.price_feed:
            await self.price_feed.stop()
        
        if self.sell_executor:
            await self.sell_executor.stop()
            
        print("✅ Shutdown complete")
        
    async def run(self):
        """Main loop"""
        await self.start()
        
        try:
            while self.running:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            await self.stop()
            
    async def _price_monitor_loop(self):
        """Monitor prices periodically with rate limiting"""
        import time
        
        while self.running:
            try:
                # Rate limiting
                now = time.time()
                if now - self._last_price_fetch < self._price_rate_limit:
                    await asyncio.sleep(self._price_rate_limit - (now - self._last_price_fetch))
                
                self._last_price_fetch = time.time()
                
                # Fetch prices for watched tokens
                for token in WATCHED_TOKENS:
                    price = await self.price_feed.get_token_price(token)
                    if price and price.price_mon > 0:
                        # Inject into orchestrator
                        await self.orchestrator.inject_prices({token: price.price_mon})
                    
                    # Small delay between tokens to avoid hammering API
                    await asyncio.sleep(0.5)
                        
            except Exception as e:
                # Only log real errors, not division by zero from empty prices
                if "division" not in str(e).lower():
                    print(f"❌ Price monitor error: {e}")
                
            await asyncio.sleep(self._price_rate_limit)
            
    async def _health_check_loop(self):
        """Periodic health checks"""
        while self.running:
            await asyncio.sleep(60)  # Every minute
            
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 🏥 Health Check")
            
            # Check isolation manager
            isolation = get_isolation_manager()
            status = isolation.get_all_health_status()
            
            for agent_id, health in status.items():
                errors = health["errors"]["count"]
                memory = health["memory"]["short_term"]
                emoji = "🟢" if errors == 0 else "🟡" if errors < 5 else "🔴"
                print(f"   {emoji} {agent_id}: mem={memory}, errors={errors}")
            
            # Check approved trades
            await self._check_swarm_approved()
                
    async def _on_price_update(self, data: dict):
        """Handle price update from CDN"""
        if self.orchestrator:
            await self.orchestrator.message_bus.publish(Message(
                type=MessageType.PRICE_UPDATE,
                sender="cdn",
                payload=data
            ))
            
    async def _on_whale_activity(self, whale_tx):
        """Handle whale activity from CDN"""
        if self.orchestrator:
            await self.orchestrator.inject_whale_alert(
                whale_tx.whale_address,
                whale_tx.amount_mon,
                whale_tx.token_address
            )
    
    async def _watch_signal_file(self):
        """Watch for signals from whale_follower"""
        while self.running:
            try:
                signals = safe_load_json(SWARM_SIGNAL_FILE, {"pending": []})
                
                for signal in signals.get("pending", []):
                    token = signal.get("token")
                    whale = signal.get("whale")
                    amount = signal.get("amount", 0)
                    
                    print(f"\n📨 Received signal from whale_follower: {token[:12]}...")
                    
                    # Inject into swarm for analysis
                    if self.orchestrator:
                        await self.orchestrator.inject_whale_alert(whale, amount, token)
                
                # Clear processed signals
                if signals.get("pending"):
                    safe_save_json(SWARM_SIGNAL_FILE, {"pending": [], "processed": signals.get("pending", [])})
                        
            except json.JSONDecodeError as e:
                print(f"❌ Signal file JSON error: {e}")
            except Exception as e:
                if "No such file" not in str(e):
                    print(f"❌ Signal watch error: {e}")
                
            await asyncio.sleep(2)
    
    async def _watch_sell_signals(self):
        """Watch for sell signals from Risk Agent and external sources"""
        from sell_executor import SellType
        
        while self.running:
            try:
                # 1. Check Risk Agent emergency signals
                if self.orchestrator:
                    risk_agent = self.orchestrator.agents.get("risk")
                    if risk_agent and hasattr(risk_agent, 'emergency_sells'):
                        for token_address in list(risk_agent.emergency_sells):
                            print(f"\n🚨 EMERGENCY SELL from Risk Agent: {token_address[:16]}...")
                            await self.sell_executor.queue_sell(
                                token_address=token_address,
                                sell_type=SellType.EMERGENCY,
                                reason="Risk Agent emergency signal"
                            )
                            risk_agent.emergency_sells.discard(token_address)
                
                # 2. Check sell signal file
                sell_signals = safe_load_json(SELL_SIGNAL_FILE, {"pending": [], "processed": []})
                
                if sell_signals.get("pending"):
                    for signal in sell_signals.get("pending", []):
                        token = signal.get("token")
                        sell_type_str = str(signal.get("type", "TP")).upper()
                        reason = signal.get("reason", "External signal")
                        percentage = signal.get("percentage", 100)
                        
                        # Map string to SellType
                        sell_type_map = {
                            "TP": SellType.TAKE_PROFIT,
                            "SL": SellType.STOP_LOSS,
                            "EMERGENCY": SellType.EMERGENCY,
                            "PARTIAL": SellType.PARTIAL,
                            "MANUAL": SellType.MANUAL
                        }
                        sell_type = sell_type_map.get(sell_type_str, SellType.MANUAL)
                        
                        if not token:
                            print("⚠️ Sell signal without token skipped")
                            continue
                        
                        print(f"\n📩 Sell signal from file: {str(token)[:16]}... ({sell_type.value})")
                        await self.sell_executor.queue_sell(
                            token_address=token,
                            sell_type=sell_type,
                            reason=reason,
                            percentage=percentage
                        )
                
                    # Clear processed signals atomically
                    safe_save_json(SELL_SIGNAL_FILE, {
                        "pending": [], 
                        "processed": sell_signals.get("pending", [])
                    })
                
                # 3. Auto TP/SL check based on current prices
                await self._auto_tp_sl_check()
                        
            except json.JSONDecodeError:
                pass
            except Exception as e:
                if "No such file" not in str(e):
                    print(f"❌ Sell signal watch error: {e}")
                
            await asyncio.sleep(3)
    
    async def _auto_tp_sl_check(self):
        """Auto check TP/SL for portfolio positions"""
        from sell_executor import SellType
        
        # Get portfolio positions
        portfolio_file = Path(__file__).parent.parent / "portfolio_state.json"
        portfolio = safe_load_json(portfolio_file, {"positions": []})
        
        # Check each position
        for position in portfolio.get("positions", []):
            token = position.get("token_address")
            if not token:
                continue
                
            entry_price = position.get("entry_price_mon", 0)
            if entry_price <= 0:
                continue
            
            # Get current price
            if self.price_feed:
                price_data = await self.price_feed.get_token_price(token)
                if not price_data or price_data.price_mon <= 0:
                    continue
                
                current_price = price_data.price_mon
                pnl_pct = ((current_price - entry_price) / entry_price) * 100
                
                # Take Profit: +50%
                if pnl_pct >= 50:
                    print(f"\n🎯 AUTO TP: {token[:16]}... +{pnl_pct:.1f}%")
                    await self.sell_executor.queue_sell(
                        token_address=token,
                        sell_type=SellType.TAKE_PROFIT,
                        reason=f"Auto TP at +{pnl_pct:.1f}%",
                        percentage=50  # Sell 50% at TP
                    )
                
                # Stop Loss: -30%
                elif pnl_pct <= -30:
                    print(f"\n🛑 AUTO SL: {token[:16]}... {pnl_pct:.1f}%")
                    await self.sell_executor.queue_sell(
                        token_address=token,
                        sell_type=SellType.STOP_LOSS,
                        reason=f"Auto SL at {pnl_pct:.1f}%",
                        percentage=100  # Full sell at SL
                    )
    
    async def _check_swarm_approved(self):
        """Check for swarm-approved trades and write them for whale_follower"""
        if not self.orchestrator:
            return
            
        # Get trader agent pending trades with consensus
        trader = self.orchestrator.agents.get("trader")
        if not trader:
            return
            
        # Check for any approved trades
        approved_tokens = []
        for token, trade_data in list(trader.pending_trades.items()):
            votes = trade_data.get("votes", {})
            approvals = sum(1 for v in votes.values() if v)
            
            if approvals >= 2:  # Consensus reached
                approved_tokens.append({
                    "token": token,
                    "amount": trade_data.get("analysis", {}).get("suggested_amount_mon", 10),
                    "analysis": trade_data.get("analysis", {}),
                    "timestamp": datetime.now().isoformat()
                })
                
        # Write approved trades for whale_follower to execute
        if approved_tokens:
            if safe_save_json(SWARM_APPROVED_FILE, {"approved": approved_tokens}):
                print(f"\n✅ Swarm approved {len(approved_tokens)} trades!")
            else:
                print(f"❌ Error writing approved trades")
            
    async def _print_status(self):
        """Print system status"""
        print("\n📊 System Status:")
        print(f"   Watched Whales: {len(WATCHED_WHALES)}")
        print(f"   Watched Tokens: {len(WATCHED_TOKENS)}")
        print(f"   Active Agents: 4 (Scanner, Analyst, Trader, Risk)")
        print(f"   Isolation: HARD/STRICT")
        print(f"   Message Bus: In-Memory")
        print(f"   Signal File: {SWARM_SIGNAL_FILE}")
        print(f"   Approved File: {SWARM_APPROVED_FILE}")
        print(f"   Sell Signal File: {SELL_SIGNAL_FILE}")
        print("\n💰 Sell Executor Settings:")
        print(f"   Take Profit: +50% → sell 50%")
        print(f"   Stop Loss: -30% → sell 100%")
        print(f"   Emergency: immediate execution")
        print(f"   Queue: priority-based (emergency > SL > TP > manual)")


def setup_signal_handlers(launcher: AgentSwarmLauncher):
    """Setup graceful shutdown"""
    def signal_handler(sig, frame):
        print("\n\n⚠️ Received shutdown signal...")
        asyncio.create_task(launcher.stop())
        
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)


async def main():
    """Main entry point"""
    launcher = AgentSwarmLauncher()
    setup_signal_handlers(launcher)
    await launcher.run()


if __name__ == "__main__":
    asyncio.run(main())
