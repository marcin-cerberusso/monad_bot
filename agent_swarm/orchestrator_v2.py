#!/usr/bin/env python3
"""
🎭 ORCHESTRATOR V2 - Orchestrator z pełną integracją Dragonfly Message Bus

Koordynuje:
- Scanner → wykrywa nowe tokeny i whale activity
- Analyst → analizuje tokeny (DeepSeek, TA)
- Trader → wykonuje trade'y
- Risk → monitoruje ryzyko

Flow:
1. Scanner/WhaleFollower → WHALE_ALERT / NEW_TOKEN
2. Orchestrator → ANALYSIS_REQUEST → Analyst
3. Analyst → ANALYSIS_RESULT → Orchestrator
4. Orchestrator → CONSENSUS_REQUEST (jeśli potrzeba)
5. Votes → Orchestrator
6. Orchestrator → TRADE_SIGNAL → Trader
7. Trader → TRADE_EXECUTED → Orchestrator
"""

import asyncio
import json
import os
import signal
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set

from dotenv import load_dotenv

load_dotenv()

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_swarm.message_bus import MessageBus, get_bus
from agent_swarm.message_types import (
    Message, MessageType, Priority, TradeAction, RiskLevel,
    WhaleAlertPayload, NewTokenPayload, AnalysisRequestPayload,
    AnalysisResultPayload, TradeSignalPayload, TradeExecutedPayload,
    ConsensusRequestPayload, RiskAlertPayload,
    MessageBuilder
)

# Import istniejących komponentów
DEEPSEEK_AVAILABLE = False
analyze_token_with_deepseek = None  # Placeholder

try:
    from smart_entry_detector import SmartEntryDetector
    ENTRY_DETECTOR_AVAILABLE = True
except ImportError:
    ENTRY_DETECTOR_AVAILABLE = False
    print("⚠️ Smart Entry Detector not available")


# === CONFIG ===

WHALE_MIN_AMOUNT = float(os.getenv("WHALE_MIN_AMOUNT", "5000"))  # Min MON dla whale alert
ANALYSIS_TIMEOUT = float(os.getenv("ANALYSIS_TIMEOUT", "30"))  # Timeout analizy
CONSENSUS_MIN_APPROVALS = int(os.getenv("CONSENSUS_MIN_APPROVALS", "2"))
CONSENSUS_TIMEOUT = float(os.getenv("CONSENSUS_TIMEOUT", "10"))

# High value trade threshold - wymaga consensus
HIGH_VALUE_THRESHOLD = float(os.getenv("HIGH_VALUE_THRESHOLD", "500"))  # MON


@dataclass
class TokenState:
    """Stan tokena w pipeline"""
    token_address: str
    token_name: str
    discovered_at: datetime
    source: str  # whale, scanner, manual
    whale_address: Optional[str] = None
    analysis_pending: bool = False
    analysis_result: Optional[Dict] = None
    quality_score: int = 0
    risk_level: str = "medium"
    trade_sent: bool = False
    trade_executed: bool = False
    

class OrchestratorV2:
    """
    Orchestrator z pełną integracją Message Bus
    """
    
    bus: MessageBus  # Type hint - set in start()
    
    def __init__(self):
        self._bus: Optional[MessageBus] = None
        self.running = False
        
        # Token pipeline
        self.tokens: Dict[str, TokenState] = {}
        
        # Cooldowns
        self.token_cooldown: Dict[str, datetime] = {}
        self.COOLDOWN_SECONDS = 300  # 5 min
        
        # Stats
        self.stats = {
            "whale_alerts_received": 0,
            "new_tokens_received": 0,
            "analyses_requested": 0,
            "analyses_completed": 0,
            "trades_signaled": 0,
            "trades_executed": 0,
            "consensus_requests": 0,
            "risk_alerts": 0
        }
        
        # Config
        self.config = self._load_config()
        
    def _load_config(self) -> Dict:
        """Załaduj config"""
        config_path = Path(__file__).parent.parent / "config.json"
        if config_path.exists():
            with open(config_path) as f:
                return json.load(f)
        return {}
        
    async def start(self):
        """Uruchom orchestrator"""
        print("🎭 Orchestrator V2 starting...")
        
        # Utwórz bus
        self.bus = await get_bus("orchestrator")
        
        # Subskrybuj kanały
        await self.bus.subscribe("all", "consensus")
        
        # Zarejestruj handlery
        self._register_handlers()
        
        # Start listening
        self.running = True
        
        # Background tasks
        tasks = [
            asyncio.create_task(self.bus.listen()),
            asyncio.create_task(self._heartbeat_loop()),
            asyncio.create_task(self._cleanup_loop()),
        ]
        
        print("🎭 Orchestrator V2 running!")
        
        # Wyślij initial heartbeat
        await self.bus.send_heartbeat("starting", "initialization")
        
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            print("🎭 Orchestrator V2 stopping...")
            
    async def stop(self):
        """Zatrzymaj"""
        self.running = False
        if self.bus:
            await self.bus.send_heartbeat("stopping", "shutdown")
            await self.bus.disconnect()
            
    def _register_handlers(self):
        """Zarejestruj handlery wiadomości"""
        if not self.bus:
            raise RuntimeError("Bus not initialized")
        
        @self.bus.on(MessageType.WHALE_ALERT)
        async def handle_whale(msg: Message):
            await self._handle_whale_alert(msg)
            
        @self.bus.on(MessageType.NEW_TOKEN)
        async def handle_new_token(msg: Message):
            await self._handle_new_token(msg)
            
        @self.bus.on(MessageType.ANALYSIS_RESULT)
        async def handle_analysis(msg: Message):
            await self._handle_analysis_result(msg)
            
        @self.bus.on(MessageType.TRADE_EXECUTED)
        async def handle_trade_executed(msg: Message):
            await self._handle_trade_executed(msg)
            
        @self.bus.on(MessageType.RISK_ALERT)
        async def handle_risk(msg: Message):
            await self._handle_risk_alert(msg)
            
        @self.bus.on(MessageType.CONSENSUS_REQUEST)
        async def handle_consensus_request(msg: Message):
            await self._handle_consensus_request(msg)
            
        @self.bus.on(MessageType.SYSTEM_STATUS)
        async def handle_status(msg: Message):
            print(f"📊 Status from {msg.sender}: {msg.payload}")
            
    # === HANDLERS ===
    
    async def _handle_whale_alert(self, msg: Message):
        """Obsłuż whale alert"""
        self.stats["whale_alerts_received"] += 1
        payload = msg.payload
        
        token = payload.get("token_address", "")
        whale = payload.get("whale_address", "")
        amount = payload.get("amount_mon", 0)
        action = payload.get("action", "buy")
        
        print(f"🐋 Whale Alert: {whale[:10]} {action} {amount:.2f} MON of {token[:16]}")
        
        # Sprawdź cooldown
        if self._is_on_cooldown(token):
            print(f"⏸️ Token {token[:16]} on cooldown, skipping")
            return
            
        # Sprawdź min amount
        if amount < WHALE_MIN_AMOUNT:
            print(f"📉 Amount {amount:.2f} below threshold {WHALE_MIN_AMOUNT}, skipping")
            return
            
        # Dodaj do pipeline
        if token not in self.tokens:
            self.tokens[token] = TokenState(
                token_address=token,
                token_name=payload.get("token_name", token[:12]),
                discovered_at=datetime.now(),
                source="whale",
                whale_address=whale
            )
            
        # Request analysis
        if action.lower() == "buy":
            await self._request_analysis(token, source="whale_alert")
            
    async def _handle_new_token(self, msg: Message):
        """Obsłuż new token"""
        self.stats["new_tokens_received"] += 1
        payload = msg.payload
        
        token = payload.get("token_address", "")
        name = payload.get("token_name", "")
        score = payload.get("quality_score", 0)
        
        print(f"🆕 New Token: {name} ({token[:16]}) score={score}")
        
        # Sprawdź cooldown
        if self._is_on_cooldown(token):
            return
            
        # Dodaj do pipeline
        if token not in self.tokens:
            self.tokens[token] = TokenState(
                token_address=token,
                token_name=name,
                discovered_at=datetime.now(),
                source="scanner",
                quality_score=score
            )
            
        # Jeśli dobry score, request analysis
        if score >= 60:
            await self._request_analysis(token, source="scanner")
            
    async def _handle_analysis_result(self, msg: Message):
        """Obsłuż wynik analizy"""
        self.stats["analyses_completed"] += 1
        payload = msg.payload
        
        token = payload.get("token_address", "")
        recommendation = payload.get("recommendation", "hold")
        confidence = payload.get("confidence", 0.5)
        score = int(confidence * 100)  # Convert 0-1 to 0-100
        reasons = payload.get("reasons", [])
        
        print(f"📊 Analysis Result: {token[:16]} → {recommendation} (confidence={confidence:.2f})")
        
        if token in self.tokens:
            self.tokens[token].analysis_pending = False
            self.tokens[token].analysis_result = payload
            self.tokens[token].quality_score = score
            
        # Decyzja
        if recommendation == "buy" and score >= 70:
            await self._process_buy_decision(token, payload)
        elif recommendation == "sell":
            await self._process_sell_decision(token, payload)
            
    async def _handle_trade_executed(self, msg: Message):
        """Obsłuż wykonany trade"""
        self.stats["trades_executed"] += 1
        payload = msg.payload
        
        token = payload.get("token_address", "")
        action = payload.get("action", "")
        success = payload.get("success", False)
        tx_hash = payload.get("tx_hash", "")
        
        status = "✅" if success else "❌"
        print(f"{status} Trade Executed: {action} {token[:16]} tx={tx_hash[:20]}...")
        
        if token in self.tokens:
            self.tokens[token].trade_executed = success
            
        # Set cooldown
        if success:
            self.token_cooldown[token] = datetime.now()
            
    async def _handle_risk_alert(self, msg: Message):
        """Obsłuż risk alert"""
        self.stats["risk_alerts"] += 1
        payload = msg.payload
        
        level = payload.get("level", "medium")
        message = payload.get("message", "")
        token = payload.get("token_address", "")
        action = payload.get("suggested_action", "")
        
        print(f"⚠️ Risk Alert [{level}]: {message}")
        
        # Dla critical - natychmiastowe działanie
        if level == "critical":
            if action == "stop_trading":
                print("🛑 CRITICAL: Stopping all trading!")
                # TODO: implementacja stop
            elif action == "sell_all":
                print("🚨 CRITICAL: Triggering emergency sell!")
                await self._trigger_emergency_sell()
                
    async def _handle_consensus_request(self, msg: Message):
        """Obsłuż consensus request (jako voter)"""
        payload = msg.payload
        request_id = msg.id
        action = payload.get("action", "")
        token = payload.get("token_address", "")
        
        print(f"🗳️ Consensus Request: {action} {token[:16]}")
        
        # Orchestrator głosuje na podstawie swojej wiedzy
        if token in self.tokens:
            state = self.tokens[token]
            if state.analysis_result:
                score = state.analysis_result.get("confidence_score", 0)
                if score >= 70:
                    await self.bus.vote(request_id, "approve", f"score={score}")
                elif score < 40:
                    await self.bus.vote(request_id, "reject", f"low score={score}")
                else:
                    await self.bus.vote(request_id, "abstain", f"uncertain score={score}")
            else:
                await self.bus.vote(request_id, "abstain", "no analysis")
        else:
            await self.bus.vote(request_id, "abstain", "unknown token")
            
    # === ACTIONS ===
    
    async def _request_analysis(self, token: str, source: str = ""):
        """Poproś o analizę tokena"""
        if token in self.tokens and self.tokens[token].analysis_pending:
            print(f"⏳ Analysis already pending for {token[:16]}")
            return
            
        self.stats["analyses_requested"] += 1
        
        if token in self.tokens:
            self.tokens[token].analysis_pending = True
            
        print(f"📤 Requesting analysis for {token[:16]} (source: {source})")
        
        # Wyślij request
        payload = AnalysisRequestPayload(
            token_address=token,
            analysis_type="quick",
            source=source
        )
        if self.bus:
            await self.bus.broadcast(MessageBuilder.analysis_request(
                self.bus.agent_name, "analyst", payload
            ))
        
        # Jeśli mamy DeepSeek lokalnie, zrób analizę
        # DeepSeek is disabled - analysis comes from external sources
        # if DEEPSEEK_AVAILABLE:
        #     asyncio.create_task(self._local_analysis(token))
            
    async def _local_analysis(self, token: str):
        """Lokalna analiza DeepSeek - currently disabled"""
        # This would be enabled if we have analyze_token_with_deepseek
        if not DEEPSEEK_AVAILABLE or analyze_token_with_deepseek is None:
            print(f"⚠️ DeepSeek not available for {token[:16]}")
            return
            
        try:
            result = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(
                    None, analyze_token_with_deepseek, token
                ),
                timeout=ANALYSIS_TIMEOUT
            )
            
            if result and self.bus:
                # Wyślij wynik na bus
                payload = AnalysisResultPayload(
                    token_address=token,
                    recommendation=result.get("decision", "hold"),
                    confidence=result.get("score", 50) / 100.0,  # normalize to 0-1
                    reasons=[result.get("reasoning", "DeepSeek analysis")]
                )
                await self.bus.broadcast(Message(
                    type=MessageType.ANALYSIS_RESULT,
                    sender="orchestrator:deepseek",
                    payload=payload.to_dict(),
                    priority=Priority.HIGH
                ))
        except asyncio.TimeoutError:
            print(f"⏰ DeepSeek analysis timeout for {token[:16]}")
        except Exception as e:
            print(f"❌ DeepSeek analysis error: {e}")
            
    async def _process_buy_decision(self, token: str, analysis: Dict):
        """Przetwórz decyzję kupna"""
        confidence = analysis.get("confidence", 0.5)
        score = int(confidence * 100)
        amount = self._calculate_position_size(score)
        
        print(f"💰 Buy decision: {token[:16]} amount={amount:.2f} MON")
        
        # Duże pozycje wymagają consensus
        requires_consensus = amount >= HIGH_VALUE_THRESHOLD
        
        if requires_consensus:
            self.stats["consensus_requests"] += 1
            token_name = self.tokens[token].token_name if token in self.tokens else token[:12]
            approved = await self.bus.request_consensus(ConsensusRequestPayload(
                action="buy",
                token_address=token,
                token_name=token_name,
                amount_mon=amount,
                reason=f"confidence={confidence:.2f}, amount={amount:.2f}",
                min_approvals=CONSENSUS_MIN_APPROVALS,
                timeout_seconds=CONSENSUS_TIMEOUT
            ))
            
            if not approved:
                print(f"❌ Consensus rejected for {token[:16]}")
                return
                
        # Wyślij trade signal
        reasons = analysis.get("reasons", [])
        reason_str = "; ".join(reasons) if reasons else "analysis"
        await self._send_trade_signal("buy", token, amount, reason=reason_str)
                                      
    async def _process_sell_decision(self, token: str, analysis: Dict):
        """Przetwórz decyzję sprzedaży"""
        percent = analysis.get("sell_percent", 100)
        reasons = analysis.get("reasons", [])
        reason_str = "; ".join(reasons) if reasons else ""
        
        print(f"📉 Sell decision: {token[:16]} {percent}%")
        
        await self._send_trade_signal("sell", token, percent=percent, reason=reason_str)
        
    async def _send_trade_signal(self, action: str, token: str, 
                                  amount: float = 0, percent: float = 100,
                                  reason: str = ""):
        """Wyślij trade signal"""
        self.stats["trades_signaled"] += 1
        
        if token in self.tokens:
            self.tokens[token].trade_sent = True
            
        await self.bus.signal_trade(
            action=action,
            token=token,
            amount=amount,
            percent=percent,
            reason=reason
        )
        
    async def _trigger_emergency_sell(self):
        """Wyzwól emergency sell"""
        # Wyślij critical risk alert
        await self.bus.signal_risk_alert(
            level="critical",
            message="EMERGENCY SELL TRIGGERED BY ORCHESTRATOR"
        )
        
        # Wyślij sell signal dla wszystkich otwartych pozycji
        # TODO: pobierz otwarte pozycje z portfolio
        
    def _calculate_position_size(self, score: int) -> float:
        """Oblicz rozmiar pozycji na podstawie score"""
        base = self.config.get("position_size_mon", 10)
        
        if score >= 90:
            multiplier = 3.0
        elif score >= 80:
            multiplier = 2.0
        elif score >= 70:
            multiplier = 1.5
        else:
            multiplier = 1.0
            
        return base * multiplier
        
    def _is_on_cooldown(self, token: str) -> bool:
        """Sprawdź czy token jest na cooldown"""
        if token not in self.token_cooldown:
            return False
        elapsed = (datetime.now() - self.token_cooldown[token]).total_seconds()
        return elapsed < self.COOLDOWN_SECONDS
        
    # === BACKGROUND TASKS ===
    
    async def _heartbeat_loop(self):
        """Wysyłaj heartbeat co 30s"""
        while self.running:
            try:
                task = f"tokens={len(self.tokens)}, pending={sum(1 for t in self.tokens.values() if t.analysis_pending)}"
                await self.bus.send_heartbeat("running", task)
                await asyncio.sleep(30)
            except Exception as e:
                print(f"❌ Heartbeat error: {e}")
                await asyncio.sleep(5)
                
    async def _cleanup_loop(self):
        """Cleanup starych tokenów co 5 min"""
        while self.running:
            try:
                await asyncio.sleep(300)
                
                # Usuń stare tokeny
                now = datetime.now()
                to_remove = []
                for token, state in self.tokens.items():
                    age = (now - state.discovered_at).total_seconds()
                    if age > 3600:  # 1h
                        to_remove.append(token)
                        
                for token in to_remove:
                    del self.tokens[token]
                    
                if to_remove:
                    print(f"🧹 Cleaned up {len(to_remove)} old tokens")
                    
            except Exception as e:
                print(f"❌ Cleanup error: {e}")


# === MAIN ===

async def main():
    orchestrator = OrchestratorV2()
    
    # Graceful shutdown
    loop = asyncio.get_event_loop()
    
    def shutdown_handler():
        print("\n🛑 Shutdown signal received")
        asyncio.create_task(orchestrator.stop())
        
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown_handler)
        
    try:
        await orchestrator.start()
    except KeyboardInterrupt:
        await orchestrator.stop()


if __name__ == "__main__":
    asyncio.run(main())
