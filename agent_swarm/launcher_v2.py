#!/usr/bin/env python3
"""
🚀 AGENT SWARM LAUNCHER V2 - Z pełną integracją Dragonfly Message Bus

Startuje:
1. CDN Price Feed
2. Message Bus (Dragonfly)
3. Orchestrator V2
4. Sell Executor V2
5. Integration Bridge

Różnice vs V1:
- Dragonfly zamiast in-memory
- Prawdziwa komunikacja async między agentami
- Centralne routing wiadomości
"""

import asyncio
import json
import os
import signal
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

# Import components
from agent_swarm.message_bus import MessageBus, get_bus, shutdown_all
from agent_swarm.message_types import Message, MessageType, Priority
from agent_swarm.cdn_price_feed import PriceFeed, get_price_feed
from agent_swarm.config_validator import validate_and_exit_on_error, ConfigStatus
from file_utils import safe_load_json, safe_save_json


# === CONFIG ===

WATCHED_TOKENS = [
    "0x5E1b1A14c8758104B8560514e94ab8320e587777",  # MonadMeme
]

SWARM_SIGNAL_FILE = Path(__file__).parent.parent / "swarm_signals.json"
SELL_SIGNAL_FILE = Path(__file__).parent.parent / "sell_signals.json"


class AgentSwarmLauncherV2:
    """Main launcher with Dragonfly Message Bus"""
    
    def __init__(self):
        self.bus: Optional[MessageBus] = None
        self.price_feed: Optional[PriceFeed] = None
        self.running = False
        self.config_status: Optional[ConfigStatus] = None
        
        # Subprocess handles for Rust binaries
        self.subprocesses = []
        
        # Rate limiting
        self._price_rate_limit = 5.0
        self._last_price_fetch = 0.0
        
    async def start(self):
        """Start everything"""
        print("=" * 70)
        print("🐝 AGENT SWARM LAUNCHER V2")
        print("   Multi-Agent Trading System with Dragonfly Message Bus")
        print("=" * 70)
        print(f"\n📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        
        # 0. Validate config (fail-fast)
        print("\n🔐 Validating configuration...")
        self.config_status = await validate_and_exit_on_error(
            require_trading=True,
            require_ai=False  # AI optional
        )
        print(f"   ✅ Config valid, chain: {self.config_status.chain_name}")
        
        # 1. Connect to Dragonfly
        print("\n🐉 Connecting to Dragonfly Message Bus...")
        self.bus = await get_bus("launcher")
        if self.bus.connected:
            print("   ✅ Dragonfly connected")
        else:
            print("   ⚠️ Dragonfly unavailable, using in-memory bus")
        
        # Subscribe to relevant channels
        await self.bus.subscribe("all", "trader")
        
        # 2. Start CDN Price Feed
        print("\n📡 Starting CDN Price Feed...")
        self.price_feed = get_price_feed()
        await self.price_feed.start()
        self.price_feed.on_price_update(self._on_price_update)
        self.price_feed.on_whale_activity(self._on_whale_activity)
        print("   ✅ Price feed active")
        
        self.running = True
        
        # 3. Start background tasks
        print("\n🔄 Starting background tasks...")
        tasks = [
            asyncio.create_task(self._price_monitor_loop()),
            asyncio.create_task(self._health_check_loop()),
            asyncio.create_task(self._watch_signal_file()),
            asyncio.create_task(self._watch_sell_signals()),
            asyncio.create_task(self.bus.listen()),
        ]
        
        print("\n" + "=" * 70)
        print("✅ AGENT SWARM V2 FULLY OPERATIONAL")
        print("=" * 70)
        
        # Show status
        await self._print_status()
        
        # Announce startup on bus
        await self.bus.send_heartbeat("running", "all systems go")
        
        # Wait for tasks
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            print("Tasks cancelled")
            
    async def stop(self):
        """Stop everything"""
        print("\n🛑 Shutting down Agent Swarm V2...")
        self.running = False
        
        # Notify shutdown
        if self.bus and self.bus.connected:
            await self.bus.send_heartbeat("stopping", "shutdown")
        
        # Stop price feed
        if self.price_feed:
            await self.price_feed.stop()
            
        # Kill subprocesses
        for proc in self.subprocesses:
            proc.terminate()
            
        # Disconnect all buses
        await shutdown_all()
        
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
        """Monitor prices with rate limiting"""
        import time
        
        while self.running:
            try:
                now = time.time()
                if now - self._last_price_fetch < self._price_rate_limit:
                    await asyncio.sleep(self._price_rate_limit - (now - self._last_price_fetch))
                    
                self._last_price_fetch = time.time()
                
                for token in WATCHED_TOKENS:
                    try:
                        price = await self.price_feed.get_token_price(token)
                        if price and price.price_mon > 0:
                            # Publish price update to bus
                            from agent_swarm.message_types import PriceUpdatePayload, MessageBuilder
                            payload = PriceUpdatePayload(
                                token_address=token,
                                token_name=token[:12],
                                price_mon=price.price_mon,
                                volume_24h=getattr(price, 'volume_24h', 0),
                                change_24h=getattr(price, 'change_24h', 0),
                                liquidity=getattr(price, 'liquidity', 0)
                            )
                            if self.bus:
                                await self.bus.broadcast(MessageBuilder.price_update(
                                    self.bus.agent_name, payload
                                ))
                    except Exception as e:
                        if "division" not in str(e).lower():
                            print(f"❌ Price fetch error for {token[:12]}: {e}")
                            
                    await asyncio.sleep(0.5)
                    
            except Exception as e:
                if "division" not in str(e).lower():
                    print(f"❌ Price monitor error: {e}")
                    
            await asyncio.sleep(self._price_rate_limit)
            
    async def _health_check_loop(self):
        """Periodic health checks"""
        while self.running:
            await asyncio.sleep(60)
            
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 🏥 Health Check")
            
            # Check Dragonfly connection
            if self.bus:
                status = "🟢 connected" if self.bus.connected else "🟡 in-memory"
                print(f"   Message Bus: {status}")
                print(f"   Stats: sent={self.bus.stats['messages_sent']}, "
                      f"recv={self.bus.stats['messages_received']}")
                      
            # Check price feed
            if self.price_feed:
                pf_status = "🟢 active" if self.price_feed.running else "🔴 stopped"
                print(f"   Price Feed: {pf_status}")
                
    async def _on_price_update(self, data: dict):
        """Handle price update from CDN"""
        if self.bus:
            from agent_swarm.message_types import PriceUpdatePayload, MessageBuilder
            payload = PriceUpdatePayload(
                token_address=data.get("token", ""),
                token_name=data.get("name", data.get("token", "")[:12]),
                price_mon=data.get("price", 0),
                volume_24h=data.get("volume", 0),
                change_24h=data.get("change", 0)
            )
            await self.bus.broadcast(MessageBuilder.price_update("cdn", payload))
            
    async def _on_whale_activity(self, whale_tx):
        """Handle whale activity from CDN"""
        if self.bus:
            await self.bus.signal_whale_alert(
                whale=whale_tx.whale_address,
                token=whale_tx.token_address,
                action="buy" if whale_tx.amount_mon > 0 else "sell",
                amount=abs(whale_tx.amount_mon)
            )
            
    async def _watch_signal_file(self):
        """Watch for signals from whale_follower (legacy file-based)"""
        while self.running:
            try:
                signals = safe_load_json(SWARM_SIGNAL_FILE, {"pending": []})
                
                for signal in signals.get("pending", []):
                    token = signal.get("token")
                    whale = signal.get("whale")
                    amount = signal.get("amount", 0)
                    
                    print(f"\n📨 Signal from whale_follower: {token[:12]}...")
                    
                    # Publish to bus
                    if self.bus:
                        await self.bus.signal_whale_alert(
                            whale=whale,
                            token=token,
                            action="buy",
                            amount=amount
                        )
                        
                # Clear processed
                if signals.get("pending"):
                    safe_save_json(SWARM_SIGNAL_FILE, {
                        "pending": [],
                        "processed": signals.get("pending", []),
                        "processed_at": datetime.now().isoformat()
                    })
                    
            except json.JSONDecodeError as e:
                print(f"❌ Signal file JSON error: {e}")
            except Exception as e:
                if "No such file" not in str(e):
                    print(f"❌ Signal watch error: {e}")
                    
            await asyncio.sleep(2)
            
    async def _watch_sell_signals(self):
        """Watch for sell signals (legacy file-based)"""
        while self.running:
            try:
                sell_signals = safe_load_json(SELL_SIGNAL_FILE, {"pending": []})
                
                for signal in sell_signals.get("pending", []):
                    token = signal.get("token")
                    sell_type = signal.get("type", "TP")
                    reason = signal.get("reason", "External signal")
                    percent = signal.get("percentage", 100)
                    
                    if not token:
                        continue
                        
                    print(f"\n💰 Sell signal: {token[:12]} {percent}% ({sell_type})")
                    
                    # Publish to bus
                    if self.bus:
                        await self.bus.signal_trade(
                            action="sell",
                            token=token,
                            percent=percent,
                            reason=f"{sell_type}: {reason}"
                        )
                        
                # Clear processed
                if sell_signals.get("pending"):
                    safe_save_json(SELL_SIGNAL_FILE, {
                        "pending": [],
                        "processed": sell_signals.get("pending", []),
                        "processed_at": datetime.now().isoformat()
                    })
                    
            except Exception as e:
                if "No such file" not in str(e):
                    print(f"❌ Sell signal watch error: {e}")
                    
            await asyncio.sleep(2)
            
    async def _print_status(self):
        """Print system status"""
        print("\n📊 SYSTEM STATUS:")
        print(f"   🐉 Dragonfly: {'connected' if (self.bus and self.bus.connected) else 'in-memory'}")
        print(f"   📡 Price Feed: {'active' if (self.price_feed and self.price_feed.running) else 'inactive'}")
        print(f"   🔗 Chain: {self.config_status.chain_name if self.config_status else 'unknown'}")
        print(f"   📁 Signal Files:")
        print(f"      - {SWARM_SIGNAL_FILE}")
        print(f"      - {SELL_SIGNAL_FILE}")
        print()


# === MAIN ===

async def main():
    launcher = AgentSwarmLauncherV2()
    
    loop = asyncio.get_event_loop()
    
    def shutdown_handler():
        print("\n🛑 Shutdown signal received")
        asyncio.create_task(launcher.stop())
        
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown_handler)
        
    try:
        await launcher.run()
    except KeyboardInterrupt:
        await launcher.stop()


if __name__ == "__main__":
    asyncio.run(main())
