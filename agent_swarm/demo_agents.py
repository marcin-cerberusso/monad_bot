#!/usr/bin/env python3
"""
🎬 DEMO - Pokazuje jak agenci komunikują się przez Dragonfly Message Bus

Uruchamia 3 agentów:
1. 🔍 Scanner - wykrywa whale'a i wysyła WHALE_ALERT
2. 📊 Analyst - odbiera alert, analizuje, wysyła ANALYSIS_RESULT  
3. 💰 Trader - odbiera analizę, prosi o CONSENSUS, wykonuje TRADE

Flow:
Scanner → WHALE_ALERT → Analyst → ANALYSIS_RESULT → Trader → CONSENSUS → TRADE_EXECUTED
"""

import asyncio
import signal
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_swarm.message_bus import MessageBus, get_bus, shutdown_all
from agent_swarm.message_types import (
    Message, MessageType, Priority, TradeAction, RiskLevel,
    WhaleAlertPayload, AnalysisResultPayload, TradeSignalPayload,
    TradeExecutedPayload, ConsensusRequestPayload,
    MessageBuilder
)


class ScannerAgent:
    """🔍 Scanner - wykrywa whale activity"""
    
    def __init__(self):
        self.bus: MessageBus = None
        self.running = False
        
    async def start(self):
        self.bus = await get_bus("scanner")
        await self.bus.subscribe("all")
        self.running = True
        print("🔍 [Scanner] Started - monitoring whales...")
        
    async def simulate_whale_detection(self):
        """Symulacja wykrycia whale'a"""
        await asyncio.sleep(2)  # Czekaj chwilę
        
        print("\n" + "="*60)
        print("🐋 [Scanner] WHALE DETECTED!")
        print("="*60)
        
        # Wyślij whale alert
        await self.bus.signal_whale_alert(
            whale="0x37556b2c49bebf840f2bec6e3c066fb93aee7f9e",
            token="0xABCDEF1234567890ABCDEF1234567890ABCDEF12",
            action="buy",
            amount=15000.0,
            whale_name="CryptoWhale_42",
            token_name="MONADMEME"
        )
        
        print("📤 [Scanner] Sent WHALE_ALERT to all agents")


class AnalystAgent:
    """📊 Analyst - analizuje tokeny"""
    
    def __init__(self):
        self.bus: MessageBus = None
        self.running = False
        
    async def start(self):
        self.bus = await get_bus("analyst")
        await self.bus.subscribe("all", "analyst")
        
        # Handler dla whale alerts
        @self.bus.on(MessageType.WHALE_ALERT)
        async def on_whale_alert(msg: Message):
            await self._analyze_whale_alert(msg)
            
        self.running = True
        print("📊 [Analyst] Started - ready to analyze...")
        
        # Start listening in background
        asyncio.create_task(self.bus.listen())
        
    async def _analyze_whale_alert(self, msg: Message):
        """Analizuj whale alert"""
        payload = msg.payload
        token = payload.get("token_address", "")
        whale = payload.get("whale_name", "unknown")
        amount = payload.get("amount_mon", 0)
        
        print(f"\n📊 [Analyst] Received WHALE_ALERT from {msg.sender}")
        print(f"   🐋 Whale: {whale}")
        print(f"   💰 Amount: {amount} MON")
        print(f"   🔍 Analyzing token {token[:16]}...")
        
        # Symulacja analizy (2 sekundy)
        await asyncio.sleep(2)
        
        # Wysoka jakość bo whale kupił dużo
        confidence = 0.85 if amount > 10000 else 0.65
        recommendation = "buy" if confidence > 0.7 else "hold"
        
        print(f"   ✅ Analysis complete: {recommendation.upper()} (confidence: {confidence:.0%})")
        
        # Wyślij wynik
        result = AnalysisResultPayload(
            token_address=token,
            recommendation=recommendation,
            confidence=confidence,
            reasons=[
                f"Whale {whale} bought {amount} MON",
                "Token has good liquidity",
                "Positive momentum detected"
            ]
        )
        
        await self.bus.broadcast(MessageBuilder.analysis_result(
            self.bus.agent_name, "trader", result
        ))
        
        print("📤 [Analyst] Sent ANALYSIS_RESULT to Trader")


class TraderAgent:
    """💰 Trader - wykonuje transakcje"""
    
    def __init__(self):
        self.bus: MessageBus = None
        self.running = False
        
    async def start(self):
        self.bus = await get_bus("trader")
        await self.bus.subscribe("all", "trader")
        
        # Handler dla analysis results
        @self.bus.on(MessageType.ANALYSIS_RESULT)
        async def on_analysis(msg: Message):
            await self._handle_analysis(msg)
            
        # Handler dla consensus requests (jako voter)
        @self.bus.on(MessageType.CONSENSUS_REQUEST)
        async def on_consensus(msg: Message):
            # Auto-approve jeśli to nie nasze zapytanie
            if msg.sender != self.bus.agent_name:
                await self.bus.vote(msg.id, "approve", "analyst recommended")
            
        self.running = True
        print("💰 [Trader] Started - ready to trade...")
        
        # Start listening
        asyncio.create_task(self.bus.listen())
        
    async def _handle_analysis(self, msg: Message):
        """Obsłuż wynik analizy"""
        payload = msg.payload
        token = payload.get("token_address", "")
        recommendation = payload.get("recommendation", "hold")
        confidence = payload.get("confidence", 0)
        reasons = payload.get("reasons", [])
        
        print(f"\n💰 [Trader] Received ANALYSIS_RESULT from {msg.sender}")
        print(f"   📊 Recommendation: {recommendation.upper()}")
        print(f"   🎯 Confidence: {confidence:.0%}")
        print(f"   📝 Reasons: {', '.join(reasons[:2])}")
        
        if recommendation != "buy" or confidence < 0.7:
            print("   ⏸️ Skipping - confidence too low")
            return
            
        # Oblicz kwotę
        amount = 10.0 * confidence  # Max 10 MON przy 100% confidence
        
        print(f"\n🗳️ [Trader] Requesting CONSENSUS for {amount:.2f} MON buy...")
        
        # Daj czas na rozpropagowanie
        await asyncio.sleep(0.5)
        
        # Poproś o consensus
        approved = await self.bus.request_consensus(ConsensusRequestPayload(
            action="buy",
            token_address=token,
            token_name="MONADMEME",
            amount_mon=amount,
            reason=f"Whale buy signal, confidence {confidence:.0%}",
            min_approvals=1,  # Demo - potrzebujemy tylko 1 głos
            timeout_seconds=8.0  # Więcej czasu na głosy
        ))
        
        if approved:
            print("\n✅ [Trader] CONSENSUS APPROVED!")
            print(f"   🚀 Executing BUY: {amount:.2f} MON")
            
            # Symulacja wykonania transakcji
            await asyncio.sleep(1)
            
            # Wyślij TRADE_EXECUTED
            executed = TradeExecutedPayload(
                action=TradeAction.BUY,
                token_address=token,
                token_name="MONADMEME",
                amount_mon=amount,
                tx_hash="0x123abc456def789...DEMO_TX_HASH",
                success=True
            )
            
            await self.bus.broadcast(Message(
                type=MessageType.TRADE_EXECUTED,
                sender=self.bus.agent_name,
                payload=executed.to_dict(),
                priority=Priority.HIGH
            ))
            
            print("   ✅ TRADE EXECUTED!")
            print(f"   📜 TX: 0x123abc456def789...DEMO")
        else:
            print("\n❌ [Trader] CONSENSUS REJECTED - trade cancelled")


class RiskAgent:
    """🛡️ Risk - monitoruje i głosuje"""
    
    def __init__(self):
        self.bus: MessageBus = None
        
    async def start(self):
        self.bus = await get_bus("risk")
        await self.bus.subscribe("all", "risk")
        
        @self.bus.on(MessageType.CONSENSUS_REQUEST)
        async def on_consensus(msg: Message):
            payload = msg.payload
            amount = payload.get("amount_mon", 0)
            
            print(f"\n🛡️ [Risk] Reviewing trade request: {amount} MON")
            
            # Prosta logika ryzyka
            if amount > 50:
                await self.bus.vote(msg.id, "reject", "amount too high")
                print("   ❌ REJECTED - amount too high")
            else:
                await self.bus.vote(msg.id, "approve", "risk acceptable")
                print("   ✅ APPROVED - risk acceptable")
                
        @self.bus.on(MessageType.TRADE_EXECUTED)
        async def on_trade(msg: Message):
            payload = msg.payload
            success = payload.get("success", False)
            amount = payload.get("amount_mon", 0)
            
            emoji = "✅" if success else "❌"
            print(f"\n🛡️ [Risk] Trade notification: {emoji} {amount} MON")
        
        print("🛡️ [Risk] Started - monitoring risk...")
        asyncio.create_task(self.bus.listen())


async def run_demo():
    """Uruchom demo wszystkich agentów"""
    print("\n" + "="*70)
    print("🎬 AGENT SWARM DEMO - Inter-Agent Communication")
    print("="*70)
    print(f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()
    
    # Utwórz agentów
    scanner = ScannerAgent()
    analyst = AnalystAgent()
    trader = TraderAgent()
    risk = RiskAgent()
    
    # Uruchom wszystkich
    print("🚀 Starting agents...\n")
    await scanner.start()
    await analyst.start()
    await trader.start()
    await risk.start()
    
    print("\n" + "-"*70)
    print("📡 All agents connected to Dragonfly Message Bus")
    print("-"*70)
    
    # Daj czas na setup
    await asyncio.sleep(1)
    
    # Symuluj wykrycie whale'a
    await scanner.simulate_whale_detection()
    
    # Czekaj na przetworzenie
    print("\n⏳ Processing...")
    await asyncio.sleep(10)
    
    # Podsumowanie
    print("\n" + "="*70)
    print("📊 DEMO COMPLETE - Message Flow Summary")
    print("="*70)
    print("""
    1. 🔍 Scanner detected whale buying 15,000 MON
    2. 📤 Scanner sent WHALE_ALERT to Message Bus
    3. 📊 Analyst received alert, analyzed token
    4. 📤 Analyst sent ANALYSIS_RESULT (BUY, 85% confidence)
    5. 💰 Trader received analysis, requested CONSENSUS
    6. 🛡️ Risk agent voted APPROVE (amount < 50 MON)
    7. ✅ Consensus reached, trade executed
    8. 📢 TRADE_EXECUTED broadcast to all agents
    """)
    
    # Cleanup
    await shutdown_all()
    print("🏁 Demo finished!\n")


async def main():
    loop = asyncio.get_event_loop()
    
    def shutdown():
        print("\n🛑 Shutting down...")
        asyncio.create_task(shutdown_all())
        
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown)
        
    await run_demo()


if __name__ == "__main__":
    asyncio.run(main())
