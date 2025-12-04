"""
📊 POSITION AGENT - Zarządza pozycjami (TP/SL/Trailing)
"""
import asyncio
import json
import aiohttp
from datetime import datetime
from pathlib import Path
from typing import Optional
from web3 import Web3

from .base_agent import BaseAgent, Message, MessageTypes, Channels
from . import config
from .notifications import get_notifier


RPC_URL = "https://monad-mainnet.g.alchemy.com/v2/FPgsxxE5R86qHQ200z04i"
POSITIONS_FILE = Path(__file__).resolve().parent.parent / "positions.json"

# NAD.FUN Lens for sell quotes
LENS = "0x7e78A8DE94f21804F7a17F4E8BF9EC2c872187ea"
ROUTER = "0x6F6B8F1a20703309951a5127c45B49b1CD981A22"


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
                    # Get current price from NAD.FUN (pass pos for fallback)
                    current_value = await self._get_token_value(token, pos.get('amount', 0), pos)
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
                            type=MessageTypes.SELL_ORDER,
                            data={
                                "token": token,
                                "percent": sell_percent,
                                "reason": reason,
                                "action": action,
                                "pnl_percent": pnl_percent
                            },
                            sender="position_agent"
                        ))
                        
                        # Send notification
                        notifier = get_notifier()
                        await notifier.send_position_alert(
                            token=token,
                            action=action,
                            pnl=pnl_percent,
                            sell_percent=sell_percent,
                            reason=reason
                        )
                    
                    # Save updated position
                    self._save_positions(positions)
                    
                except Exception as e:
                    self.log(f"Error checking position {token[:10]}...: {e}")
                    
        except Exception as e:
            self.log(f"Error in _check_positions: {e}")
    
    async def _get_token_value(self, token: str, amount: float, pos: dict = None) -> float:
        """
        Get current MON value of token holdings.
        
        For NAD.FUN tokens, the price depends on bonding curve state.
        Since Lens calls may fail, we use entry value as fallback.
        """
        entry_value = 0
        if pos:
            entry_value = pos.get('entry_value', pos.get('amount_mon', 0))
        
        try:
            # Connect to Monad
            w3 = Web3(Web3.HTTPProvider(RPC_URL))
            
            # Get token balance
            amount_wei = int(amount * 10**18) if amount > 0 else 0
            
            if amount_wei <= 0 and pos:
                # Try to get actual balance from blockchain
                try:
                    token_contract = w3.eth.contract(
                        address=Web3.to_checksum_address(token),
                        abi=[{"constant":True,"inputs":[{"name":"account","type":"address"}],
                              "name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"type":"function"}]
                    )
                    # Get our wallet
                    wallet = "0x7b2897EA9547a6BB3c147b3E262483ddAb132A7D"
                    amount_wei = token_contract.functions.balanceOf(Web3.to_checksum_address(wallet)).call()
                except:
                    pass
            
            if amount_wei <= 0:
                # No tokens to price - return entry value
                return entry_value
            
            # === Try Lens getSellQuote ===
            # getSellQuote(address,uint256) -> returns (uint256 monOut, uint256 fee)
            method_id = "0x9c3e8f47"
            token_padded = token.lower().replace('0x', '').zfill(64)
            amount_padded = hex(amount_wei)[2:].zfill(64)
            calldata = method_id + token_padded + amount_padded
            
            try:
                result = w3.eth.call({
                    'to': Web3.to_checksum_address(LENS),
                    'data': bytes.fromhex(calldata)
                })
                
                if result and len(result) >= 32:
                    mon_wei = int(result[:32].hex(), 16)
                    if mon_wei > 0:
                        value = mon_wei / 10**18
                        self.log(f"💰 {token[:10]}... value: {value:.4f} MON (from Lens)")
                        return value
            except Exception as e:
                pass  # Lens failed, try fallback
            
            # === Fallback: use entry value ===
            # When Lens fails (token might be dead/graduated), assume entry value
            if entry_value > 0:
                self.log(f"⚠️ Using entry value for {token[:10]}... ({entry_value:.2f} MON)")
                return entry_value
            
            return 0
            
        except Exception as e:
            self.log(f"⚠️ Price check failed for {token[:10]}...: {e}")
            return entry_value if entry_value > 0 else 0
    
    def _load_positions(self) -> dict:
        """Load positions from file"""
        try:
            if POSITIONS_FILE.exists():
                with open(POSITIONS_FILE) as f:
                    return json.load(f)
            return {}
        except Exception as e:
            self.log(f"Error loading positions: {e}")
            return {}
    
    def _save_positions(self, positions: dict):
        """Save positions to file"""
        try:
            POSITIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(POSITIONS_FILE, 'w') as f:
                json.dump(positions, f, indent=2, default=str)
        except Exception as e:
            self.log(f"Error saving positions: {e}")
        except Exception as e:
            self.log(f"Error saving positions: {e}")


if __name__ == "__main__":
    agent = PositionAgent()
    asyncio.run(agent.start())
