#!/usr/bin/env python3
"""
💰 SELL EXECUTOR - Moduł sprzedaży dla Agent Swarm

Integruje się z:
- position_manager (Rust)
- portfolio_manager (Rust)  
- Agent Swarm (consensus)

Wykonuje:
- Take Profit sells
- Stop Loss sells
- Partial sells
- Emergency sells
"""

import asyncio
import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional
import aiohttp
from dotenv import load_dotenv
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from file_utils import safe_load_json, safe_save_json

load_dotenv()


class SellType(Enum):
    """Typ sprzedaży"""
    TAKE_PROFIT = "TP"
    STOP_LOSS = "SL"
    PARTIAL = "PARTIAL"
    EMERGENCY = "EMERGENCY"
    MANUAL = "MANUAL"

# Config
BASE_DIR = Path(__file__).parent.parent
POSITIONS_FILE = BASE_DIR / "positions.json"
PORTFOLIO_FILE = BASE_DIR / "portfolio.json"
SELL_QUEUE_FILE = BASE_DIR / "sell_queue.json"
SELL_HISTORY_FILE = BASE_DIR / "sell_history.json"

# Monad config
MONAD_RPC = os.getenv("MONAD_RPC_URL", "")
ENABLE_DIRECT_RPC_SELL = os.getenv("ENABLE_DIRECT_RPC_SELL", "false").lower() in ("1", "true", "yes")
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS", "0x7b2897EA9547a6BB3c147b3E262483ddAb132A7D")
NADFUN_ROUTER = "0x6F6B8F1a20703309951a5127c45B49b1CD981A22"

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


@dataclass
class SellOrder:
    """Zlecenie sprzedaży"""
    token_address: str
    token_name: str
    sell_percent: float  # 0-100
    reason: str  # TP, SL, PARTIAL, EMERGENCY, MANUAL
    priority: int  # 1-10 (10 = urgent)
    created_at: str
    executed: bool = False
    executed_at: Optional[str] = None
    tx_hash: Optional[str] = None
    amount_received: float = 0.0
    retries: int = 0
    fail_reason: str = ""
    
    def to_dict(self) -> dict:
        return {
            "token_address": self.token_address,
            "token_name": self.token_name,
            "sell_percent": self.sell_percent,
            "reason": self.reason,
            "priority": self.priority,
            "created_at": self.created_at,
            "executed": self.executed,
            "executed_at": self.executed_at,
            "tx_hash": self.tx_hash,
            "amount_received": self.amount_received,
            "retries": self.retries,
            "fail_reason": self.fail_reason
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "SellOrder":
        return cls(
            token_address=data["token_address"],
            token_name=data.get("token_name", ""),
            sell_percent=data["sell_percent"],
            reason=data.get("reason", "MANUAL"),
            priority=data.get("priority", 5),
            created_at=data.get("created_at", datetime.now().isoformat()),
            executed=data.get("executed", False),
            executed_at=data.get("executed_at"),
            tx_hash=data.get("tx_hash"),
            amount_received=data.get("amount_received", 0.0),
            retries=data.get("retries", 0),
            fail_reason=data.get("fail_reason", "")
        )


class SellExecutor:
    """
    Executor sprzedaży - zarządza kolejką i wykonuje sell
    """
    
    def __init__(self):
        self.sell_queue: List[SellOrder] = []
        self.sell_history: List[SellOrder] = []
        self.running = False
        self.session: Optional[aiohttp.ClientSession] = None
        self.max_retries = 5
        
        # Load existing queue
        self._load_queue()
        
    def _load_queue(self):
        """Load sell queue from file"""
        data = safe_load_json(SELL_QUEUE_FILE, {"orders": []})
        self.sell_queue = [SellOrder.from_dict(o) for o in data.get("orders", [])]
            
    def _save_queue(self):
        """Save sell queue to file"""
        if not safe_save_json(SELL_QUEUE_FILE, {
            "orders": [o.to_dict() for o in self.sell_queue],
            "updated_at": datetime.now().isoformat()
        }):
            print(f"⚠️ Could not save sell queue")
            
    def _save_history(self, order: SellOrder):
        """Save executed order to history"""
        history_data = safe_load_json(SELL_HISTORY_FILE, {"sells": []})
        history = history_data.get("sells", [])
        history.append(order.to_dict())
        # Keep last 1000
        history = history[-1000:]
        if not safe_save_json(SELL_HISTORY_FILE, {"sells": history}):
            print(f"⚠️ Could not save history")
            
    async def start(self):
        """Start executor"""
        self.running = True
        self.session = aiohttp.ClientSession()
        print("💰 Sell Executor started")
        if not MONAD_RPC:
            print("⚠️ MONAD_RPC_URL nie ustawiony - direct RPC fallback będzie pominięty")
        
    async def stop(self):
        """Stop executor"""
        self.running = False
        if self.session:
            await self.session.close()
        print("💰 Sell Executor stopped")
    
    async def queue_sell(self, 
                         token_address: str,
                         sell_type: SellType = SellType.MANUAL,
                         reason: str = "Manual sell",
                         percentage: float = 100) -> SellOrder:
        """
        Async version - queue sell from launcher/orchestrator
        """
        # Map SellType to priority
        priority_map = {
            SellType.EMERGENCY: 10,
            SellType.STOP_LOSS: 9,
            SellType.TAKE_PROFIT: 7,
            SellType.PARTIAL: 6,
            SellType.MANUAL: 5
        }
        priority = priority_map.get(sell_type, 5)
        
        # Get token name (from portfolio or contract)
        token_name = await self._get_token_name(token_address)
        
        return self._queue_sell_sync(
            token_address=token_address,
            token_name=token_name,
            sell_percent=percentage,
            reason=reason,
            priority=priority
        )
    
    async def _get_token_name(self, token_address: str) -> str:
        """Get token name from portfolio or contract"""
        # Try portfolio first
        portfolio_file = BASE_DIR / "portfolio_state.json"
        portfolio = safe_load_json(portfolio_file, {"positions": []})
        for pos in portfolio.get("positions", []):
            if pos.get("token_address", "").lower() == token_address.lower():
                return pos.get("symbol", pos.get("name", token_address[:12]))
        
        # Return shortened address as fallback
        return token_address[:12] + "..."
        
    def _queue_sell_sync(self, 
                   token_address: str,
                   token_name: str,
                   sell_percent: float,
                   reason: str = "MANUAL",
                   priority: int = 5) -> SellOrder:
        """
        Dodaj zlecenie sprzedaży do kolejki
        """
        order = SellOrder(
            token_address=token_address.lower(),
            token_name=token_name,
            sell_percent=sell_percent,
            reason=reason,
            priority=priority,
            created_at=datetime.now().isoformat()
        )
        
        # Insert by priority (higher first)
        inserted = False
        for i, existing in enumerate(self.sell_queue):
            if order.priority > existing.priority:
                self.sell_queue.insert(i, order)
                inserted = True
                break
                
        if not inserted:
            self.sell_queue.append(order)
            
        self._save_queue()
        print(f"📝 Queued SELL: {token_name} ({sell_percent}%) - {reason}")
        
        return order
        
    def queue_take_profit(self, token_address: str, token_name: str, percent: float = 100):
        """Take Profit sell"""
        return self._queue_sell_sync(token_address, token_name, percent, "TP", priority=7)
        
    def queue_stop_loss(self, token_address: str, token_name: str, percent: float = 100):
        """Stop Loss sell - higher priority"""
        return self._queue_sell_sync(token_address, token_name, percent, "SL", priority=9)
        
    def queue_emergency(self, token_address: str, token_name: str):
        """Emergency sell - highest priority"""
        return self._queue_sell_sync(token_address, token_name, 100, "EMERGENCY", priority=10)
        
    def queue_partial(self, token_address: str, token_name: str, percent: float):
        """Partial sell"""
        return self._queue_sell_sync(token_address, token_name, percent, "PARTIAL", priority=6)
        
    async def process_queue(self):
        """Process sell queue"""
        while self.running:
            try:
                if self.sell_queue:
                    # Get highest priority order
                    order = self.sell_queue[0]
                    
                    if not order.executed:
                        print(f"\n💰 Processing SELL: {order.token_name} ({order.sell_percent}%)")
                        
                        # Execute sell
                        success, tx_hash, amount, fail_reason = await self._execute_sell(order)
                        
                        if success:
                            order.executed = True
                            order.executed_at = datetime.now().isoformat()
                            order.tx_hash = tx_hash
                            order.amount_received = amount
                            order.fail_reason = ""
                            order.retries = 0
                            
                            # Move to history
                            self._save_history(order)
                            self.sell_queue.pop(0)
                            self._save_queue()
                            
                            # Send alert
                            await self._send_telegram(
                                f"💰 <b>SOLD</b> {order.token_name}\n\n"
                                f"Percent: {order.sell_percent}%\n"
                                f"Reason: {order.reason}\n"
                                f"Received: {amount:.4f} MON\n"
                                f"TX: {tx_hash[:16]}..."
                            )
                        else:
                            order.retries += 1
                            order.fail_reason = fail_reason or "unknown error"
                            self._save_queue()
                            backoff = min(60, 2 ** order.retries)
                            if order.retries >= self.max_retries:
                                print(f"❌ Sell failed after {order.retries} retries -> parking in history ({order.fail_reason})")
                                order.executed = False
                                self._save_history(order)
                                self.sell_queue.pop(0)
                                self._save_queue()
                            else:
                                print(f"❌ Sell failed ({order.fail_reason}), retry {order.retries}/{self.max_retries} in {backoff}s")
                                await asyncio.sleep(backoff)
                            
            except Exception as e:
                print(f"❌ Queue processing error: {e}")
                
            await asyncio.sleep(1)
            
    async def _execute_sell(self, order: SellOrder) -> tuple[bool, str, float, str]:
        """
        Execute sell via Rust binary or direct RPC
        """
        try:
            # Option 1: Use position_manager CLI
            result = subprocess.run(
                [
                    str(BASE_DIR / "position_manager"),
                    "sell",
                    order.token_address,
                    str(order.sell_percent)
                ],
                capture_output=True,
                text=True,
                timeout=60,
                cwd=str(BASE_DIR)
            )
            
            if result.returncode == 0:
                # Parse output for tx hash and amount
                output = result.stdout
                tx_hash = ""
                amount = 0.0
                
                for line in output.split("\n"):
                    if "tx:" in line.lower() or "hash:" in line.lower():
                        tx_hash = line.split(":")[-1].strip()
                    if "received:" in line.lower() or "mon:" in line.lower():
                        try:
                            amount = float(line.split(":")[-1].strip().replace("MON", "").strip())
                        except (ValueError, IndexError):
                            pass  # Amount parsing failed, use default 0.0
                            
                return True, tx_hash, amount, ""
            else:
                print(f"❌ position_manager error: {result.stderr}")
                
                # Fallback: Try direct RPC call
                return await self._execute_sell_direct(order)
                
        except subprocess.TimeoutExpired:
            print("❌ Sell timeout")
            return False, "", 0.0, "timeout"
        except FileNotFoundError:
            # position_manager not found, try direct
            return await self._execute_sell_direct(order)
        except Exception as e:
            print(f"❌ Sell error: {e}")
            return False, "", 0.0, str(e)
            
    async def _execute_sell_direct(self, order: SellOrder) -> tuple[bool, str, float, str]:
        """
        Direct RPC sell - DISABLED
        
        Note: This fallback is intentionally disabled. All sells should go through
        the Rust position_manager binary which handles signing and transaction
        construction properly. This method exists only as a placeholder.
        
        To enable: implement proper transaction signing with web3.py or similar.
        """
        # Log why we're not using direct RPC
        print("⚠️ Direct RPC sell disabled - position_manager binary required")
        print("   Install/build position_manager: cargo build --release --bin position_manager")
        
        return False, "", 0.0, "direct RPC disabled - use position_manager binary"
            
    async def _get_token_balance(self, token_address: str) -> int:
        """Get token balance"""
        if not self.session or not MONAD_RPC:
            return 0
            
        try:
            # balanceOf(address)
            data = f"0x70a08231{WALLET_ADDRESS[2:].zfill(64)}"
            
            payload = {
                "jsonrpc": "2.0",
                "method": "eth_call",
                "params": [{
                    "to": token_address,
                    "data": data
                }, "latest"],
                "id": 1
            }
            
            async with self.session.post(MONAD_RPC, json=payload) as resp:
                result = await resp.json()
                balance_hex = result.get("result", "0x0")
                return int(balance_hex, 16)
                
        except Exception as e:
            print(f"❌ Balance check error: {e}")
            return 0
            
    async def _estimate_gas(self, data: str) -> int:
        """Estimate gas for transaction"""
        if not self.session or not MONAD_RPC:
            return 200000  # Default
            
        try:
            payload = {
                "jsonrpc": "2.0",
                "method": "eth_estimateGas",
                "params": [{
                    "from": WALLET_ADDRESS,
                    "to": NADFUN_ROUTER,
                    "data": data
                }],
                "id": 1
            }
            
            async with self.session.post(MONAD_RPC, json=payload) as resp:
                result = await resp.json()
                gas_hex = result.get("result", "0x30d40")
                return int(gas_hex, 16)
                
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError):
            return 200000  # Default gas estimate on error
            
    async def _send_telegram(self, message: str):
        """Send Telegram notification"""
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            return
            
        try:
            if self.session:
                await self.session.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                    json={
                        "chat_id": TELEGRAM_CHAT_ID,
                        "text": message,
                        "parse_mode": "HTML"
                    }
                )
        except Exception:
            pass  # Telegram alerts are non-critical
            
    def get_queue_status(self) -> dict:
        """Get current queue status"""
        return {
            "pending": len([o for o in self.sell_queue if not o.executed]),
            "orders": [o.to_dict() for o in self.sell_queue[:10]]
        }


# Singleton
_executor: Optional[SellExecutor] = None


def get_sell_executor() -> SellExecutor:
    """Get singleton executor"""
    global _executor
    if _executor is None:
        _executor = SellExecutor()
    return _executor


async def main():
    """Test sell executor"""
    executor = get_sell_executor()
    await executor.start()
    
    try:
        # Test queue
        executor.queue_take_profit(
            "0x5E1b1A14c8758104B8560514e94ab8320e587777",
            "MonadMeme",
            50  # 50% partial TP
        )
        
        print("\n📊 Queue status:")
        print(json.dumps(executor.get_queue_status(), indent=2))
        
        # Process would normally run in background
        # await executor.process_queue()
        
    finally:
        await executor.stop()


if __name__ == "__main__":
    asyncio.run(main())
