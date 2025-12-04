"""
📊 POSITION AGENT - Zarządza pozycjami (TP/SL/Trailing)
"""
import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from .base_agent import BaseAgent, Message, MessageType
from . import config


LENS = "0x7e78A8DE94f21804F7a17F4E8BF9EC2c872187ea"
RPC_URL = config.MONAD_RPC_URL
CAST_PATH = config.CAST_PATH
POSITIONS_FILE = Path(__file__).resolve().parent.parent / "positions.json"

# TP/SL settings
TP1_PERCENT = 30   # Take 30% profit at +30%
TP2_PERCENT = 60   # Take 40% more at +60%
STOP_LOSS = -25    # Stop loss at -25%
TRAILING_ACTIVATE = 40  # Activate trailing at +40%
TRAILING_STOP = 15  # Trail by 15%


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
    
    async def _check_positions(self):
        """Check all positions for TP/SL triggers"""
        try:
            positions = self._load_positions()
            if not positions:
                return
                
            for token, pos in list(positions.items()):
                try:
                    # Get current price from NAD.FUN
                    current_value = await self._get_token_value(token, pos.get('amount', 0))
                    entry_value = pos.get('entry_value', pos.get('amount_mon', 0))
                    
                    if entry_value <= 0:
                        continue
                    
                    # Calculate PnL
                    pnl_percent = ((current_value - entry_value) / entry_value) * 100
                    
                    # Update position with current PnL
                    pos['current_value'] = current_value
                    pos['pnl_percent'] = pnl_percent
                    pos['last_check'] = datetime.now().isoformat()
                    
                    # Track ATH for trailing stop
                    if 'ath_value' not in pos or current_value > pos['ath_value']:
                        pos['ath_value'] = current_value
                    
                    # Check triggers
                    action = None
                    sell_percent = 0
                    reason = ""
                    
                    # 🔴 STOP LOSS
                    if pnl_percent <= config.STOP_LOSS_PERCENT:
                        action = "STOP_LOSS"
                        sell_percent = 100
                        reason = f"Stop Loss triggered at {pnl_percent:.1f}%"
                        self.log(f"🔴 {token[:10]}... STOP LOSS: {pnl_percent:.1f}%")
                    
                    # 🟢 TAKE PROFIT 1 (30% of position at +50%)
                    elif pnl_percent >= config.TP1_PERCENT and not pos.get('tp1_hit', False):
                        action = "TP1"
                        sell_percent = config.TP1_SELL_PERCENT
                        reason = f"TP1 hit at {pnl_percent:.1f}%"
                        pos['tp1_hit'] = True
                        self.log(f"🟢 {token[:10]}... TP1: +{pnl_percent:.1f}% - selling {sell_percent}%")
                    
                    # 🟢 TAKE PROFIT 2 (40% of position at +100%)
                    elif pnl_percent >= config.TP2_PERCENT and not pos.get('tp2_hit', False):
                        action = "TP2"
                        sell_percent = config.TP2_SELL_PERCENT
                        reason = f"TP2 hit at {pnl_percent:.1f}%"
                        pos['tp2_hit'] = True
                        self.log(f"🟢 {token[:10]}... TP2: +{pnl_percent:.1f}% - selling {sell_percent}%")
                    
                    # 🟡 TRAILING STOP (if we're up 40%+ and drop 20% from ATH)
                    elif pnl_percent >= 40:
                        ath = pos.get('ath_value', current_value)
                        drop_from_ath = ((ath - current_value) / ath) * 100 if ath > 0 else 0
                        
                        if drop_from_ath >= 20:
                            action = "TRAILING_STOP"
                            sell_percent = 100
                            reason = f"Trailing stop: dropped {drop_from_ath:.1f}% from ATH"
                            self.log(f"🟡 {token[:10]}... TRAILING STOP: -{drop_from_ath:.1f}% from ATH")
                    
                    # Execute sell if triggered
                    if action and sell_percent > 0:
                        await self.publish("monad:trader", Message(
                            type=MessageType.SELL_ORDER,
                            data={
                                "token": token,
                                "percent": sell_percent,
                                "reason": reason,
                                "action": action,
                                "pnl_percent": pnl_percent
                            },
                            source="position_agent"
                        ))
                    
                    # Save updated position
                    self._save_positions(positions)
                    
                except Exception as e:
                    self.log(f"Error checking position {token[:10]}...: {e}")
                    
        except Exception as e:
            self.log(f"Error in _check_positions: {e}")
    
    async def _get_token_value(self, token: str, amount: float) -> float:
        """Get current MON value of token holdings using NAD.FUN Lens"""
        try:
            import subprocess
            
            # NAD.FUN Lens contract for price queries
            LENS = "0x7e78A8DE94f21804F7a17F4E8BF9EC2c872187ea"
            RPC = "https://monad-mainnet.g.alchemy.com/v2/FPgsxxE5R86qHQ200z04i"
            
            # Convert amount to wei (18 decimals)
            amount_wei = int(amount * 10**18)
            
            # Call getAmountOut on Lens
            result = subprocess.run([
                "cast", "call", LENS,
                f"getAmountOut(address,uint256,bool)(uint256)",
                token, str(amount_wei), "true",  # true = selling tokens for MON
                "--rpc-url", RPC
            ], capture_output=True, text=True, timeout=10)
            
            if result.returncode == 0 and result.stdout.strip():
                mon_wei = int(result.stdout.strip())
                return mon_wei / 10**18
            
            return 0
            
        except Exception as e:
            self.log(f"Error getting token value: {e}")
            return 0
    
    def _load_positions(self) -> dict:
        """Load positions from file"""
        try:
            positions_file = Path("data/positions.json")
            if positions_file.exists():
                with open(positions_file) as f:
                    return json.load(f)
            return {}
        except Exception as e:
            self.log(f"Error loading positions: {e}")
            return {}
    
    def _save_positions(self, positions: dict):
        """Save positions to file"""
        try:
            positions_file = Path("data/positions.json")
            positions_file.parent.mkdir(parents=True, exist_ok=True)
            with open(positions_file, 'w') as f:
                json.dump(positions, f, indent=2, default=str)
        except Exception as e:
            self.log(f"Error saving positions: {e}")


if __name__ == "__main__":
    agent = PositionAgent()
    asyncio.run(agent.start())
