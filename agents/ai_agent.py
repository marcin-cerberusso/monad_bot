"""
🧠 AI AGENT - DeepSeek/Gemini analiza tokenów
"""
import asyncio
import aiohttp
import os
import json
from typing import Optional, Dict
from dotenv import load_dotenv

from .base_agent import BaseAgent, Message, MessageTypes, Channels
from . import decision_logger

load_dotenv()

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")


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
    
    def _build_prompt(self, data: dict) -> str:
        """Build AI prompt"""
        return f"""You are a crypto trading analyst on NAD.FUN (Monad blockchain meme platform).

Token: {data['token']}
Whale bought: {data['amount_mon']:.1f} MON (this is a TRUSTED signal!)

Key info:
- A whale just bought this token for {data['amount_mon']:.1f} MON
- Whale buys > 500 MON are very bullish signals
- We follow whales because they have insider info
- NAD.FUN is new, no liquidity data available - IGNORE liquidity

Trading Rules:
- Max 20 MON per trade
- Follow whale if amount > 200 MON
- Bigger whale = more confidence

Respond ONLY with JSON (no other text):
{{
  "action": "BUY" or "SKIP",
  "confidence": 0-100,
  "amount_mon": 5-20,
  "reason": "brief explanation"
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
        """Fallback rule-based decision - trust whales on NAD.FUN"""
        whale_size = data.get("amount_mon", 0)
        
        # Simple whale-trust logic - no DexScreener data needed
        if whale_size < 200:
            return {"action": "SKIP", "confidence": 70, "reason": f"Whale too small: {whale_size:.0f} MON"}
        
        # Calculate amount based on whale size
        if whale_size >= 1000:
            confidence = 85
            amount = 20
            reason = f"Big whale: {whale_size:.0f} MON - strong signal!"
        elif whale_size >= 500:
            confidence = 75
            amount = 15
            reason = f"Medium whale: {whale_size:.0f} MON - good signal"
        else:
            confidence = 65
            amount = 10
            reason = f"Small whale: {whale_size:.0f} MON - cautious entry"
        
        return {
            "action": "BUY",
            "confidence": confidence,
            "amount_mon": amount,
            "reason": reason
        }


if __name__ == "__main__":
    agent = AIAgent()
    asyncio.run(agent.start())
