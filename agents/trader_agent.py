"""
💰 TRADER AGENT - Wykonuje buy/sell na NAD.FUN
"""
import asyncio
import os
import json
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Optional
from dotenv import load_dotenv
from web3 import Web3

from .base_agent import BaseAgent, Message, MessageTypes, Channels
from .notifications import get_notifier
from . import config
from . import decision_logger
from .smart_agent import SmartTradingAgent

load_dotenv()

ROUTER = "0x6F6B8F1a20703309951a5127c45B49b1CD981A22"
RPC_URL = os.getenv("MONAD_RPC_URL")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
WALLET = os.getenv("WALLET_ADDRESS", "0x7b2897EA9547a6BB3c147b3E262483ddAb132A7D")
POSITIONS_FILE = Path(__file__).resolve().parent.parent / "positions.json"
BUY_SCRIPT = Path(__file__).resolve().parent.parent / "buy_token.py"
SELL_SCRIPT = Path(__file__).resolve().parent.parent / "sell_token.py"
MAX_FOLLOW_SIZE = float(os.getenv("FOLLOW_AMOUNT_MON", "20"))


class TraderAgent(BaseAgent):
    """Agent wykonujący transakcje"""
    
    def __init__(self, redis_url: str = "redis://localhost:6379"):
        super().__init__("TraderAgent", redis_url)
        self.w3 = Web3(Web3.HTTPProvider(RPC_URL))
        self.trades_today = 0
        
        # 🧠 Memory system
        self.smart = SmartTradingAgent("TraderMemory", "data")
        self.log("🧠 Memory system initialized")
        
    async def run(self):
        """Subscribe to trader channel"""
        await self.subscribe(Channels.TRADER)
        self.log(f"Ready! Wallet: {WALLET[:12]}... Max: {MAX_FOLLOW_SIZE} MON")
        
        while self.running:
            await asyncio.sleep(1)
    
    async def on_message(self, message: Message):
        """Handle trade orders"""
        if message.type == MessageTypes.BUY_ORDER:
            await self._execute_buy(message.data)
        elif message.type == MessageTypes.SELL_ORDER:
            await self._execute_sell(message.data)
    
    async def _execute_buy(self, data: dict):
        """Execute buy order"""
        token = data.get("token")
        amount = data.get("amount", config.DEFAULT_TRADE_SIZE)
        whale = data.get("whale", "unknown")
        confidence = data.get("confidence", 0)
        
        self.log(f"🛒 Buying {amount} MON of {token[:16]}...")
        
        try:
            # Execute buy
            result = subprocess.run(
                ["python3", "buy_token.py", token, str(amount)],
                capture_output=True, text=True, timeout=60
            )
            
            if result.returncode == 0:
                self.log(f"✅ Buy successful!")
                
                # Send Telegram notification
                notifier = get_notifier()
                await notifier.notify_buy(token, amount, whale, confidence)
                
                # Save position
                self._save_position(token, amount, whale)
                
                return True
            else:
                self.log(f"❌ Buy failed: {result.stderr}")
                return False
                
        except Exception as e:
            self.log(f"❌ Buy error: {e}")
            await get_notifier().notify_error(str(e), f"Buy {token[:16]}")
            return False
    
    async def _execute_sell(self, data: dict):
        """Execute sell order"""
        token = data.get("token")
        percent = data.get("percent", 100)
        reason = data.get("reason", "manual")
        pnl = data.get("pnl_percent", 0)
        action = data.get("action", "SELL")
        
        self.log(f"💸 Selling {percent}% of {token[:16]}... ({reason})")
        
        try:
            # Execute sell
            result = subprocess.run(
                ["python3", "sell_token.py", token, str(percent)],
                capture_output=True, text=True, timeout=60
            )
            
            if result.returncode == 0:
                self.log(f"✅ Sell successful!")
                
                # Send Telegram notification
                notifier = get_notifier()
                await notifier.notify_sell(token, percent, reason, pnl)
                
                # Update or remove position
                if percent >= 100:
                    self._remove_position(token)
                
                return True
            else:
                self.log(f"❌ Sell failed: {result.stderr}")
                return False
                
        except Exception as e:
            self.log(f"❌ Sell error: {e}")
            await get_notifier().notify_error(str(e), f"Sell {token[:16]}")
            return False

    async def _buy(self, token: str, amount_mon: float) -> tuple:
        """Execute buy via buy_token.py"""
        try:
            result = subprocess.run(
                ["python3", str(BUY_SCRIPT), token, str(amount_mon)],
                capture_output=True,
                text=True,
                timeout=60,
                cwd=str(BUY_SCRIPT.parent)
            )
            
            if result.returncode == 0:
                # Extract tx hash from output
                for line in result.stdout.split("\n"):
                    if "0x" in line and len(line) >= 66:
                        return True, line.strip()
                return True, "success"
            return False, result.stderr
        except Exception as e:
            return False, str(e)
    
    async def _sell(self, token: str, percent: int = 100) -> tuple:
        """Execute sell via sell_token.py"""
        try:
            result = subprocess.run(
                ["python3", str(SELL_SCRIPT), token, str(percent)],
                capture_output=True,
                text=True,
                timeout=60,
                cwd=str(SELL_SCRIPT.parent)
            )
            
            if result.returncode == 0:
                for line in result.stdout.split("\n"):
                    if "0x" in line and len(line) >= 66:
                        return True, line.strip()
                return True, "success"
            return False, result.stderr
        except Exception as e:
            return False, str(e)
    
    def _get_balance(self) -> float:
        """Get MON balance"""
        try:
            bal = self.w3.eth.get_balance(WALLET)
            return bal / 1e18
        except:
            return 0
    
    def _load_positions(self) -> dict:
        """Load positions"""
        try:
            if POSITIONS_FILE.exists():
                with open(POSITIONS_FILE) as f:
                    return json.load(f)
        except:
            pass
        return {}
    
    def _save_position(self, token: str, amount_mon: float, whale: str, 
                        confidence: float = 0.5, smart_action: str = "buy"):
        """Save position with proper fields"""
        try:
            positions = self._load_positions()
            positions[token.lower()] = {
                "token": token.lower(),
                "amount_mon": amount_mon,
                "entry_value": amount_mon,  # For PnL calculation
                "entry_time": datetime.now().isoformat(),
                "tx_hash": "success",
                "whale": whale,
                "ai_confidence": confidence,
                "smart_action": smart_action,
                "liquidity_usd": 0
            }
            with open(POSITIONS_FILE, "w") as f:
                json.dump(positions, f, indent=2)
        except Exception as e:
            self.log(f"Error saving position: {e}")
    
    def _remove_position(self, token: str):
        """Remove position"""
        try:
            positions = self._load_positions()
            if token.lower() in positions:
                del positions[token.lower()]
                with open(POSITIONS_FILE, "w") as f:
                    json.dump(positions, f, indent=2)
        except:
            pass


if __name__ == "__main__":
    agent = TraderAgent()
    asyncio.run(agent.start())
