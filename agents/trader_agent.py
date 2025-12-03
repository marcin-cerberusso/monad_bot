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
from .notifications import notifier
from . import decision_logger
from . import config

load_dotenv()

ROUTER = "0x6F6B8F1a20703309951a5127c45B49b1CD981A22"
RPC_URL = os.getenv("MONAD_RPC_URL")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
WALLET = os.getenv("WALLET_ADDRESS", "0x7b2897EA9547a6BB3c147b3E262483ddAb132A7D")
POSITIONS_FILE = Path(__file__).resolve().parent.parent / "positions.json"
BUY_SCRIPT = Path(__file__).resolve().parent.parent / "buy_token.py"
SELL_SCRIPT = Path(__file__).resolve().parent.parent / "sell_token.py"
MAX_FOLLOW_SIZE = config.FOLLOW_AMOUNT_MON


class TraderAgent(BaseAgent):
    """Agent wykonujący transakcje"""
    
    def __init__(self, redis_url: str = "redis://localhost:6379"):
        super().__init__("TraderAgent", redis_url)
        self.w3 = Web3(Web3.HTTPProvider(RPC_URL))
        self.trades_today = 0
        
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
        token = data["token"]
        suggested_amount = data.get("suggested_amount", 10)
        amount = min(suggested_amount, MAX_FOLLOW_SIZE)
        
        self.log(f"🛒 Buying {amount:.1f} MON of {token[:12]}...")
        
        # Check balance
        balance = self._get_balance()
        if balance < amount + 1:  # +1 for gas
            self.log(f"  ❌ Insufficient balance: {balance:.2f} MON")
            return
        
        # Check if already have position
        positions = self._load_positions()
        if token.lower() in positions:
            self.log(f"  ⚠️ Already have position")
            return
        
        # Execute buy
        success, tx_hash = await self._buy(token, amount)
        
        if success:
            self.trades_today += 1
            
            # Log for ML
            decision_logger.log_trade(
                token=token,
                action="BUY",
                amount_mon=amount,
                tx_hash=tx_hash,
                success=True,
                whale_amount=data.get("amount_mon"),
                ai_confidence=data.get("ai_decision", {}).get("confidence")
            )
            
            # Save position
            position = {
                "token": token,
                "amount_mon": amount,
                "entry_time": datetime.now().isoformat(),
                "tx_hash": tx_hash,
                "whale": data.get("whale", ""),
                "ai_confidence": data.get("ai_decision", {}).get("confidence", 0),
                "liquidity_usd": data.get("liquidity_usd", 0)
            }
            self._save_position(token, position)
            
            self.log(f"  ✅ Bought! TX: {tx_hash[:16]}...")
            
            # Send notification
            await notifier.send_alert(
                "🟢 BUY EXECUTED",
                f"**Token:** `{token}`\n**Amount:** `{amount} MON`\n**TX:** `{tx_hash}`",
                0x00FF00
            )
            
            # Notify position agent
            await self.publish(Channels.POSITION, Message(
                type=MessageTypes.TRADE_EXECUTED,
                data={"action": "buy", **position},
                sender=self.name
            ))
        else:
            self.log(f"  ❌ Buy failed")
            decision_logger.log_trade(
                token=token,
                action="BUY",
                amount_mon=amount,
                success=False,
                error="Transaction failed"
            )
            await notifier.send_alert(
                "🔴 BUY FAILED",
                f"Failed to buy `{token}`",
                0xFF0000
            )
    
    async def _execute_sell(self, data: dict):
        """Execute sell order"""
        token = data["token"]
        percent = data.get("percent", 100)
        
        self.log(f"💸 Selling {percent}% of {token[:12]}...")
        
        success, tx_hash = await self._sell(token, percent)
        
        if success:
            self.log(f"  ✅ Sold! TX: {tx_hash[:16]}...")
            
            # Send notification
            await notifier.send_alert(
                "🟢 SELL EXECUTED",
                f"**Token:** `{token}`\n**Percent:** `{percent}%`\n**TX:** `{tx_hash}`",
                0x00FF00
            )
            
            if percent >= 100:
                self._remove_position(token)
            
            await self.publish(Channels.POSITION, Message(
                type=MessageTypes.TRADE_EXECUTED,
                data={"action": "sell", "token": token, "percent": percent, "tx_hash": tx_hash},
                sender=self.name
            ))
        else:
            self.log(f"  ❌ Sell failed")
            await notifier.send_alert(
                "🔴 SELL FAILED",
                f"Failed to sell `{token}`",
                0xFF0000
            )
    
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
    
    def _save_position(self, token: str, data: dict):
        """Save position"""
        try:
            positions = self._load_positions()
            positions[token.lower()] = data
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
