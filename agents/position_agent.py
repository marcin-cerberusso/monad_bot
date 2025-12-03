"""
📊 POSITION AGENT - Zarządza pozycjami (TP/SL/Trailing)
"""
import asyncio
import os
import json
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Dict, Optional
from dotenv import load_dotenv

from .base_agent import BaseAgent, Message, MessageTypes, Channels
from . import config

load_dotenv()

LENS = "0x7e78A8DE94f21804F7a17F4E8BF9EC2c872187ea"
RPC_URL = os.getenv("MONAD_RPC_URL")
CAST_PATH = os.path.expanduser("~/.foundry/bin/cast")
POSITIONS_FILE = Path(__file__).resolve().parent.parent / "positions.json"

# TP/SL settings from central config
TP1_PERCENT = config.TP1_PERCENT
TP1_SELL = config.TP1_SELL_PERCENT
TP2_PERCENT = config.TP2_PERCENT
TP2_SELL = config.TP2_SELL_PERCENT
STOP_LOSS = config.STOP_LOSS_PERCENT
TRAILING_ACTIVATE = config.TRAILING_ACTIVATE
TRAILING_STOP = config.TRAILING_STOP


class PositionAgent(BaseAgent):
    """Agent zarządzający pozycjami"""
    
    def __init__(self, redis_url: str = "redis://localhost:6379"):
        super().__init__("PositionAgent", redis_url)
        self.check_interval = 30  # seconds
        
    async def run(self):
        """Main loop - check positions periodically"""
        await self.subscribe(Channels.POSITION)
        
        # Initial load
        positions = self._load_positions()
        self.log(f"Starting position monitoring... Resumed {len(positions)} positions.")
        
        while self.running:
            await self._check_positions()
            await asyncio.sleep(self.check_interval)
    
    async def on_message(self, message: Message):
        """Handle position updates"""
        if message.type == MessageTypes.TRADE_EXECUTED:
            self.log(f"Trade executed: {message.data.get('action')} {message.data.get('token', '')[:12]}...")
        
        # === WHALE SELL - natychmiast sprzedaj pozycję! ===
        elif message.type == MessageTypes.WHALE_SELL:
            token = message.data.get("token", "").lower()
            whale = message.data.get("whale", "")[:10]
            self.log(f"🔴 WHALE EXIT: {whale}... selling {token[:12]} - following!")
            await self._emergency_sell_token(token, reason="whale_exit")
    
    async def _check_positions(self):
        """Check all positions for TP/SL"""
        positions = self._load_positions()
        
        if not positions:
            return
        
        self.log(f"Checking {len(positions)} positions...")
        
        for token, pos in list(positions.items()):
            try:
                await self._check_position(token, pos)
            except Exception as e:
                self.log(f"  Error checking {token[:12]}: {e}")
    
    async def _check_position(self, token: str, pos: dict):
        """Check single position"""
        entry_amount = pos.get("amount_mon", 0)
        if entry_amount <= 0:
            return
        
        # Get current value
        current_value = await self._get_position_value(token)
        if current_value is None:
            return
        
        pnl_percent = ((current_value - entry_amount) / entry_amount) * 100
        
        # Update highest value for trailing
        highest = pos.get("highest_value", entry_amount)
        if current_value > highest:
            highest = current_value
            pos["highest_value"] = highest
            self._save_position(token, pos)
        
        # Calculate drawdown from ATH
        drawdown = ((highest - current_value) / highest) * 100 if highest > 0 else 0
        
        self.log(f"  {token[:12]}: PnL {pnl_percent:+.1f}% (ATH draw: -{drawdown:.1f}%)")
        
        # Check conditions
        action = None
        percent_to_sell = 0
        reason = ""
        
        # Stop Loss
        if pnl_percent <= STOP_LOSS:
            action = "sell"
            percent_to_sell = 100
            reason = f"STOP LOSS at {pnl_percent:.1f}%"
        
        # TP1 (not taken yet)
        elif pnl_percent >= TP1_PERCENT and not pos.get("tp1_taken"):
            action = "sell"
            percent_to_sell = TP1_SELL
            reason = f"TP1 at +{pnl_percent:.1f}%"
            pos["tp1_taken"] = True
            self._save_position(token, pos)
        
        # TP2 (not taken yet)
        elif pnl_percent >= TP2_PERCENT and not pos.get("tp2_taken"):
            action = "sell"
            percent_to_sell = TP2_SELL
            reason = f"TP2 at +{pnl_percent:.1f}%"
            pos["tp2_taken"] = True
            self._save_position(token, pos)
        
        # Trailing stop
        elif pnl_percent >= TRAILING_ACTIVATE:
            if drawdown >= TRAILING_STOP:
                action = "sell"
                percent_to_sell = 100
                reason = f"TRAILING STOP ({drawdown:.1f}% from ATH)"
        
        # Execute sell if needed
        if action == "sell":
            self.log(f"  🔔 {reason}")
            await self.publish(Channels.TRADER, Message(
                type=MessageTypes.SELL_ORDER,
                data={
                    "token": token,
                    "percent": percent_to_sell,
                    "reason": reason
                },
                sender=self.name
            ))
    
    async def _get_position_value(self, token: str) -> Optional[float]:
        """Get current position value in MON"""
        try:
            # Get token balance
            positions = self._load_positions()
            pos = positions.get(token.lower())
            if not pos:
                return None
            
            # For simplicity, use entry amount * (1 + simulated change)
            # In production, query actual balance and price
            entry = pos.get("amount_mon", 0)
            
            # Get sell quote from Lens
            # This is a simplified version - in production query actual balance
            amount_wei = int(entry * 1e18)
            
            cmd = f'{CAST_PATH} call {LENS} "getAmountOut(address,uint256,bool)" {token} {amount_wei} false --rpc-url {RPC_URL}'
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
            
            if result.returncode == 0 and result.stdout.strip():
                # Parse tuple output
                output = result.stdout.strip()
                # Extract second value (amount out)
                if len(output) > 66:
                    hex_val = output[-64:]
                    return int(hex_val, 16) / 1e18
            
            return entry  # Fallback to entry value
            
        except Exception as e:
            return None
    
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
        except:
            pass
    
    async def _emergency_sell_token(self, token: str, reason: str = "whale_exit"):
        """Natychmiastowa sprzedaż tokena - whale exit!"""
        positions = self._load_positions()
        token_lower = token.lower()
        
        if token_lower not in positions:
            self.log(f"  No position in {token[:12]} to sell")
            return
        
        pos = positions[token_lower]
        self.log(f"🚨 EMERGENCY SELL: {token[:12]}... (reason: {reason})")
        
        # Wyślij SELL_ORDER do TraderAgent
        await self.publish(Channels.TRADER, Message(
            type=MessageTypes.SELL_ORDER,
            data={
                "token": token,
                "sell_percent": 100,  # Sprzedaj wszystko!
                "reason": reason,
                "position": pos
            },
            sender=self.name
        ))
        
        # Usuń pozycję
        del positions[token_lower]
        with open(POSITIONS_FILE, "w") as f:
            json.dump(positions, f, indent=2)
        
        # Notify
        await self.notify(
            "🚨 WHALE EXIT - Sold!",
            f"Following whale - sold 100% of {token[:16]}",
            0xFF0000  # Red
        )


if __name__ == "__main__":
    agent = PositionAgent()
    asyncio.run(agent.start())
