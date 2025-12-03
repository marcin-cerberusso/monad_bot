#!/usr/bin/env python3
"""
💰 SELL EXECUTOR V2 - Zintegrowany z Message Bus

Odbiera:
- TRADE_SIGNAL (action=sell) → wykonuje sprzedaż
- RISK_ALERT (level=critical) → emergency sell

Wysyła:
- TRADE_EXECUTED → potwierdzenie transakcji
"""

import asyncio
import json
import os
import subprocess
import signal
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Tuple, List

import aiohttp
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv()

from agent_swarm.message_bus import MessageBus, get_bus
from agent_swarm.message_types import (
    Message, MessageType, Priority, TradeAction, RiskLevel,
    TradeSignalPayload, TradeExecutedPayload, RiskAlertPayload,
    MessageBuilder
)
from file_utils import safe_load_json, safe_save_json


# === CONFIG ===

BASE_DIR = Path(__file__).parent.parent
POSITIONS_FILE = BASE_DIR / "positions.json"
PORTFOLIO_STATE_FILE = BASE_DIR / "portfolio_state.json"
SELL_HISTORY_FILE = BASE_DIR / "sell_history.json"

WALLET_ADDRESS = os.getenv("WALLET_ADDRESS", "0x7b2897EA9547a6BB3c147b3E262483ddAb132A7D")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Max concurrent sells
MAX_CONCURRENT_SELLS = 3


class SellExecutorV2:
    """
    Sell Executor zintegrowany z Dragonfly Message Bus
    """
    
    def __init__(self):
        self.bus: Optional[MessageBus] = None
        self.running = False
        self.session: Optional[aiohttp.ClientSession] = None
        
        # Active sells
        self.active_sells: Dict[str, asyncio.Task] = {}
        self.sell_semaphore = asyncio.Semaphore(MAX_CONCURRENT_SELLS)
        
        # Stats
        self.stats = {
            "signals_received": 0,
            "sells_attempted": 0,
            "sells_success": 0,
            "sells_failed": 0,
            "emergency_sells": 0,
            "total_mon_received": 0.0
        }
        
    async def start(self):
        """Start executor"""
        print("💰 Sell Executor V2 starting...")
        
        # HTTP session
        self.session = aiohttp.ClientSession()
        
        # Message bus
        self.bus = await get_bus("sell_executor")
        await self.bus.subscribe("trader", "all")
        
        # Register handlers
        self._register_handlers()
        
        self.running = True
        
        # Background tasks
        tasks = [
            asyncio.create_task(self.bus.listen()),
            asyncio.create_task(self._heartbeat_loop()),
        ]
        
        print("💰 Sell Executor V2 running!")
        await self.bus.send_heartbeat("running", "ready")
        
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            print("💰 Sell Executor V2 stopping...")
            
    async def stop(self):
        """Stop executor"""
        self.running = False
        
        # Cancel active sells
        for task in self.active_sells.values():
            task.cancel()
            
        if self.session:
            await self.session.close()
        if self.bus:
            await self.bus.send_heartbeat("stopped", "shutdown")
            await self.bus.disconnect()
            
    def _register_handlers(self):
        """Register message handlers"""
        if not self.bus:
            raise RuntimeError("Bus not initialized")
        
        @self.bus.on(MessageType.TRADE_SIGNAL)
        async def handle_trade_signal(msg: Message):
            await self._handle_trade_signal(msg)
            
        @self.bus.on(MessageType.RISK_ALERT)
        async def handle_risk_alert(msg: Message):
            await self._handle_risk_alert(msg)
            
    async def _handle_trade_signal(self, msg: Message):
        """Handle trade signal"""
        self.stats["signals_received"] += 1
        payload = msg.payload
        
        action = payload.get("action", "")
        token = payload.get("token_address", "")
        token_name = payload.get("token_name", token[:12])
        percent = payload.get("sell_percent", 100)
        amount = payload.get("amount_mon", 0)
        reason = payload.get("reason", "signal")
        
        # Only handle sell signals
        if action.lower() not in ["sell", TradeAction.SELL.value]:
            print(f"📤 Ignoring {action} signal (not a sell)")
            return
            
        print(f"📨 Sell signal received: {token_name} {percent}% ({reason})")
        
        # Execute sell
        asyncio.create_task(self._execute_sell_with_semaphore(
            token=token,
            token_name=token_name,
            percent=percent,
            reason=reason,
            source=msg.sender
        ))
        
    async def _handle_risk_alert(self, msg: Message):
        """Handle risk alert"""
        payload = msg.payload
        level = payload.get("level", "")
        message = payload.get("message", "")
        action = payload.get("suggested_action", "")
        token = payload.get("token_address", "")
        
        if level.lower() not in ["critical", RiskLevel.CRITICAL.value]:
            return
            
        print(f"🚨 CRITICAL RISK ALERT: {message}")
        
        if action == "sell_all" or "emergency" in message.lower():
            await self._emergency_sell_all()
        elif token:
            asyncio.create_task(self._execute_sell_with_semaphore(
                token=token,
                token_name=token[:12],
                percent=100,
                reason=f"RISK: {message}",
                source=msg.sender,
                is_emergency=True
            ))
            
    async def _execute_sell_with_semaphore(self, token: str, token_name: str, 
                                            percent: float, reason: str,
                                            source: str, is_emergency: bool = False):
        """Execute sell with concurrency limit"""
        async with self.sell_semaphore:
            await self._execute_sell(token, token_name, percent, reason, source, is_emergency)
            
    async def _execute_sell(self, token: str, token_name: str, 
                            percent: float, reason: str, 
                            source: str, is_emergency: bool = False):
        """Execute sell via Rust position_manager"""
        self.stats["sells_attempted"] += 1
        if is_emergency:
            self.stats["emergency_sells"] += 1
            
        try:
            # Execute via Rust binary
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: subprocess.run(
                    [
                        str(BASE_DIR / "target" / "release" / "position_manager"),
                        "sell",
                        token,
                        str(percent)
                    ],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    cwd=str(BASE_DIR)
                )
            )
            
            if result.returncode == 0:
                # Parse output
                tx_hash, amount = self._parse_sell_output(result.stdout)
                
                self.stats["sells_success"] += 1
                self.stats["total_mon_received"] += amount
                
                print(f"✅ SOLD {token_name} ({percent}%): {amount:.4f} MON")
                
                # Broadcast success
                await self._broadcast_trade_executed(
                    token=token,
                    token_name=token_name,
                    action="sell",
                    success=True,
                    tx_hash=tx_hash,
                    amount=amount,
                    reason=reason
                )
                
                # Telegram
                await self._send_telegram(
                    f"💰 <b>SOLD</b> {token_name}\n\n"
                    f"Percent: {percent}%\n"
                    f"Reason: {reason}\n"
                    f"Received: {amount:.4f} MON\n"
                    f"TX: {tx_hash[:20]}..."
                )
                
                # Save to history
                self._save_to_history(token, token_name, percent, amount, tx_hash, reason)
                
            else:
                self.stats["sells_failed"] += 1
                error = result.stderr or "unknown error"
                print(f"❌ Sell failed: {error}")
                
                await self._broadcast_trade_executed(
                    token=token,
                    token_name=token_name,
                    action="sell",
                    success=False,
                    error=error,
                    reason=reason
                )
                
        except subprocess.TimeoutExpired:
            self.stats["sells_failed"] += 1
            print(f"❌ Sell timeout for {token_name}")
            await self._broadcast_trade_executed(
                token=token,
                token_name=token_name,
                action="sell",
                success=False,
                error="timeout",
                reason=reason
            )
            
        except FileNotFoundError:
            self.stats["sells_failed"] += 1
            print("❌ position_manager binary not found!")
            print("   Build: cargo build --release --bin position_manager")
            await self._broadcast_trade_executed(
                token=token,
                token_name=token_name,
                action="sell",
                success=False,
                error="binary not found",
                reason=reason
            )
            
        except Exception as e:
            self.stats["sells_failed"] += 1
            print(f"❌ Sell error: {e}")
            await self._broadcast_trade_executed(
                token=token,
                token_name=token_name,
                action="sell",
                success=False,
                error=str(e),
                reason=reason
            )
            
    def _parse_sell_output(self, output: str) -> Tuple[str, float]:
        """Parse position_manager output"""
        tx_hash = ""
        amount = 0.0
        
        for line in output.split("\n"):
            line_lower = line.lower()
            if "tx:" in line_lower or "hash:" in line_lower:
                tx_hash = line.split(":")[-1].strip()
            if "received:" in line_lower or "mon:" in line_lower:
                try:
                    # Handle formats like "Received: 10.5 MON" or "10.5MON"
                    num_str = line.split(":")[-1].strip()
                    num_str = num_str.replace("MON", "").replace("mon", "").strip()
                    amount = float(num_str)
                except (ValueError, IndexError):
                    pass
                    
        return tx_hash, amount
        
    async def _emergency_sell_all(self):
        """Emergency sell all positions"""
        print("🚨 EMERGENCY SELL ALL!")
        
        # Load positions
        positions = safe_load_json(PORTFOLIO_STATE_FILE, {"positions": []})
        
        tasks = []
        for pos in positions.get("positions", []):
            token = pos.get("token_address", "")
            name = pos.get("symbol", token[:12])
            if token:
                tasks.append(self._execute_sell_with_semaphore(
                    token=token,
                    token_name=name,
                    percent=100,
                    reason="EMERGENCY_SELL_ALL",
                    source="risk_manager",
                    is_emergency=True
                ))
                
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            print(f"🚨 Emergency sell: {len(tasks)} positions processed")
        else:
            print("ℹ️ No positions to sell")
            
    async def _broadcast_trade_executed(self, token: str, token_name: str,
                                         action: str, success: bool,
                                         tx_hash: str = "", amount: float = 0,
                                         error: str = "", reason: str = ""):
        """Broadcast trade executed message"""
        if not self.bus:
            return
            
        payload = TradeExecutedPayload(
            action=TradeAction.SELL,
            token_address=token,
            token_name=token_name,
            amount_mon=amount,
            tx_hash=tx_hash,
            success=success,
            error=error
        )
        
        await self.bus.broadcast(Message(
            type=MessageType.TRADE_EXECUTED,
            sender=self.bus.agent_name,
            payload=payload.to_dict(),
            priority=Priority.HIGH
        ))
        
    def _save_to_history(self, token: str, name: str, percent: float,
                         amount: float, tx_hash: str, reason: str):
        """Save sell to history"""
        history = safe_load_json(SELL_HISTORY_FILE, {"sells": []})
        sells = history.get("sells", [])
        
        sells.append({
            "token_address": token,
            "token_name": name,
            "percent": percent,
            "amount_received": amount,
            "tx_hash": tx_hash,
            "reason": reason,
            "executed_at": datetime.now().isoformat()
        })
        
        # Keep last 1000
        sells = sells[-1000:]
        safe_save_json(SELL_HISTORY_FILE, {"sells": sells})
        
    async def _send_telegram(self, message: str):
        """Send Telegram notification"""
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID or not self.session:
            return
            
        try:
            await self.session.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": message,
                    "parse_mode": "HTML"
                },
                timeout=aiohttp.ClientTimeout(total=5)
            )
        except Exception:
            pass  # Non-critical
            
    async def _heartbeat_loop(self):
        """Send heartbeat every 30s"""
        while self.running:
            try:
                if self.bus:
                    task = f"success={self.stats['sells_success']}/{self.stats['sells_attempted']}"
                    await self.bus.send_heartbeat("running", task)
                await asyncio.sleep(30)
            except Exception as e:
                print(f"❌ Heartbeat error: {e}")
                await asyncio.sleep(5)


# === MAIN ===

async def main():
    executor = SellExecutorV2()
    
    loop = asyncio.get_event_loop()
    
    def shutdown_handler():
        print("\n🛑 Shutdown signal")
        asyncio.create_task(executor.stop())
        
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown_handler)
        
    try:
        await executor.start()
    except KeyboardInterrupt:
        await executor.stop()


if __name__ == "__main__":
    asyncio.run(main())
