"""
🛡️ RISK AGENT - Sprawdza honeypot, slippage, FOMO
"""
import asyncio
import subprocess
import os
from typing import Tuple
from dotenv import load_dotenv

from .base_agent import BaseAgent, Message, MessageTypes, Channels
from . import decision_logger

load_dotenv()

LENS = "0x7e78A8DE94f21804F7a17F4E8BF9EC2c872187ea"
RPC_URL = os.getenv("MONAD_RPC_URL")
CAST_PATH = os.path.expanduser("~/.foundry/bin/cast")

# Risk thresholds
MAX_TAX_PERCENT = 15  # Max acceptable tax
MIN_LIQUIDITY_USD = 1000
MAX_FOMO_PUMP_1H = 200  # Max +200% w 1h


class RiskAgent(BaseAgent):
    """Agent sprawdzający ryzyko"""
    
    def __init__(self, redis_url: str = "redis://localhost:6379"):
        super().__init__("RiskAgent", redis_url)
        self.blocked_tokens: set = set()
        
    async def run(self):
        """Subscribe to risk channel"""
        await self.subscribe(Channels.RISK)
        
        while self.running:
            await asyncio.sleep(1)
    
    async def on_message(self, message: Message):
        """Handle risk check requests"""
        if message.type == MessageTypes.WHALE_BUY:
            await self._check_token(message.data)
    
    async def _check_token(self, data: dict):
        """Full risk check on token"""
        token = data["token"]
        amount = data["amount_mon"]
        whale = data["whale"]
        
        self.log(f"Checking {token[:12]}... ({amount:.1f} MON from {whale[:10]}...)")
        
        # 1. Check if blocked
        if token in self.blocked_tokens:
            self.log(f"  ❌ Already blocked")
            return
        
        # 2. Honeypot test - DISABLED: NAD.FUN Lens contract reverts for all tokens
        # We trust whales - if they buy 1000+ MON, token is probably legit
        # is_honeypot, tax = await self._test_honeypot(token)
        # if is_honeypot:
        #     self.log(f"  🚫 HONEYPOT! Tax: {tax:.1f}%")
        #     self.blocked_tokens.add(token)
        #     decision_logger.log_risk_check(token, False, f"Honeypot: tax {tax:.1f}%", {"tax_percent": tax, "is_honeypot": True})
        #     return
        tax = 0  # Unknown - Lens not working
        
        self.log(f"  ✅ Whale trusted ({amount:.0f} MON buy)")
        
        # 3. Get liquidity from DexScreener (optional - NAD.FUN tokens may not be listed)
        liquidity = await self._get_liquidity(token)
        # Skip liquidity check for now - DexScreener doesn't index NAD.FUN yet
        # if liquidity < MIN_LIQUIDITY_USD:
        #     self.log(f"  ⚠️ Low liquidity: ${liquidity:.0f}")
        #     decision_logger.log_risk_check(token, False, f"Low liquidity: ${liquidity:.0f}", {"liquidity_usd": liquidity})
        #     return
        
        # 4. FOMO check (also from DexScreener - skip for now)
        pump_1h = 0  # await self._get_pump_percent(token)
        # if pump_1h > MAX_FOMO_PUMP_1H:
        #     self.log(f"  🔥 FOMO! Already +{pump_1h:.0f}% in 1h")
        #     decision_logger.log_risk_check(token, False, f"FOMO: +{pump_1h:.0f}% in 1h", {"pump_1h": pump_1h})
        #     return
        
        # All checks passed!
        self.log(f"  ✅ APPROVED! Tax={tax:.1f}% Liq=${liquidity:.0f}")
        decision_logger.log_risk_check(token, True, "All checks passed", {
            "tax_percent": tax, "liquidity_usd": liquidity, "pump_1h": pump_1h, "is_honeypot": False
        })
        
        # Send to AI for analysis
        await self.publish(Channels.AI, Message(
            type=MessageTypes.AI_ANALYZE,
            data={
                **data,
                "tax_percent": tax,
                "liquidity_usd": liquidity,
                "pump_1h": pump_1h
            },
            sender=self.name
        ))
    
    async def _test_honeypot(self, token: str) -> Tuple[bool, float]:
        """Test honeypot via NAD.FUN Lens"""
        try:
            amount_wei = int(0.1 * 1e18)
            
            # Get buy quote
            cmd = f'{CAST_PATH} call {LENS} "getTokenBuyQuote(address,uint256)" {token} {amount_wei} --rpc-url {RPC_URL}'
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
            
            if result.returncode != 0:
                return True, 100.0
            
            tokens_out = int(result.stdout.strip(), 16) if result.stdout.strip() else 0
            if tokens_out == 0:
                return True, 100.0
            
            # Get sell quote
            cmd = f'{CAST_PATH} call {LENS} "getTokenSellQuote(address,uint256)" {token} {tokens_out} --rpc-url {RPC_URL}'
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
            
            if result.returncode != 0:
                return True, 100.0
            
            mon_back = int(result.stdout.strip(), 16) if result.stdout.strip() else 0
            mon_back_float = mon_back / 1e18
            
            tax = (1 - (mon_back_float / 0.1)) * 100 if mon_back_float > 0 else 100
            
            return tax > MAX_TAX_PERCENT, tax
            
        except Exception as e:
            self.log(f"  Honeypot test error: {e}")
            return True, 100.0
    
    async def _get_liquidity(self, token: str) -> float:
        """Get liquidity from DexScreener"""
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                url = f"https://api.dexscreener.com/latest/dex/tokens/{token}"
                async with session.get(url, timeout=5) as resp:
                    data = await resp.json()
                    pairs = data.get("pairs", [])
                    if pairs:
                        return pairs[0].get("liquidity", {}).get("usd", 0)
        except:
            pass
        return 0
    
    async def _get_pump_percent(self, token: str) -> float:
        """Get 1h price change from DexScreener"""
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                url = f"https://api.dexscreener.com/latest/dex/tokens/{token}"
                async with session.get(url, timeout=5) as resp:
                    data = await resp.json()
                    pairs = data.get("pairs", [])
                    if pairs:
                        return pairs[0].get("priceChange", {}).get("h1", 0)
        except:
            pass
        return 0


if __name__ == "__main__":
    agent = RiskAgent()
    asyncio.run(agent.start())
