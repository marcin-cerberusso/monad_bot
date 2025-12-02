#!/usr/bin/env python3
"""
🎯 SMART ENTRY DETECTOR - Inteligentne wykrywanie momentów wejścia

Analizuje tokeny PRZED kupnem:
- Volume patterns (accumulation vs distribution)
- Whale behavior analysis
- Price momentum
- Liquidity depth
- Contract safety checks
- Social signals (jeśli dostępne)

Daje SMART SCORE 0-100 który zastępuje prosty score z whale_follower
"""

import os
import json
import asyncio
import aiohttp
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from collections import defaultdict
from dotenv import load_dotenv
from file_utils import safe_load_json, safe_save_json

load_dotenv()

# Config
MONAD_RPC_URL = os.getenv("MONAD_RPC_URL", "")
NADFUN_ROUTER = "0x6F6B8F1a20703309951a5127c45B49b1CD981A22"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")

# Paths
BASE_DIR = Path(__file__).parent
WHALE_HISTORY_FILE = BASE_DIR / "whale_history.json"
TOKEN_ANALYSIS_CACHE = BASE_DIR / "token_analysis_cache.json"

# Trusted whales (from your config)
TRUSTED_WHALES = [
    "0x37556b2c49bebf840f2bec6e3c066fb93aee7f9e",
    "0xe25386dfa2c55ff1b5f444f32e8b2d93c95a465d",
    "0x85f67cf4429d07e61280dbdddd8d3513ecd4fc7d",
    "0xe4f951030adde7c62bd0fdfcb8dee0c4d4325d8f",
]


@dataclass
class TokenAnalysis:
    """Wynik analizy tokena"""
    token_address: str
    token_name: str
    smart_score: int  # 0-100
    
    # Components
    whale_score: int  # 0-25 - jakość wielorybów kupujących
    momentum_score: int  # 0-25 - momentum cenowe
    volume_score: int  # 0-25 - wzorce volume
    safety_score: int  # 0-25 - bezpieczeństwo kontraktu
    
    # Details
    whale_count: int
    total_whale_volume: float
    price_change_1h: float
    liquidity_usd: float
    buy_sell_ratio: float
    
    # Recommendations
    recommended_action: str  # BUY / WAIT / SKIP
    confidence: float  # 0-1
    reasons: List[str]
    
    timestamp: str


def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def send_telegram(message: str):
    """Wysyła alert na Telegram"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        import requests
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }, timeout=10)
    except Exception:
        pass  # Telegram alerts are non-critical


class WhaleTracker:
    """Śledzi historię wielorybów i ich win rate"""
    
    def __init__(self):
        self.whale_history: Dict[str, Dict] = {}
        self.load_history()
    
    def load_history(self):
        """Ładuje historię wielorybów"""
        self.whale_history = safe_load_json(WHALE_HISTORY_FILE, {})
        if self.whale_history:
            print(f"Loaded whale history: {len(self.whale_history)} whales")
    
    def save_history(self):
        """Zapisuje historię"""
        if not safe_save_json(WHALE_HISTORY_FILE, self.whale_history):
            print(f"Warning: Could not save whale history")
    
    def record_whale_buy(self, whale_address: str, token: str, amount: float, result: str = "pending"):
        """Zapisuje zakup wieloryba"""
        whale = whale_address.lower()
        if whale not in self.whale_history:
            self.whale_history[whale] = {
                "total_buys": 0,
                "wins": 0,
                "losses": 0,
                "total_volume": 0,
                "recent_trades": []
            }
        
        self.whale_history[whale]["total_buys"] += 1
        self.whale_history[whale]["total_volume"] += amount
        self.whale_history[whale]["recent_trades"].append({
            "token": token,
            "amount": amount,
            "result": result,
            "timestamp": datetime.now().isoformat()
        })
        
        # Keep only last 50 trades
        self.whale_history[whale]["recent_trades"] = self.whale_history[whale]["recent_trades"][-50:]
        self.save_history()
    
    def update_result(self, whale_address: str, token: str, won: bool):
        """Aktualizuje wynik trade'a"""
        whale = whale_address.lower()
        if whale in self.whale_history:
            if won:
                self.whale_history[whale]["wins"] += 1
            else:
                self.whale_history[whale]["losses"] += 1
            self.save_history()
    
    def get_whale_score(self, whale_address: str) -> int:
        """Zwraca score wieloryba 0-100 na podstawie historii"""
        whale = whale_address.lower()
        
        # Trusted whales get bonus
        if whale in [w.lower() for w in TRUSTED_WHALES]:
            return 85
        
        if whale not in self.whale_history:
            return 50  # Unknown whale = neutral
        
        data = self.whale_history[whale]
        total = data["wins"] + data["losses"]
        
        if total < 3:
            return 50  # Not enough data
        
        win_rate = data["wins"] / total if total > 0 else 0.5
        
        # Score based on win rate
        # 70%+ win rate = 90+ score
        # 50% win rate = 50 score
        # 30% win rate = 20 score
        score = int(win_rate * 100)
        
        # Volume bonus (active whales)
        if data["total_volume"] > 10000:
            score += 10
        elif data["total_volume"] > 5000:
            score += 5
        
        return min(100, max(0, score))


class SmartEntryDetector:
    """Główna klasa analizująca tokeny"""
    
    def __init__(self):
        self.whale_tracker = WhaleTracker()
        self.token_cache: Dict[str, TokenAnalysis] = {}
        self.recent_buys: Dict[str, List[Dict]] = defaultdict(list)  # token -> list of recent buys
    
    def record_whale_buy(self, whale: str, token: str, amount_mon: float):
        """Zapisuje wykryty zakup wieloryba"""
        self.recent_buys[token.lower()].append({
            "whale": whale,
            "amount": amount_mon,
            "timestamp": time.time()
        })
        # Keep only last 5 minutes
        cutoff = time.time() - 300
        self.recent_buys[token.lower()] = [
            b for b in self.recent_buys[token.lower()] 
            if b["timestamp"] > cutoff
        ]
        self.whale_tracker.record_whale_buy(whale, token, amount_mon)
    
    async def analyze_token(self, token_address: str, token_name: str = "", 
                           trigger_whale: str = "", trigger_amount: float = 0) -> TokenAnalysis:
        """
        Pełna analiza tokena przed kupnem
        """
        token = token_address.lower()
        
        # 1. WHALE SCORE (0-25)
        whale_score = self._calculate_whale_score(token, trigger_whale, trigger_amount)
        
        # 2. MOMENTUM SCORE (0-25)
        momentum_score = await self._calculate_momentum_score(token)
        
        # 3. VOLUME SCORE (0-25)
        volume_score = self._calculate_volume_score(token)
        
        # 4. SAFETY SCORE (0-25)
        safety_score = await self._calculate_safety_score(token)
        
        # TOTAL SMART SCORE
        smart_score = whale_score + momentum_score + volume_score + safety_score
        
        # Determine action
        reasons = []
        if smart_score >= 75:
            action = "BUY"
            confidence = 0.8
            reasons.append(f"✅ High score: {smart_score}")
        elif smart_score >= 60:
            action = "BUY"
            confidence = 0.6
            reasons.append(f"👍 Good score: {smart_score}")
        elif smart_score >= 45:
            action = "WAIT"
            confidence = 0.4
            reasons.append(f"⚠️ Medium score: {smart_score}, wait for better entry")
        else:
            action = "SKIP"
            confidence = 0.7
            reasons.append(f"❌ Low score: {smart_score}")
        
        # Add component reasons
        if whale_score >= 20:
            reasons.append(f"🐳 Strong whale signal ({whale_score}/25)")
        elif whale_score < 10:
            reasons.append(f"⚠️ Weak whale signal ({whale_score}/25)")
            
        if momentum_score >= 20:
            reasons.append(f"📈 Strong momentum ({momentum_score}/25)")
        elif momentum_score < 10:
            reasons.append(f"📉 Weak momentum ({momentum_score}/25)")
        
        if volume_score >= 20:
            reasons.append(f"📊 Good volume pattern ({volume_score}/25)")
            
        if safety_score < 15:
            reasons.append(f"⚠️ Safety concerns ({safety_score}/25)")
            if action == "BUY":
                action = "WAIT"
                confidence *= 0.7
        
        # Get additional data
        recent_whale_buys = self.recent_buys.get(token, [])
        whale_count = len(set(b["whale"] for b in recent_whale_buys))
        total_whale_volume = sum(b["amount"] for b in recent_whale_buys)
        
        analysis = TokenAnalysis(
            token_address=token_address,
            token_name=token_name or f"Token {token[:8]}",
            smart_score=smart_score,
            whale_score=whale_score,
            momentum_score=momentum_score,
            volume_score=volume_score,
            safety_score=safety_score,
            whale_count=whale_count,
            total_whale_volume=total_whale_volume,
            price_change_1h=0.0,  # TODO: fetch from API
            liquidity_usd=0.0,  # TODO: fetch from API
            buy_sell_ratio=1.0,  # TODO: calculate
            recommended_action=action,
            confidence=confidence,
            reasons=reasons,
            timestamp=datetime.now().isoformat()
        )
        
        # Cache result
        self.token_cache[token] = analysis
        
        return analysis
    
    def _calculate_whale_score(self, token: str, trigger_whale: str, trigger_amount: float) -> int:
        """
        Ocenia jakość sygnału od wielorybów (0-25)
        """
        score = 0
        
        # 1. Trigger whale quality
        if trigger_whale:
            whale_quality = self.whale_tracker.get_whale_score(trigger_whale)
            score += int(whale_quality * 0.1)  # Max 10 points
        
        # 2. Multiple whales buying = stronger signal
        recent = self.recent_buys.get(token, [])
        unique_whales = len(set(b["whale"].lower() for b in recent))
        
        if unique_whales >= 3:
            score += 8  # Multiple whales = very strong
        elif unique_whales >= 2:
            score += 5
        elif unique_whales == 1:
            score += 2
        
        # 3. Trigger amount size
        if trigger_amount >= 5000:
            score += 7  # Mega whale
        elif trigger_amount >= 1000:
            score += 5
        elif trigger_amount >= 500:
            score += 3
        elif trigger_amount >= 200:
            score += 1
        
        return min(25, score)
    
    async def _calculate_momentum_score(self, token: str) -> int:
        """
        Ocenia momentum cenowe (0-25)
        """
        score = 12  # Neutral start
        
        # TODO: Fetch actual price data from NAD.FUN API
        # For now, use basic heuristics based on recent buys
        
        recent = self.recent_buys.get(token, [])
        if not recent:
            return score
        
        # If multiple buys in short time = positive momentum
        if len(recent) >= 3:
            time_span = recent[-1]["timestamp"] - recent[0]["timestamp"]
            if time_span < 60:  # 3+ buys in 1 minute
                score += 10
            elif time_span < 180:
                score += 5
        
        # Volume increasing = good
        if len(recent) >= 2:
            first_half = sum(b["amount"] for b in recent[:len(recent)//2])
            second_half = sum(b["amount"] for b in recent[len(recent)//2:])
            if second_half > first_half * 1.5:
                score += 3  # Increasing volume
        
        return min(25, max(0, score))
    
    def _calculate_volume_score(self, token: str) -> int:
        """
        Ocenia wzorce volume (0-25)
        """
        score = 12  # Neutral
        
        recent = self.recent_buys.get(token, [])
        total_volume = sum(b["amount"] for b in recent)
        
        # High recent volume = good
        if total_volume >= 5000:
            score += 10
        elif total_volume >= 2000:
            score += 7
        elif total_volume >= 500:
            score += 3
        
        # Consistent buys better than one big buy
        if len(recent) >= 3 and total_volume >= 1000:
            amounts = [b["amount"] for b in recent]
            avg = sum(amounts) / len(amounts)
            variance = sum((a - avg) ** 2 for a in amounts) / len(amounts)
            if variance < avg * avg:  # Low variance = consistent
                score += 3
        
        return min(25, max(0, score))
    
    async def _calculate_safety_score(self, token: str) -> int:
        """
        Ocenia bezpieczeństwo kontraktu (0-25)
        """
        score = 15  # Assume moderately safe by default on NAD.FUN
        
        # TODO: Add contract verification checks
        # - Check if honeypot
        # - Check token supply distribution
        # - Check if renounced
        # - Check liquidity lock
        
        # For now, basic heuristics
        recent = self.recent_buys.get(token, [])
        
        # If trusted whales are buying = safer
        trusted_buying = any(
            b["whale"].lower() in [w.lower() for w in TRUSTED_WHALES]
            for b in recent
        )
        if trusted_buying:
            score += 8
        
        # Multiple unique buyers = less likely honeypot
        unique_buyers = len(set(b["whale"].lower() for b in recent))
        if unique_buyers >= 3:
            score += 2
        
        return min(25, max(0, score))
    
    def get_entry_recommendation(self, analysis: TokenAnalysis) -> str:
        """Generuje rekomendację tekstową"""
        
        emoji_map = {"BUY": "🟢", "WAIT": "🟡", "SKIP": "🔴"}
        emoji = emoji_map.get(analysis.recommended_action, "⚪")
        
        msg = f"""
{emoji} <b>SMART ENTRY ANALYSIS</b>

🪙 <b>{analysis.token_name}</b>
📍 {analysis.token_address[:12]}...

<b>SMART SCORE: {analysis.smart_score}/100</b>

📊 Components:
  🐳 Whale: {analysis.whale_score}/25
  📈 Momentum: {analysis.momentum_score}/25
  📊 Volume: {analysis.volume_score}/25
  🛡️ Safety: {analysis.safety_score}/25

🐳 Whales buying: {analysis.whale_count}
💰 Total volume: {analysis.total_whale_volume:.0f} MON

<b>Recommendation: {analysis.recommended_action}</b>
Confidence: {analysis.confidence*100:.0f}%

Reasons:
{chr(10).join(analysis.reasons)}
"""
        return msg.strip()


# Global instance
detector = SmartEntryDetector()


async def analyze_and_decide(token_address: str, token_name: str, 
                            whale_address: str, whale_amount: float) -> Tuple[bool, TokenAnalysis]:
    """
    Main function - analizuje token i decyduje czy kupić
    
    Returns: (should_buy, analysis)
    """
    
    # Record the whale buy
    detector.record_whale_buy(whale_address, token_address, whale_amount)
    
    # Analyze
    analysis = await detector.analyze_token(
        token_address, 
        token_name,
        whale_address,
        whale_amount
    )
    
    log(f"🎯 Smart Score: {analysis.smart_score}/100 | Action: {analysis.recommended_action}")
    
    # Send telegram for interesting opportunities
    if analysis.smart_score >= 60:
        msg = detector.get_entry_recommendation(analysis)
        send_telegram(msg)
    
    should_buy = analysis.recommended_action == "BUY" and analysis.confidence >= 0.5
    
    return should_buy, analysis


async def main():
    """Test mode"""
    log("🎯 SMART ENTRY DETECTOR - Test Mode")
    log("=" * 50)
    
    # Simulate whale buys
    test_token = "0x1234567890abcdef1234567890abcdef12345678"
    test_name = "TestCoin"
    
    # Simulate multiple whales buying
    detector.record_whale_buy(TRUSTED_WHALES[0], test_token, 1500)
    await asyncio.sleep(0.1)
    detector.record_whale_buy("0xrandomwhale123", test_token, 800)
    await asyncio.sleep(0.1)
    detector.record_whale_buy(TRUSTED_WHALES[1], test_token, 2000)
    
    # Analyze
    should_buy, analysis = await analyze_and_decide(
        test_token, test_name, TRUSTED_WHALES[1], 2000
    )
    
    log("\n" + "=" * 50)
    log(detector.get_entry_recommendation(analysis))
    log("=" * 50)
    log(f"\n🎯 Should buy: {should_buy}")


if __name__ == "__main__":
    asyncio.run(main())
