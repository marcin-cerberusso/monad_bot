"""
🧠 AI AGENT - DeepSeek/Gemini analiza tokenów
+ Integracja z LeverUp dla wysokiego confidence
"""
import asyncio
import aiohttp
import os
import json
from typing import Optional, Dict
from dotenv import load_dotenv

from .base_agent import BaseAgent, Message, MessageTypes, Channels
from . import decision_logger
from . import config
from .leverage_agent import LeverageConfig, LeverageAgent, should_use_leverage

load_dotenv()

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Global leverage agent instance
_leverage_agent: Optional[LeverageAgent] = None


def get_leverage_agent() -> Optional[LeverageAgent]:
    """Lazy initialization of LeverageAgent"""
    global _leverage_agent
    if _leverage_agent is None and LeverageConfig.ENABLED:
        _leverage_agent = LeverageAgent()
    return _leverage_agent


class AIAgent(BaseAgent):
    """Agent AI do analizy tokenów"""
    
    def __init__(self, redis_url: str = "redis://localhost:6379"):
        super().__init__("AIAgent", redis_url)
        self.use_deepseek = bool(DEEPSEEK_API_KEY)
        self.use_gemini = bool(GEMINI_API_KEY)
        
    async def run(self):
        """Subscribe to AI channel"""
        await self.subscribe(Channels.AI)
        self.log(f"AI ready (DeepSeek: {self.use_deepseek}, Gemini: {self.use_gemini})")
        
        while self.running:
            await asyncio.sleep(1)
    
    async def on_message(self, message: Message):
        """Handle AI analysis requests"""
        if message.type == MessageTypes.AI_ANALYZE:
            await self._analyze(message.data)
    
    async def _analyze(self, data: dict):
        """AI analysis of token"""
        token = data["token"]
        whale_amount = data["amount_mon"]
        liquidity = data.get("liquidity_usd", 0)
        tax = data.get("tax_percent", 0)
        pump_1h = data.get("pump_1h", 0)
        
        self.log(f"Analyzing {token[:12]}...")
        
        # Build prompt
        prompt = self._build_prompt(data)
        
        # Get AI decision
        decision = await self._get_ai_decision(prompt)
        
        if decision is None:
            # Fallback to simple rules
            decision = self._rule_based_decision(data)
        
        self.log(f"  AI Decision: {decision['action']} (confidence: {decision['confidence']}%)")
        self.log(f"  Reason: {decision['reason'][:50]}...")
        
        # Log for ML
        decision_logger.log_ai_decision(token, decision, data)
        
        if decision["action"] == "BUY":
            # Send buy order to trader
            await self.publish(Channels.TRADER, Message(
                type=MessageTypes.BUY_ORDER,
                data={
                    **data,
                    "ai_decision": decision,
                    "suggested_amount": decision.get("amount_mon", 20)
                },
                sender=self.name
            ))
            
            # === LEVERAGE: Wysyłaj do LeverageAgent gdy confidence >= 85% ===
            ai_decision_for_leverage = {
                "decision": "BUY",
                "confidence": decision.get("confidence", 0),
                "token_symbol": data.get("token_symbol", "MON")
            }
            if should_use_leverage(ai_decision_for_leverage):
                self.log(f"  🔥 HIGH CONFIDENCE ({decision['confidence']}%) - opening leverage position")
                leverage_agent = get_leverage_agent()
                if leverage_agent and leverage_agent.config.ENABLED:
                    try:
                        await leverage_agent.handle_signal({
                            "token": ai_decision_for_leverage["token_symbol"],
                            "direction": "long",
                            "confidence": decision["confidence"]
                        })
                    except Exception as e:
                        self.log(f"  ⚠️ Leverage error: {e}")
    
    def _build_prompt(self, data: dict) -> str:
        """Build AI prompt"""
        return f"""You are a crypto trading analyst. Analyze this token opportunity:

Token: {data['token']}
Whale bought: {data['amount_mon']:.1f} MON
Tax/Slippage: {data.get('tax_percent', 0):.1f}%
Liquidity: ${data.get('liquidity_usd', 0):.0f}
1h Pump: {data.get('pump_1h', 0):.1f}%

Rules:
- Max 20 MON per trade
- Skip if tax > 15%
- Skip if already pumped > 100%
- Higher liquidity = safer
- Whale following works best on fresh tokens

Respond in JSON:
{{
  "action": "BUY" or "SKIP",
  "confidence": 0-100,
  "amount_mon": 5-20 (if BUY),
  "reason": "explanation"
}}"""
    
    async def _get_ai_decision(self, prompt: str) -> Optional[Dict]:
        """Get decision from AI"""
        try:
            if self.use_deepseek:
                return await self._call_deepseek(prompt)
            elif self.use_gemini:
                return await self._call_gemini(prompt)
        except Exception as e:
            self.log(f"  AI error: {e}")
        return None
    
    async def _call_deepseek(self, prompt: str) -> Optional[Dict]:
        """Call DeepSeek API"""
        async with aiohttp.ClientSession() as session:
            url = "https://api.deepseek.com/v1/chat/completions"
            headers = {
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                "Content-Type": "application/json"
            }
            payload = {
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
                "max_tokens": 200
            }
            
            async with session.post(url, headers=headers, json=payload, timeout=15) as resp:
                data = await resp.json()
                content = data["choices"][0]["message"]["content"]
                
                # Parse JSON from response
                start = content.find("{")
                end = content.rfind("}") + 1
                if start >= 0 and end > start:
                    return json.loads(content[start:end])
        return None
    
    async def _call_gemini(self, prompt: str) -> Optional[Dict]:
        """Call Gemini API"""
        async with aiohttp.ClientSession() as session:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
            payload = {
                "contents": [{"parts": [{"text": prompt}]}]
            }
            
            async with session.post(url, json=payload, timeout=15) as resp:
                data = await resp.json()
                content = data["candidates"][0]["content"]["parts"][0]["text"]
                
                start = content.find("{")
                end = content.rfind("}") + 1
                if start >= 0 and end > start:
                    return json.loads(content[start:end])
        return None
    
    def _rule_based_decision(self, data: dict) -> Dict:
        """Fallback rule-based decision"""
        tax = data.get("tax_percent", 100)
        liq = data.get("liquidity_usd", 0)
        pump = data.get("pump_1h", 0)
        whale_size = data.get("amount_mon", 0)
        
        # Skip conditions
        if tax > 15:
            return {"action": "SKIP", "confidence": 90, "reason": f"Tax too high: {tax:.1f}%"}
        if liq < 1000:
            return {"action": "SKIP", "confidence": 85, "reason": f"Low liquidity: ${liq:.0f}"}
        if pump > 100:
            return {"action": "SKIP", "confidence": 80, "reason": f"Already pumped: +{pump:.0f}%"}
        
        # Calculate amount based on conditions
        confidence = 70
        amount = 10
        
        if whale_size > 500:
            confidence += 10
            amount = 15
        if liq > 10000:
            confidence += 10
            amount = 20
        if tax < 5:
            confidence += 5
        
        return {
            "action": "BUY",
            "confidence": min(confidence, 95),
            "amount_mon": min(amount, 20),
            "reason": f"Whale {whale_size:.0f} MON, liq ${liq:.0f}, tax {tax:.1f}%"
        }


if __name__ == "__main__":
    agent = AIAgent()
    asyncio.run(agent.start())
