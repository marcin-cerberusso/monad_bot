#!/usr/bin/env python3
"""
📊 POSITION MANAGER v2.0 - Intelligent Position Management

Integruje się z:
- risk_engine.py (centralne zarządzanie ryzykiem)
- sell_executor.py (wykonywanie sprzedaży)

Features:
- Dynamic TP/SL based on liquidity tier
- Trailing stop with ATH tracking
- Partial sells (moonbag strategy)
- Live quote verification before sell
- Position health monitoring
"""

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import aiohttp
from web3 import Web3
from dotenv import load_dotenv

# Import risk engine
import sys
sys.path.insert(0, str(Path(__file__).parent))
from risk_engine import (
    RiskConfig, 
    PositionManager as RiskPositionManager,
    get_live_quote,
    calculate_min_amount_out,
    record_trade_pnl,
    save_metrics_to_file,
    metrics
)
from file_utils import safe_load_json, safe_save_json

load_dotenv()

# ═══════════════════════════════════════════════════════════════════════════════
# 📊 CONFIG
# ═══════════════════════════════════════════════════════════════════════════════

BASE_DIR = Path(__file__).parent
POSITIONS_FILE = BASE_DIR / "positions.json"
PORTFOLIO_FILE = BASE_DIR / "portfolio.json"

MONAD_RPC = os.getenv("MONAD_RPC_URL", "")
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
WALLET = os.getenv("WALLET_ADDRESS", "0x7b2897EA9547a6BB3c147b3E262483ddAb132A7D")
NADFUN_ROUTER = "0x6F6B8F1a20703309951a5127c45B49b1CD981A22"
NADFUN_LENS = "0x7e78A8DE94f21804F7a17F4E8BF9EC2c872187ea"
WMON = "0x760AfE86e5de5fa0Ee542fc7B7B713e1c5425701"

# Telegram
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")

# Check interval
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_SEC", "5"))

# Trailing stop config
TRAILING_ACTIVATION_PCT = float(os.getenv("TRAILING_ACTIVATION_PCT", "20.0"))
TRAILING_DROP_PCT = float(os.getenv("TRAILING_DROP_PCT", "10.0"))

# Partial sell config
PARTIAL_SELL_1_PCT = float(os.getenv("PARTIAL_SELL_1_PCT", "30.0"))  # Sell 30% at TP1
PARTIAL_SELL_2_PCT = float(os.getenv("PARTIAL_SELL_2_PCT", "40.0"))  # Sell 40% at TP2
MOONBAG_PCT = float(os.getenv("MOONBAG_PCT", "30.0"))  # Keep 30% as moonbag

# Min liquidity for sell
MIN_SELL_LIQUIDITY = float(os.getenv("MIN_SELL_LIQUIDITY_USD", "500.0"))

# Slippage
MAX_SELL_SLIPPAGE = float(os.getenv("MAX_SELL_SLIPPAGE_PCT", "10.0"))


# ═══════════════════════════════════════════════════════════════════════════════
# 📈 POSITION DATA
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class Position:
    """Enhanced position with trailing stop and partial sell tracking"""
    token_address: str
    token_name: str
    entry_amount_mon: float
    entry_price_per_token: float  # MON per token at entry
    entry_timestamp: int
    
    # Current state
    token_balance: int = 0
    current_value_mon: float = 0.0
    highest_value_mon: float = 0.0  # ATH for trailing stop
    
    # Liquidity tier (from risk_engine)
    liquidity_usd: float = 0.0
    liquidity_tier: str = "LOW"  # LOW, MEDIUM, HIGH
    
    # TP/SL levels (dynamic based on liquidity)
    tp1_pct: float = 15.0
    tp2_pct: float = 30.0
    sl_pct: float = -10.0
    
    # Sell tracking
    tp1_taken: bool = False
    tp2_taken: bool = False
    moonbag_only: bool = False  # Only moonbag left
    trailing_active: bool = False
    
    # Copied whale info
    copied_from: str = ""
    whale_exited: bool = False
    
    def pnl_pct(self) -> float:
        if self.entry_amount_mon <= 0:
            return 0.0
        return ((self.current_value_mon / self.entry_amount_mon) - 1) * 100
    
    def should_take_tp1(self) -> bool:
        return not self.tp1_taken and self.pnl_pct() >= self.tp1_pct
    
    def should_take_tp2(self) -> bool:
        return self.tp1_taken and not self.tp2_taken and self.pnl_pct() >= self.tp2_pct
    
    def should_stop_loss(self) -> bool:
        return self.pnl_pct() <= self.sl_pct
    
    def should_trailing_stop(self) -> bool:
        if not self.trailing_active:
            # Activate trailing after reaching activation threshold
            if self.pnl_pct() >= TRAILING_ACTIVATION_PCT:
                self.trailing_active = True
                self.highest_value_mon = self.current_value_mon
                return False
        else:
            # Update ATH
            if self.current_value_mon > self.highest_value_mon:
                self.highest_value_mon = self.current_value_mon
            
            # Check if dropped from ATH
            if self.highest_value_mon > 0:
                drop_from_ath = (1 - self.current_value_mon / self.highest_value_mon) * 100
                if drop_from_ath >= TRAILING_DROP_PCT:
                    return True
        return False
    
    def to_dict(self) -> dict:
        return {
            "token_address": self.token_address,
            "token_name": self.token_name,
            "entry_amount_mon": self.entry_amount_mon,
            "entry_price_per_token": self.entry_price_per_token,
            "entry_timestamp": self.entry_timestamp,
            "token_balance": self.token_balance,
            "current_value_mon": self.current_value_mon,
            "highest_value_mon": self.highest_value_mon,
            "liquidity_usd": self.liquidity_usd,
            "liquidity_tier": self.liquidity_tier,
            "tp1_pct": self.tp1_pct,
            "tp2_pct": self.tp2_pct,
            "sl_pct": self.sl_pct,
            "tp1_taken": self.tp1_taken,
            "tp2_taken": self.tp2_taken,
            "moonbag_only": self.moonbag_only,
            "trailing_active": self.trailing_active,
            "copied_from": self.copied_from,
            "whale_exited": self.whale_exited,
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> 'Position':
        return cls(
            token_address=data["token_address"],
            token_name=data.get("token_name", "Unknown"),
            entry_amount_mon=data.get("entry_amount_mon", data.get("amount_mon", 0)),
            entry_price_per_token=data.get("entry_price_per_token", 0),
            entry_timestamp=data.get("entry_timestamp", data.get("timestamp", 0)),
            token_balance=data.get("token_balance", 0),
            current_value_mon=data.get("current_value_mon", 0),
            highest_value_mon=data.get("highest_value_mon", 0),
            liquidity_usd=data.get("liquidity_usd", 0),
            liquidity_tier=data.get("liquidity_tier", "LOW"),
            tp1_pct=data.get("tp1_pct", 15),
            tp2_pct=data.get("tp2_pct", 30),
            sl_pct=data.get("sl_pct", -10),
            tp1_taken=data.get("tp1_taken", data.get("tp_level_1_taken", False)),
            tp2_taken=data.get("tp2_taken", data.get("tp_level_2_taken", False)),
            moonbag_only=data.get("moonbag_only", data.get("moonbag_secured", False)),
            trailing_active=data.get("trailing_active", False),
            copied_from=data.get("copied_from", ""),
            whale_exited=data.get("whale_exited", False),
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 💰 POSITION MANAGER
# ═══════════════════════════════════════════════════════════════════════════════

class AdvancedPositionManager:
    """Advanced position manager with trailing stop and partial sells"""
    
    def __init__(self):
        self.positions: Dict[str, Position] = {}
        self.w3 = Web3(Web3.HTTPProvider(MONAD_RPC))
        self.config = RiskConfig.from_env()
        self.risk_pm = RiskPositionManager(self.config)
        self.http_session: Optional[aiohttp.ClientSession] = None
        
        self._load_positions()
    
    def _load_positions(self):
        """Load positions from file"""
        data = safe_load_json(POSITIONS_FILE, {})
        for addr, pos_data in data.items():
            # Handle old format
            if 'token_address' not in pos_data:
                pos_data['token_address'] = addr
            self.positions[addr] = Position.from_dict(pos_data)
    
    def _save_positions(self):
        """Save positions to file"""
        data = {addr: pos.to_dict() for addr, pos in self.positions.items()}
        safe_save_json(POSITIONS_FILE, data)
    
    async def get_token_balance(self, token: str) -> int:
        """Get token balance"""
        try:
            contract = self.w3.eth.contract(
                address=Web3.to_checksum_address(token),
                abi=[{
                    'inputs': [{'type': 'address'}],
                    'name': 'balanceOf',
                    'outputs': [{'type': 'uint256'}],
                    'stateMutability': 'view',
                    'type': 'function'
                }]
            )
            return contract.functions.balanceOf(Web3.to_checksum_address(WALLET)).call()
        except Exception as e:
            print(f"Error getting balance: {e}")
            return 0
    
    async def get_token_value(self, token: str, balance: int) -> Tuple[float, float]:
        """Get token value in MON and liquidity
        Returns: (value_mon, liquidity_usd)
        """
        if balance == 0:
            return 0.0, 0.0
        
        try:
            # Get quote from Lens
            quote_result = await get_live_quote(self.w3, token, balance, is_buy=False)
            if quote_result:
                value_mon = quote_result[0] / 1e18
            else:
                value_mon = 0.0
            
            # Get liquidity from DexScreener
            liquidity_usd = 0.0
            if self.http_session:
                try:
                    url = f'https://api.dexscreener.com/latest/dex/tokens/{token}'
                    async with self.http_session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                        data = await resp.json()
                    pairs = data.get('pairs', [])
                    monad_pair = next((p for p in pairs if p.get('chainId') == 'monad'), None)
                    if monad_pair:
                        liquidity_usd = monad_pair.get('liquidity', {}).get('usd', 0) or 0
                except:
                    pass
            
            return value_mon, liquidity_usd
            
        except Exception as e:
            print(f"Error getting value: {e}")
            return 0.0, 0.0
    
    async def update_position(self, pos: Position) -> Position:
        """Update position with current values"""
        balance = await self.get_token_balance(pos.token_address)
        pos.token_balance = balance
        
        value_mon, liquidity_usd = await self.get_token_value(pos.token_address, balance)
        pos.current_value_mon = value_mon
        pos.liquidity_usd = liquidity_usd
        
        # Update ATH
        if value_mon > pos.highest_value_mon:
            pos.highest_value_mon = value_mon
        
        # Update TP/SL based on liquidity
        tp_sl = self.risk_pm.get_tp_sl_for_liquidity(liquidity_usd)
        pos.tp1_pct = tp_sl['tp1_pct']
        pos.tp2_pct = tp_sl['tp2_pct']
        pos.sl_pct = tp_sl['sl_pct']
        pos.liquidity_tier = tp_sl['tier']
        
        return pos
    
    async def execute_sell(
        self, 
        pos: Position, 
        sell_pct: float, 
        reason: str
    ) -> Tuple[bool, str, float]:
        """Execute partial or full sell
        Returns: (success, tx_hash, amount_received_mon)
        """
        if pos.token_balance == 0:
            return False, "", 0.0
        
        sell_amount = int(pos.token_balance * (sell_pct / 100))
        if sell_amount == 0:
            return False, "", 0.0
        
        # Get quote for slippage protection
        quote_result = await get_live_quote(self.w3, pos.token_address, sell_amount, is_buy=False)
        if not quote_result:
            print(f"❌ Failed to get quote for {pos.token_address}")
            return False, "", 0.0
        
        expected_out, price_impact = quote_result
        if price_impact > MAX_SELL_SLIPPAGE:
            print(f"⚠️ Price impact too high: {price_impact:.1f}% - selling smaller portion")
            # Try selling half
            sell_amount = sell_amount // 2
            quote_result = await get_live_quote(self.w3, pos.token_address, sell_amount, is_buy=False)
            if not quote_result:
                return False, "", 0.0
            expected_out, price_impact = quote_result
        
        min_amount_out = calculate_min_amount_out(expected_out, MAX_SELL_SLIPPAGE)
        
        try:
            from eth_account import Account
            
            account = Account.from_key(PRIVATE_KEY)
            
            # Build sell transaction via NAD.FUN Router
            # sell((uint256 amountIn, uint256 amountOutMin, address token, address to, uint256 deadline))
            router = self.w3.eth.contract(
                address=Web3.to_checksum_address(NADFUN_ROUTER),
                abi=[{
                    'inputs': [{
                        'components': [
                            {'name': 'amountIn', 'type': 'uint256'},
                            {'name': 'amountOutMin', 'type': 'uint256'},
                            {'name': 'token', 'type': 'address'},
                            {'name': 'to', 'type': 'address'},
                            {'name': 'deadline', 'type': 'uint256'}
                        ],
                        'name': 'params',
                        'type': 'tuple'
                    }],
                    'name': 'sell',
                    'outputs': [],
                    'stateMutability': 'nonpayable',
                    'type': 'function'
                }]
            )
            
            deadline = int(time.time()) + 300  # 5 min
            
            # First approve if needed
            token_contract = self.w3.eth.contract(
                address=Web3.to_checksum_address(pos.token_address),
                abi=[
                    {'inputs': [{'type': 'address'}, {'type': 'uint256'}], 'name': 'approve', 'outputs': [{'type': 'bool'}], 'stateMutability': 'nonpayable', 'type': 'function'},
                    {'inputs': [{'type': 'address'}, {'type': 'address'}], 'name': 'allowance', 'outputs': [{'type': 'uint256'}], 'stateMutability': 'view', 'type': 'function'}
                ]
            )
            
            allowance = token_contract.functions.allowance(WALLET, NADFUN_ROUTER).call()
            if allowance < sell_amount:
                nonce = self.w3.eth.get_transaction_count(WALLET)
                approve_tx = token_contract.functions.approve(
                    NADFUN_ROUTER, 
                    2**256 - 1  # Max approval
                ).build_transaction({
                    'from': WALLET,
                    'gas': 100000,
                    'gasPrice': self.w3.eth.gas_price,
                    'nonce': nonce
                })
                signed = account.sign_transaction(approve_tx)
                tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
                self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            
            # Execute sell
            nonce = self.w3.eth.get_transaction_count(WALLET)
            sell_params = (
                sell_amount,
                min_amount_out,
                Web3.to_checksum_address(pos.token_address),
                WALLET,
                deadline
            )
            
            sell_tx = router.functions.sell(sell_params).build_transaction({
                'from': WALLET,
                'gas': 300000,
                'gasPrice': int(self.w3.eth.gas_price * 1.2),
                'nonce': nonce
            })
            
            signed = account.sign_transaction(sell_tx)
            tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            
            if receipt['status'] == 1:
                amount_received = expected_out / 1e18
                
                # Record metrics
                pnl = amount_received - (pos.entry_amount_mon * sell_pct / 100)
                record_trade_pnl(pnl, price_impact)
                
                # Send telegram notification
                await self._send_telegram(
                    f"💰 <b>SELL {reason}</b>\n"
                    f"Token: {pos.token_name}\n"
                    f"Sold: {sell_pct:.0f}%\n"
                    f"Received: {amount_received:.2f} MON\n"
                    f"P&L: {pos.pnl_pct():.1f}%\n"
                    f"TX: {tx_hash.hex()[:20]}..."
                )
                
                return True, tx_hash.hex(), amount_received
            else:
                print(f"❌ Sell TX failed: {tx_hash.hex()}")
                return False, tx_hash.hex(), 0.0
                
        except Exception as e:
            print(f"❌ Sell error: {e}")
            return False, "", 0.0
    
    async def _send_telegram(self, message: str):
        """Send telegram notification"""
        if not TG_TOKEN or not TG_CHAT:
            return
        try:
            if self.http_session:
                url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
                await self.http_session.post(url, data={
                    "chat_id": TG_CHAT,
                    "text": message,
                    "parse_mode": "HTML"
                })
        except:
            pass
    
    async def check_and_manage_position(self, pos: Position) -> Optional[str]:
        """Check position and execute sells if needed
        Returns: action taken or None
        """
        pos = await self.update_position(pos)
        
        # Skip if no balance
        if pos.token_balance == 0:
            return "EMPTY"
        
        # Skip if no liquidity (can't sell)
        if pos.liquidity_usd < MIN_SELL_LIQUIDITY:
            print(f"⚠️ {pos.token_name}: Low liquidity ${pos.liquidity_usd:.0f} - skipping")
            return None
        
        pnl = pos.pnl_pct()
        action = None
        
        # Check stop loss first
        if pos.should_stop_loss():
            print(f"🔴 {pos.token_name}: STOP LOSS at {pnl:.1f}%")
            success, _, _ = await self.execute_sell(pos, 100, "STOP_LOSS")
            if success:
                del self.positions[pos.token_address]
                action = "STOP_LOSS"
        
        # Check trailing stop
        elif pos.should_trailing_stop():
            print(f"📉 {pos.token_name}: TRAILING STOP triggered")
            success, _, _ = await self.execute_sell(pos, 100, "TRAILING_STOP")
            if success:
                del self.positions[pos.token_address]
                action = "TRAILING_STOP"
        
        # Check TP1 (partial sell)
        elif pos.should_take_tp1():
            print(f"🟢 {pos.token_name}: TP1 at {pnl:.1f}%")
            success, _, _ = await self.execute_sell(pos, PARTIAL_SELL_1_PCT, "TP1")
            if success:
                pos.tp1_taken = True
                action = "TP1"
        
        # Check TP2 (partial sell, leave moonbag)
        elif pos.should_take_tp2():
            print(f"🟢🟢 {pos.token_name}: TP2 at {pnl:.1f}%")
            success, _, _ = await self.execute_sell(pos, PARTIAL_SELL_2_PCT, "TP2")
            if success:
                pos.tp2_taken = True
                pos.moonbag_only = True
                action = "TP2"
        
        # Save after any action
        if action:
            self._save_positions()
        
        return action
    
    async def run(self):
        """Main loop"""
        print("📊 Position Manager v2.0 started")
        print(f"Trailing: activate at +{TRAILING_ACTIVATION_PCT}%, drop {TRAILING_DROP_PCT}%")
        print(f"Partial sells: TP1={PARTIAL_SELL_1_PCT}%, TP2={PARTIAL_SELL_2_PCT}%, Moonbag={MOONBAG_PCT}%")
        
        async with aiohttp.ClientSession() as session:
            self.http_session = session
            
            while True:
                try:
                    if not self.positions:
                        print("No positions to manage")
                    else:
                        print(f"\n{'='*60}")
                        print(f"Checking {len(self.positions)} positions...")
                        
                        for addr, pos in list(self.positions.items()):
                            try:
                                action = await self.check_and_manage_position(pos)
                                
                                if action:
                                    print(f"  {pos.token_name}: {action}")
                                else:
                                    pnl = pos.pnl_pct()
                                    emoji = "🟢" if pnl > 0 else "🔴"
                                    tier_emoji = {"LOW": "🟡", "MEDIUM": "🟠", "HIGH": "🟢"}.get(pos.liquidity_tier, "⚪")
                                    print(f"  {emoji} {pos.token_name}: {pnl:+.1f}% | {tier_emoji} {pos.liquidity_tier} | ATH: {pos.highest_value_mon:.2f}")
                                    
                            except Exception as e:
                                print(f"  ❌ Error checking {pos.token_name}: {e}")
                        
                        # Save metrics periodically
                        save_metrics_to_file()
                    
                except Exception as e:
                    print(f"❌ Loop error: {e}")
                
                await asyncio.sleep(CHECK_INTERVAL)


# ═══════════════════════════════════════════════════════════════════════════════
# 🚀 MAIN
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    pm = AdvancedPositionManager()
    asyncio.run(pm.run())
