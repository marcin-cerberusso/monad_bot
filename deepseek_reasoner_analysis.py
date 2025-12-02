#!/usr/bin/env python3
"""
🧠 DeepSeek Reasoner Analysis - Special Recovery Mode
Uses deepseek-reasoner model for deep analysis
"""

import os
import json
import requests
from dotenv import load_dotenv

load_dotenv()

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")

# Current portfolio data
PORTFOLIO = """
WALLET BALANCE: 5.59 MON (prawie pusty!)
ZAINWESTOWANE W POZYCJE: 1237.50 MON

31 OTWARTYCH POZYCJI (posortowane od najlepszych):
1. 0x973eb1 | 63.42 MON (+1.5%) | entry: 62.5 | ATH drop: 0%
2. 0xa9cc72 | 61.64 MON (-1.4%) | entry: 62.5 | ATH drop: 0%
3. 0x9778b2 | 59.81 MON (-4.3%) | entry: 62.5 | ATH drop: 3%
4. 0x8f912e | 37.88 MON (+1.0%) | entry: 37.5 | ATH drop: 0%
5. 0x25b912 | 37.56 MON (+0.2%) | entry: 37.5 | ATH drop: 10.3%
6-15. 10 pozycji | ~36.5 MON (-2% do -3%) | entry: 37.5
16-25. 10 pozycji | ~35.5 MON (-5% do -7%) | entry: 37.5
26-30. 5 pozycji | ~34.5 MON (-7% do -8%) | entry: 37.5
31. 0xdc5407 | 33.20 MON (-11.5%) | entry: 37.5 | ATH drop: 14.6% <- NAJGORSZA

HISTORIA DZISIEJSZYCH TRADÓW:
- Win rate: 18.5% (10 wins / 44 losses)
- Total closed P&L: -208 MON
- Średni win: +17.3 MON
- Średni loss: -8.7 MON

PROBLEM:
- Whale Follower kupował zbyt agresywnie (31 pozycji!)
- Większość tokeny meme z NAD.FUN (niska płynność)
- Position Manager ma hard stop na -12%

PYTANIA DO AI:
1. Czy sprzedać wszystko teraz i odzyskać ~1100 MON?
2. Czy trzymać i czekać na odbicie?
3. Które pozycje sprzedać, a które trzymać?
4. Jaka jest optymalna strategia wyjścia?
"""

def ask_deepseek_reasoner(prompt):
    """Query DeepSeek Reasoner for deep analysis"""
    url = "https://api.deepseek.com/v1/chat/completions"
    
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json"
    }
    
    data = {
        "model": "deepseek-reasoner",  # New reasoning model!
        "messages": [
            {
                "role": "system",
                "content": """Jesteś ekspertem od tradingu memecoinów i zarządzania ryzykiem.
Analizujesz portfolio i dajesz konkretne rekomendacje.
Odpowiadaj po polsku. Bądź konkretny i podawaj liczby."""
            },
            {
                "role": "user",
                "content": prompt
            }
        ],
        "temperature": 0.7,
        "max_tokens": 2000
    }
    
    try:
        response = requests.post(url, headers=headers, json=data, timeout=120)
        response.raise_for_status()
        result = response.json()
        
        # DeepSeek Reasoner returns reasoning_content + content
        message = result.get("choices", [{}])[0].get("message", {})
        
        reasoning = message.get("reasoning_content", "")
        answer = message.get("content", "")
        
        return reasoning, answer
        
    except Exception as e:
        print(f"❌ Error: {e}")
        return None, None

def main():
    print("=" * 70)
    print("🧠 DeepSeek REASONER - Portfolio Recovery Analysis")
    print("=" * 70)
    
    prompt = f"""Przeanalizuj moje portfolio memecoinów i daj konkretne rekomendacje:

{PORTFOLIO}

Daj mi:
1. NATYCHMIASTOWĄ AKCJĘ - co zrobić TERAZ
2. PLAN WYJŚCIA - krok po kroku
3. KTÓRE POZYCJE SPRZEDAĆ od razu (lista adresów)
4. KTÓRE TRZYMAĆ i dlaczego
5. PROGNOZĘ - ile mogę realnie odzyskać

Odpowiedz w formacie JSON:
{{
    "immediate_action": "...",
    "sell_now": ["0x...", "0x..."],
    "hold": ["0x...", "0x..."],
    "expected_recovery_mon": 1100,
    "reasoning": "..."
}}
"""
    
    print("\n📡 Wysyłam do DeepSeek Reasoner...")
    reasoning, answer = ask_deepseek_reasoner(prompt)
    
    if reasoning:
        print("\n" + "=" * 70)
        print("🧠 REASONING (Chain of Thought):")
        print("=" * 70)
        print(reasoning[:2000])  # First 2000 chars of reasoning
        
    if answer:
        print("\n" + "=" * 70)
        print("💡 FINAL ANSWER:")
        print("=" * 70)
        print(answer)
    else:
        print("❌ Nie udało się uzyskać odpowiedzi")

if __name__ == "__main__":
    main()
