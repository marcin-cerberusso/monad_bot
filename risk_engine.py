#!/usr/bin/env python3
"""
🛡️ RISK ENGINE v2.0 - Professional Trading Risk Management

Centralne zarządzanie ryzykiem dla wszystkich agentów:
- Dynamic TP/SL based on liquidity
- Live quote slippage guard  
- Position limits per token
- Bundle/wash detection
- FOMO filter
- Gas guard
- Metrics collection
"""

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from pathlib import Path
import aiohttp
from web3 import Web3

# ═══════════════════════════════════════════════════════════════════════════════
# 📊 CONFIGURATION FROM ENV
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class RiskConfig:
    """Risk parameters loaded from ENV"""
    
    # Position limits
    max_position_pct: float = 0.10          # Max 10% of portfolio per token
    max_parallel_positions: int = 10         # Max concurrent positions
    daily_risk_cap_pct: float = 0.50         # Max 50% portfolio in risk
    min_wallet_balance_mon: float = 50.0     # Reserve balance
    
    # Slippage & price impact
    max_slippage_pct: float = 5.0            # Max allowed slippage
    max_price_impact_pct: float = 3.0        # Max price impact on entry
    min_amount_out_pct: float = 90.0         # Min amountOutMin as % of quote
    
    # Dynamic TP/SL thresholds
    low_liq_threshold: float = 5000          # USD - below = low liquidity
    med_liq_threshold: float = 20000         # USD - below = medium liquidity
    
    # TP/SL for LOW liquidity (<5k)
    low_liq_tp1_pct: float = 15.0            # Quick TP at 15%
    low_liq_tp2_pct: float = 30.0            # Second TP at 30%  
    low_liq_sl_pct: float = -10.0            # Tight stop loss
    low_liq_sizing_mult: float = 0.5         # Half position size
    
    # TP/SL for MEDIUM liquidity (5k-20k)
    med_liq_tp1_pct: float = 25.0
    med_liq_tp2_pct: float = 50.0
    med_liq_sl_pct: float = -15.0
    med_liq_sizing_mult: float = 0.75
    
    # TP/SL for HIGH liquidity (>20k)
    high_liq_tp1_pct: float = 35.0
    high_liq_tp2_pct: float = 80.0
    high_liq_sl_pct: float = -20.0
    high_liq_sizing_mult: float = 1.0
    
    # FOMO filter
    max_pump_1h_pct: float = 100.0           # Skip if >100% in 1h
    max_pump_6h_pct: float = 300.0           # Skip if >300% in 6h
    
    # Bundle/wash detection
    bundle_max_same_block: int = 5           # Max buys in same block
    bundle_min_unique_buyers: int = 3        # Min unique buyers
    wash_repeat_threshold: int = 3           # Same addr trading X times = wash
    
    # Gas guard
    max_gas_gwei: float = 100.0              # Max gas price
    gas_multiplier: float = 1.2              # Gas buffer
    
    # Scoring
    min_score: int = 65                      # Minimum score to buy
    whale_trust_threshold_mon: float = 500.0 # Trust whale above this
    
    # Trailing stop
    trailing_activation_pct: float = 20.0    # Activate after 20% profit
    trailing_drop_pct: float = 10.0          # Sell if drops 10% from ATH
    
    @classmethod
    def from_env(cls) -> 'RiskConfig':
        """Load config from environment"""
        return cls(
            max_position_pct=float(os.getenv('MAX_POSITION_PCT', '0.10')),
            max_parallel_positions=int(os.getenv('MAX_OPEN_POSITIONS', '10')),
            daily_risk_cap_pct=float(os.getenv('DAILY_RISK_CAP_PCT', '0.50')),
            min_wallet_balance_mon=float(os.getenv('MIN_WALLET_BALANCE_MON', '50.0')),
            max_slippage_pct=float(os.getenv('MAX_SLIPPAGE_PCT', '5.0')),
            max_price_impact_pct=float(os.getenv('MAX_PRICE_IMPACT_PCT', '3.0')),
            low_liq_threshold=float(os.getenv('LOW_LIQ_THRESHOLD_USD', '5000')),
            med_liq_threshold=float(os.getenv('MED_LIQ_THRESHOLD_USD', '20000')),
            max_pump_1h_pct=float(os.getenv('MAX_PUMP_1H_PCT', '100.0')),
            max_pump_6h_pct=float(os.getenv('MAX_PUMP_6H_PCT', '300.0')),
            max_gas_gwei=float(os.getenv('MAX_GAS_GWEI', '100.0')),
            min_score=int(os.getenv('MIN_BUY_SCORE', '65')),
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 📈 METRICS COLLECTOR
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class TradeMetrics:
    """Track trading performance"""
    total_entries: int = 0
    successful_entries: int = 0
    failed_entries: int = 0
    blocked_bundle: int = 0
    blocked_fomo: int = 0
    blocked_honeypot: int = 0
    blocked_low_score: int = 0
    blocked_slippage: int = 0
    blocked_gas: int = 0
    total_pnl_mon: float = 0.0
    realized_slippage_sum: float = 0.0
    trades_count: int = 0
    
    def entry_success_rate(self) -> float:
        if self.total_entries == 0:
            return 0.0
        return self.successful_entries / self.total_entries * 100
    
    def avg_slippage(self) -> float:
        if self.trades_count == 0:
            return 0.0
        return self.realized_slippage_sum / self.trades_count
    
    def to_dict(self) -> dict:
        return {
            'entry_success_rate': f'{self.entry_success_rate():.1f}%',
            'total_entries': self.total_entries,
            'successful': self.successful_entries,
            'failed': self.failed_entries,
            'blocked': {
                'bundle': self.blocked_bundle,
                'fomo': self.blocked_fomo,
                'honeypot': self.blocked_honeypot,
                'low_score': self.blocked_low_score,
                'slippage': self.blocked_slippage,
                'gas': self.blocked_gas,
            },
            'avg_slippage': f'{self.avg_slippage():.2f}%',
            'total_pnl': f'{self.total_pnl_mon:.2f} MON',
        }

# Global metrics instance
metrics = TradeMetrics()


# ═══════════════════════════════════════════════════════════════════════════════
# 💰 POSITION SIZING & LIMITS
# ═══════════════════════════════════════════════════════════════════════════════

class PositionManager:
    """Manage position sizes and limits"""
    
    def __init__(self, config: RiskConfig):
        self.config = config
        self.positions_file = Path(os.getenv('POSITIONS_FILE', 'positions.json'))
        
    def load_positions(self) -> dict:
        try:
            if self.positions_file.exists():
                return json.loads(self.positions_file.read_text())
        except:
            pass
        return {}
    
    def count_open_positions(self) -> int:
        return len(self.load_positions())
    
    def total_invested(self) -> float:
        positions = self.load_positions()
        return sum(p.get('entry_price_mon', 0) for p in positions.values())
    
    def can_open_position(self, wallet_balance: float) -> Tuple[bool, str]:
        """Check if we can open a new position"""
        
        open_count = self.count_open_positions()
        if open_count >= self.config.max_parallel_positions:
            return False, f'Max positions ({self.config.max_parallel_positions}) reached'
        
        total_portfolio = wallet_balance + self.total_invested()
        invested_pct = self.total_invested() / total_portfolio if total_portfolio > 0 else 0
        
        if invested_pct >= self.config.daily_risk_cap_pct:
            return False, f'Daily risk cap ({self.config.daily_risk_cap_pct*100:.0f}%) reached'
        
        if wallet_balance < self.config.min_wallet_balance_mon:
            return False, f'Wallet balance too low ({wallet_balance:.1f} < {self.config.min_wallet_balance_mon})'
        
        return True, 'OK'
    
    def calculate_position_size(
        self, 
        wallet_balance: float, 
        base_amount: float,
        liquidity_usd: float
    ) -> float:
        """Calculate position size based on liquidity and portfolio"""
        
        total_portfolio = wallet_balance + self.total_invested()
        
        # Max per token limit
        max_per_token = total_portfolio * self.config.max_position_pct
        
        # Liquidity-based sizing
        if liquidity_usd < self.config.low_liq_threshold:
            sizing_mult = self.config.low_liq_sizing_mult
        elif liquidity_usd < self.config.med_liq_threshold:
            sizing_mult = self.config.med_liq_sizing_mult
        else:
            sizing_mult = self.config.high_liq_sizing_mult
        
        # Also limit to 1-2% of liquidity to not move price too much
        max_liq_impact = liquidity_usd * 0.02 / 30  # Assuming 30 USD/MON
        
        # Final size = minimum of all limits
        size = min(
            base_amount * sizing_mult,
            max_per_token,
            max_liq_impact if liquidity_usd > 0 else base_amount
        )
        
        return max(size, 0.5)  # Minimum 0.5 MON
    
    def get_tp_sl_for_liquidity(self, liquidity_usd: float) -> dict:
        """Get TP/SL levels based on liquidity"""
        
        if liquidity_usd < self.config.low_liq_threshold:
            return {
                'tp1_pct': self.config.low_liq_tp1_pct,
                'tp2_pct': self.config.low_liq_tp2_pct,
                'sl_pct': self.config.low_liq_sl_pct,
                'tier': 'LOW',
            }
        elif liquidity_usd < self.config.med_liq_threshold:
            return {
                'tp1_pct': self.config.med_liq_tp1_pct,
                'tp2_pct': self.config.med_liq_tp2_pct,
                'sl_pct': self.config.med_liq_sl_pct,
                'tier': 'MEDIUM',
            }
        else:
            return {
                'tp1_pct': self.config.high_liq_tp1_pct,
                'tp2_pct': self.config.high_liq_tp2_pct,
                'sl_pct': self.config.high_liq_sl_pct,
                'tier': 'HIGH',
            }


# ═══════════════════════════════════════════════════════════════════════════════
# 🔍 SLIPPAGE & QUOTE GUARD
# ═══════════════════════════════════════════════════════════════════════════════

NADFUN_LENS = '0x7e78A8DE94f21804F7a17F4E8BF9EC2c872187ea'

async def get_live_quote(
    w3: Web3,
    token: str,
    amount_in: int,
    is_buy: bool
) -> Optional[Tuple[int, float]]:
    """Get live quote from NAD.FUN Lens
    Returns: (amountOut, price_impact_pct)
    """
    try:
        # getAmountOut(address token, uint256 amountIn, bool isBuy)
        # returns (address router, uint256 amountOut)
        lens = w3.eth.contract(
            address=Web3.to_checksum_address(NADFUN_LENS),
            abi=[{
                'inputs': [
                    {'type': 'address', 'name': 'token'},
                    {'type': 'uint256', 'name': 'amountIn'},
                    {'type': 'bool', 'name': 'isBuy'}
                ],
                'name': 'getAmountOut',
                'outputs': [
                    {'type': 'address', 'name': 'router'},
                    {'type': 'uint256', 'name': 'amountOut'}
                ],
                'stateMutability': 'view',
                'type': 'function'
            }]
        )
        
        result = lens.functions.getAmountOut(
            Web3.to_checksum_address(token),
            amount_in,
            is_buy
        ).call()
        
        amount_out = result[1]
        
        # Calculate price impact by comparing with smaller trade
        small_amount = amount_in // 100  # 1% of trade
        if small_amount > 0:
            small_result = lens.functions.getAmountOut(
                Web3.to_checksum_address(token),
                small_amount,
                is_buy
            ).call()
            small_out = small_result[1]
            
            # Expected output if linear
            expected_out = small_out * 100
            if expected_out > 0:
                price_impact = (1 - amount_out / expected_out) * 100
            else:
                price_impact = 0
        else:
            price_impact = 0
            
        return amount_out, price_impact
        
    except Exception as e:
        print(f'Quote error: {e}')
        return None


def calculate_min_amount_out(
    quote_amount: int,
    max_slippage_pct: float
) -> int:
    """Calculate amountOutMin with slippage protection"""
    slippage_factor = 1 - (max_slippage_pct / 100)
    return int(quote_amount * slippage_factor)


async def check_slippage_guard(
    w3: Web3,
    config: RiskConfig,
    token: str,
    amount_mon: float,
    is_buy: bool
) -> Tuple[bool, int, str]:
    """Check if trade passes slippage requirements
    Returns: (is_ok, min_amount_out, reason)
    """
    amount_wei = int(amount_mon * 1e18)
    
    quote_result = await get_live_quote(w3, token, amount_wei, is_buy)
    if quote_result is None:
        return False, 0, 'Failed to get quote'
    
    amount_out, price_impact = quote_result
    
    # Check price impact
    if price_impact > config.max_price_impact_pct:
        metrics.blocked_slippage += 1
        return False, 0, f'Price impact too high: {price_impact:.1f}% > {config.max_price_impact_pct}%'
    
    # Calculate min amount with slippage
    min_amount_out = calculate_min_amount_out(amount_out, config.max_slippage_pct)
    
    if min_amount_out <= 0:
        return False, 0, 'Min amount out would be 0'
    
    return True, min_amount_out, f'Quote OK: impact={price_impact:.1f}%'


# ═══════════════════════════════════════════════════════════════════════════════
# 🎯 FOMO FILTER
# ═══════════════════════════════════════════════════════════════════════════════

async def check_fomo_filter(
    http_client: aiohttp.ClientSession,
    config: RiskConfig,
    token: str
) -> Tuple[bool, str]:
    """Check if token is in FOMO territory
    Returns: (is_ok, reason)
    """
    try:
        url = f'https://api.dexscreener.com/latest/dex/tokens/{token}'
        async with http_client.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            data = await resp.json()
            
        pairs = data.get('pairs', [])
        monad_pair = next((p for p in pairs if p.get('chainId') == 'monad'), None)
        
        if not monad_pair:
            return True, 'No DEX data - new token'
        
        price_change = monad_pair.get('priceChange', {})
        change_1h = price_change.get('h1', 0) or 0
        change_6h = price_change.get('h6', 0) or 0
        
        if change_1h > config.max_pump_1h_pct:
            metrics.blocked_fomo += 1
            return False, f'FOMO: Already +{change_1h:.0f}% in 1h (max: {config.max_pump_1h_pct}%)'
        
        if change_6h > config.max_pump_6h_pct:
            metrics.blocked_fomo += 1
            return False, f'FOMO: Already +{change_6h:.0f}% in 6h (max: {config.max_pump_6h_pct}%)'
        
        return True, f'Not FOMO: +{change_1h:.0f}% 1h, +{change_6h:.0f}% 6h'
        
    except Exception as e:
        return True, f'FOMO check failed: {e} - allowing'


# ═══════════════════════════════════════════════════════════════════════════════
# 🔄 BUNDLE/WASH DETECTION
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class BlockBuyTracker:
    """Track buys within same block for bundle detection"""
    buys: Dict[str, List[str]] = field(default_factory=dict)  # token -> [buyer1, buyer2, ...]
    last_block: int = 0
    
    def add_buy(self, block_number: int, token: str, buyer: str):
        # Reset if new block
        if block_number != self.last_block:
            self.buys = {}
            self.last_block = block_number
        
        if token not in self.buys:
            self.buys[token] = []
        self.buys[token].append(buyer)
    
    def get_buy_count(self, token: str) -> int:
        return len(self.buys.get(token, []))
    
    def get_unique_buyers(self, token: str) -> int:
        return len(set(self.buys.get(token, [])))


def check_bundle_pattern(
    tracker: BlockBuyTracker,
    config: RiskConfig,
    token: str
) -> Tuple[bool, str]:
    """Check if token has bundle buying pattern
    Returns: (is_bundled, reason)
    """
    buy_count = tracker.get_buy_count(token)
    unique_buyers = tracker.get_unique_buyers(token)
    
    if buy_count > config.bundle_max_same_block:
        metrics.blocked_bundle += 1
        return True, f'BUNDLE: {buy_count} buys in same block (max: {config.bundle_max_same_block})'
    
    # Check for wash trading (same buyer multiple times)
    if buy_count > 0 and unique_buyers < buy_count:
        repeat_buys = buy_count - unique_buyers
        if repeat_buys >= config.wash_repeat_threshold:
            metrics.blocked_bundle += 1
            return True, f'WASH: {repeat_buys} repeat buys detected'
    
    return False, 'No bundle detected'


# ═══════════════════════════════════════════════════════════════════════════════
# ⛽ GAS GUARD
# ═══════════════════════════════════════════════════════════════════════════════

async def check_gas_guard(
    w3: Web3,
    config: RiskConfig
) -> Tuple[bool, int, str]:
    """Check if gas price is acceptable
    Returns: (is_ok, gas_price_wei, reason)
    """
    try:
        gas_price = w3.eth.gas_price
        gas_gwei = gas_price / 1e9
        
        if gas_gwei > config.max_gas_gwei:
            metrics.blocked_gas += 1
            return False, gas_price, f'Gas too high: {gas_gwei:.1f} gwei > {config.max_gas_gwei}'
        
        # Apply multiplier for faster execution
        boosted_gas = int(gas_price * config.gas_multiplier)
        
        return True, boosted_gas, f'Gas OK: {gas_gwei:.1f} gwei'
        
    except Exception as e:
        return True, int(50e9), f'Gas check failed: {e} - using default'


# ═══════════════════════════════════════════════════════════════════════════════
# 🚫 DEV WALLET RISK DETECTION
# ═══════════════════════════════════════════════════════════════════════════════

async def check_dev_risk(
    w3: Web3,
    http_client: aiohttp.ClientSession,
    token: str,
    creator: str = None
) -> Tuple[bool, str]:
    """Check if dev wallet poses risk (selling, approving, etc.)
    Returns: (is_safe, reason)
    """
    # If we don't know creator, skip this check
    if not creator:
        return True, 'No creator info'
    
    try:
        # Check if dev has approved large amounts (preparing to sell)
        # This would require checking Transfer/Approval events
        # For now, we'll rely on the existing honeypot check
        
        # Check dev's token balance vs total supply
        token_contract = w3.eth.contract(
            address=Web3.to_checksum_address(token),
            abi=[
                {'inputs': [{'type': 'address'}], 'name': 'balanceOf', 'outputs': [{'type': 'uint256'}], 'stateMutability': 'view', 'type': 'function'},
                {'inputs': [], 'name': 'totalSupply', 'outputs': [{'type': 'uint256'}], 'stateMutability': 'view', 'type': 'function'},
            ]
        )
        
        dev_balance = token_contract.functions.balanceOf(Web3.to_checksum_address(creator)).call()
        total_supply = token_contract.functions.totalSupply().call()
        
        if total_supply > 0:
            dev_pct = (dev_balance / total_supply) * 100
            
            if dev_pct > 50:
                return False, f'DEV RISK: Dev holds {dev_pct:.0f}% of supply'
            elif dev_pct > 30:
                return True, f'DEV WARNING: Dev holds {dev_pct:.0f}% of supply'
        
        return True, 'Dev check OK'
        
    except Exception as e:
        return True, f'Dev check failed: {e}'


# ═══════════════════════════════════════════════════════════════════════════════
# 🎯 MAIN RISK CHECK - Run all checks
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class RiskCheckResult:
    approved: bool
    position_size: float
    min_amount_out: int
    gas_price: int
    tp_sl: dict
    reasons: List[str]
    
    def to_dict(self) -> dict:
        return {
            'approved': self.approved,
            'position_size': self.position_size,
            'min_amount_out': self.min_amount_out,
            'gas_price': self.gas_price,
            'tp_sl': self.tp_sl,
            'reasons': self.reasons,
        }


async def full_risk_check(
    w3: Web3,
    http_client: aiohttp.ClientSession,
    config: RiskConfig,
    position_manager: PositionManager,
    bundle_tracker: BlockBuyTracker,
    token: str,
    wallet_balance: float,
    base_amount: float,
    liquidity_usd: float,
    score: int,
    block_number: int = 0,
    buyer: str = '',
    creator: str = None
) -> RiskCheckResult:
    """Run all risk checks and return decision"""
    
    reasons = []
    metrics.total_entries += 1
    
    # 1. Score check
    if score < config.min_score:
        metrics.blocked_low_score += 1
        return RiskCheckResult(
            approved=False,
            position_size=0,
            min_amount_out=0,
            gas_price=0,
            tp_sl={},
            reasons=[f'Score too low: {score} < {config.min_score}']
        )
    reasons.append(f'Score: {score}')
    
    # 2. Position limits
    can_open, reason = position_manager.can_open_position(wallet_balance)
    if not can_open:
        return RiskCheckResult(
            approved=False,
            position_size=0,
            min_amount_out=0,
            gas_price=0,
            tp_sl={},
            reasons=[f'Position limit: {reason}']
        )
    reasons.append('Position OK')
    
    # 3. Bundle detection
    if block_number > 0 and buyer:
        bundle_tracker.add_buy(block_number, token, buyer)
    is_bundled, bundle_reason = check_bundle_pattern(bundle_tracker, config, token)
    if is_bundled:
        return RiskCheckResult(
            approved=False,
            position_size=0,
            min_amount_out=0,
            gas_price=0,
            tp_sl={},
            reasons=[bundle_reason]
        )
    reasons.append('No bundle')
    
    # 4. FOMO filter
    fomo_ok, fomo_reason = await check_fomo_filter(http_client, config, token)
    if not fomo_ok:
        return RiskCheckResult(
            approved=False,
            position_size=0,
            min_amount_out=0,
            gas_price=0,
            tp_sl={},
            reasons=[fomo_reason]
        )
    reasons.append(fomo_reason)
    
    # 5. Dev risk check
    dev_ok, dev_reason = await check_dev_risk(w3, http_client, token, creator)
    if not dev_ok:
        metrics.blocked_honeypot += 1
        return RiskCheckResult(
            approved=False,
            position_size=0,
            min_amount_out=0,
            gas_price=0,
            tp_sl={},
            reasons=[dev_reason]
        )
    reasons.append(dev_reason)
    
    # 6. Gas guard
    gas_ok, gas_price, gas_reason = await check_gas_guard(w3, config)
    if not gas_ok:
        return RiskCheckResult(
            approved=False,
            position_size=0,
            min_amount_out=0,
            gas_price=0,
            tp_sl={},
            reasons=[gas_reason]
        )
    reasons.append(gas_reason)
    
    # 7. Calculate position size
    position_size = position_manager.calculate_position_size(
        wallet_balance, base_amount, liquidity_usd
    )
    reasons.append(f'Size: {position_size:.2f} MON')
    
    # 8. Slippage guard
    slip_ok, min_amount_out, slip_reason = await check_slippage_guard(
        w3, config, token, position_size, is_buy=True
    )
    if not slip_ok:
        return RiskCheckResult(
            approved=False,
            position_size=0,
            min_amount_out=0,
            gas_price=gas_price,
            tp_sl={},
            reasons=[slip_reason]
        )
    reasons.append(slip_reason)
    
    # 9. Get TP/SL levels
    tp_sl = position_manager.get_tp_sl_for_liquidity(liquidity_usd)
    reasons.append(f'TP/SL tier: {tp_sl["tier"]}')
    
    metrics.successful_entries += 1
    
    return RiskCheckResult(
        approved=True,
        position_size=position_size,
        min_amount_out=min_amount_out,
        gas_price=gas_price,
        tp_sl=tp_sl,
        reasons=reasons
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 📊 METRICS API
# ═══════════════════════════════════════════════════════════════════════════════

def get_metrics() -> dict:
    return metrics.to_dict()

def record_trade_pnl(pnl_mon: float, slippage_pct: float):
    metrics.total_pnl_mon += pnl_mon
    metrics.realized_slippage_sum += slippage_pct
    metrics.trades_count += 1

def save_metrics_to_file(filepath: str = 'risk_metrics.json'):
    with open(filepath, 'w') as f:
        json.dump(get_metrics(), f, indent=2)


# ═══════════════════════════════════════════════════════════════════════════════
# 🧪 TEST
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    config = RiskConfig.from_env()
    print('Risk Engine v2.0')
    print(f'Config: {config}')
    print(f'Metrics: {get_metrics()}')
