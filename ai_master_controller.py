#!/usr/bin/env python3
"""
🧠 AI MASTER CONTROLLER - DeepSeek V3 steruje WSZYSTKIMI botami!

Pełna kontrola nad:
- whale_follower
- mempool_sniper  
- position_manager
- Konfiguracja (.env)
- Pozycje (kupno/sprzedaż)

SECURITY:
- AI actions are validated against whitelist
- SELL_ALL requires confirmation
- Config updates limited to safe keys
"""

import os
import json
import asyncio
import subprocess
import requests
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from file_utils import safe_load_json, safe_save_json, locked_file

load_dotenv()

# Config
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
MONAD_RPC_URL = os.getenv("MONAD_RPC_URL", "")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS", "0x7b2897EA9547a6BB3c147b3E262483ddAb132A7D")

# SECURITY: Whitelists for AI actions
ALLOWED_BOTS = {"whale_follower", "mempool_sniper", "position_manager", "copy_trader"}
ALLOWED_CONFIG_KEYS = {
    "MIN_BUY_SCORE", "FOLLOW_AMOUNT_MON", "MAX_OPEN_POSITIONS",
    "TRAILING_STOP_PCT", "HARD_STOP_LOSS_PCT", "TAKE_PROFIT_PCT",
    "MIN_LIQUIDITY_USD", "MIN_WHALE_BUY_MON"
}
# DANGEROUS actions that require confirmation file
DANGEROUS_ACTIONS = {"SELL_ALL", "UPDATE_ENV"}
CONFIRM_FILE = Path(__file__).parent / ".ai_confirm"

# Paths
BASE_DIR = Path(__file__).parent
POSITIONS_FILE = BASE_DIR / "positions.json"
TRADES_FILE = BASE_DIR / "trades_history.json"
ENV_FILE = BASE_DIR / ".env"


def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def send_telegram(message: str):
    """Wysyła wiadomość na Telegram"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }, timeout=10)
    except (requests.RequestException, OSError) as e:
        pass  # Telegram errors are non-critical


def get_wallet_balance() -> float:
    """Pobiera balance MON z walleta"""
    try:
        from web3 import Web3
        w3 = Web3(Web3.HTTPProvider(MONAD_RPC_URL))
        balance_wei = w3.eth.get_balance(WALLET_ADDRESS)
        return balance_wei / 1e18
    except Exception as e:
        log(f"❌ Balance error: {e}")
        return 0.0


def load_json(filepath: Path, default=None):
    """Ładuje JSON z file locking"""
    return safe_load_json(filepath, default if default is not None else {})


def get_screen_list() -> list:
    """Lista aktywnych screenów"""
    try:
        result = subprocess.run(["screen", "-ls"], capture_output=True, text=True)
        return result.stdout
    except (subprocess.SubprocessError, OSError) as e:
        log(f"⚠️ Screen list error: {e}")
        return ""


def check_bot_running(bot_name: str) -> bool:
    """Sprawdza czy bot działa"""
    screens = get_screen_list()
    name_map = {
        "whale_follower": "whale",
        "mempool_sniper": "sniper",
        "position_manager": "position",
    }
    screen_name = name_map.get(bot_name, bot_name)
    return screen_name in screens


def start_bot(bot_name: str) -> bool:
    """Uruchamia bota"""
    bot_map = {
        "whale_follower": ("whale", "./whale_follower 2>&1 | tee whale.log"),
        "mempool_sniper": ("sniper", "./mempool_sniper 2>&1 | tee sniper.log"),
        "position_manager": ("position", "./position_manager 2>&1 | tee pm.log"),
    }
    
    if bot_name not in bot_map:
        return False
    
    screen_name, cmd = bot_map[bot_name]
    
    # Sprawdź czy już działa
    if check_bot_running(bot_name):
        log(f"⚠️ {bot_name} already running")
        return True
    
    try:
        subprocess.run(
            ["screen", "-dmS", screen_name, "bash", "-c", cmd],
            cwd=str(BASE_DIR)
        )
        log(f"✅ Started {bot_name}")
        return True
    except Exception as e:
        log(f"❌ Failed to start {bot_name}: {e}")
        return False


def stop_bot(bot_name: str) -> bool:
    """Zatrzymuje bota"""
    name_map = {
        "whale_follower": "whale",
        "mempool_sniper": "sniper",
        "position_manager": "position",
    }
    screen_name = name_map.get(bot_name, bot_name)
    
    try:
        subprocess.run(["screen", "-S", screen_name, "-X", "quit"], 
                      capture_output=True)
        log(f"🛑 Stopped {bot_name}")
        return True
    except (subprocess.SubprocessError, OSError) as e:
        log(f"⚠️ Failed to stop {bot_name}: {e}")
        return False


def update_env_config(changes: dict):
    """Aktualizuje .env z nowymi wartościami"""
    if not ENV_FILE.exists():
        return
    
    with locked_file(ENV_FILE, 'r') as f:
        lines = f.readlines()
    
    updated = []
    changed_keys = set()
    
    for line in lines:
        if "=" in line and not line.strip().startswith("#"):
            key = line.split("=")[0].strip()
            if key in changes:
                updated.append(f"{key}={changes[key]}\n")
                changed_keys.add(key)
            else:
                updated.append(line)
        else:
            updated.append(line)
    
    # Nowe klucze
    for key, value in changes.items():
        if key not in changed_keys:
            updated.append(f"{key}={value}\n")
    
    with locked_file(ENV_FILE, 'w') as f:
        f.writelines(updated)
    
    log(f"⚙️ Config updated: {list(changes.keys())}")


def call_deepseek(prompt: str, use_reasoner: bool = False) -> str:
    """Wywołuje DeepSeek API"""
    model = "deepseek-reasoner" if use_reasoner else "deepseek-chat"
    
    try:
        response = requests.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": model,
                "messages": [
                    {
                        "role": "system",
                        "content": "Jesteś AI Master Controller dla systemu tradingowego memecoinów na Monad blockchain. Odpowiadasz TYLKO w formacie JSON. Jesteś agresywny ale ostrożny - maksymalizujesz zyski minimalizując ryzyko."
                    },
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.1,
                "max_tokens": 2000
            },
            timeout=120
        )
        
        data = response.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "{}")
        return content
        
    except Exception as e:
        log(f"❌ DeepSeek error: {e}")
        return '{"error": "API call failed"}'


def parse_ai_response(content: str) -> dict:
    """Parsuje odpowiedź AI do JSON"""
    try:
        # Wyczyść markdown
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0]
        elif "```" in content:
            content = content.split("```")[1].split("```")[0]
        
        return json.loads(content.strip())
    except (json.JSONDecodeError, ValueError) as e:
        log(f"⚠️ AI response parse error: {e}")
        return {"error": "Parse failed", "raw": content[:500]}


def get_system_state() -> dict:
    """Zbiera pełny stan systemu"""
    
    # Balance
    balance = get_wallet_balance()
    
    # Pozycje
    positions = load_json(POSITIONS_FILE, {})
    
    # Historia (ostatnie 10)
    trades = load_json(TRADES_FILE, [])
    recent_trades = trades[-10:] if len(trades) > 10 else trades
    
    # Metryki
    wins = len([t for t in trades if t.get("pnl", 0) > 0])
    losses = len([t for t in trades if t.get("pnl", 0) < 0])
    total_pnl = sum(t.get("pnl", 0) for t in trades)
    win_rate = (wins / len(trades) * 100) if trades else 0
    
    # Status botów
    bot_status = {
        "whale_follower": check_bot_running("whale_follower"),
        "mempool_sniper": check_bot_running("mempool_sniper"),
        "position_manager": check_bot_running("position_manager"),
    }
    
    # Config
    config = {}
    if ENV_FILE.exists():
        with locked_file(ENV_FILE, 'r') as f:
            for line in f:
                if "=" in line and not line.strip().startswith("#"):
                    parts = line.strip().split("=", 1)
                    if len(parts) == 2:
                        config[parts[0]] = parts[1]
    
    return {
        "timestamp": datetime.now().isoformat(),
        "wallet_balance_mon": balance,
        "positions_count": len(positions),
        "positions": positions,
        "total_invested_mon": sum(p.get("amount_mon", 0) for p in positions.values()),
        "trades_count": len(trades),
        "win_rate": win_rate,
        "total_pnl_mon": total_pnl,
        "wins": wins,
        "losses": losses,
        "recent_trades": recent_trades,
        "bot_status": bot_status,
        "config": {
            "MIN_WHALE_BUY_MON": config.get("MIN_WHALE_BUY_MON", "?"),
            "FOLLOW_AMOUNT_MON": config.get("FOLLOW_AMOUNT_MON", "?"),
            "MAX_OPEN_POSITIONS": config.get("MAX_OPEN_POSITIONS", "?"),
            "MIN_BUY_SCORE": config.get("MIN_BUY_SCORE", "?"),
            "MIN_LIQUIDITY_USD": config.get("MIN_LIQUIDITY_USD", "?"),
        }
    }


def build_ai_prompt(state: dict) -> str:
    """Buduje prompt dla AI"""
    
    positions_str = ""
    for addr, pos in state.get("positions", {}).items():
        positions_str += f"  - {pos.get('token_name', addr[:12])}: {pos.get('amount_mon', 0)} MON\n"
    
    if not positions_str:
        positions_str = "  (brak otwartych pozycji)\n"
    
    return f"""# 🧠 AI MASTER CONTROLLER - ANALIZA SYSTEMU

## 💰 WALLET
- Balance: **{state['wallet_balance_mon']:.2f} MON**
- Zainwestowane: {state['total_invested_mon']:.2f} MON
- Dostępne: {state['wallet_balance_mon']:.2f} MON

## 📊 POZYCJE ({state['positions_count']})
{positions_str}

## 📈 STATYSTYKI
- Total Trades: {state['trades_count']}
- Win Rate: {state['win_rate']:.1f}%
- Total P&L: {state['total_pnl_mon']:.2f} MON
- Wins/Losses: {state['wins']}/{state['losses']}

## 🤖 STATUS BOTÓW
- whale_follower: {"🟢 RUNNING" if state['bot_status']['whale_follower'] else "🔴 STOPPED"}
- mempool_sniper: {"🟢 RUNNING" if state['bot_status']['mempool_sniper'] else "🔴 STOPPED"}
- position_manager: {"🟢 RUNNING" if state['bot_status']['position_manager'] else "🔴 STOPPED"}

## ⚙️ KONFIGURACJA
- MIN_WHALE_BUY_MON: {state['config']['MIN_WHALE_BUY_MON']}
- FOLLOW_AMOUNT_MON: {state['config']['FOLLOW_AMOUNT_MON']}
- MAX_OPEN_POSITIONS: {state['config']['MAX_OPEN_POSITIONS']}
- MIN_BUY_SCORE: {state['config']['MIN_BUY_SCORE']}
- MIN_LIQUIDITY_USD: {state['config']['MIN_LIQUIDITY_USD']}

---

## 🎯 TWOJE ZADANIE

Przeanalizuj system i wydaj komendy. Odpowiedz TYLKO w JSON:

```json
{{
    "analysis": "Krótka analiza sytuacji (1-2 zdania)",
    "risk_level": "LOW/MEDIUM/HIGH/CRITICAL",
    "actions": [
        {{"type": "START_BOT", "bot": "whale_follower"}},
        {{"type": "STOP_BOT", "bot": "mempool_sniper"}},
        {{"type": "UPDATE_CONFIG", "key": "FOLLOW_AMOUNT_MON", "value": "15"}},
        {{"type": "SELL_ALL"}}
    ],
    "telegram_alert": "Wiadomość dla użytkownika (lub null)",
    "next_check_seconds": 60
}}
```

## ZASADY DECYZYJNE:
1. Jeśli balance < 100 MON → CRITICAL, rozważ SELL_ALL
2. Jeśli win_rate < 30% → ustaw MIN_BUY_SCORE na 68 (nie wyżej!)
3. Jeśli win_rate > 40% → możesz obniżyć MIN_BUY_SCORE do 65 i zwiększyć FOLLOW_AMOUNT_MON
4. Jeśli pozycji > MAX_OPEN_POSITIONS → nie uruchamiaj whale_follower
5. position_manager ZAWSZE powinien działać jeśli są pozycje
6. MIN_BUY_SCORE NIE MOŻE być wyższy niż 72! Typowy score to 70.
7. FOLLOW_AMOUNT_MON minimum 8 MON, max 20 MON
8. Bądź ostrożny ale MUSISZ handlować żeby zarabiać!

## 🛡️ ZASADY SPRZEDAŻY - CIERPLIWOŚĆ!
9. NIE SPRZEDAWAJ pozycji ze stratą mniejszą niż -10%!
10. Minimalne trzymanie: 30 MINUT przed jakąkolwiek sprzedażą!
11. "Dead token" NIE jest powodem do sprzedaży - czekaj na dane!
12. Sprzedawaj TYLKO gdy: strata > -15% LUB zysk > +30%
13. NIE PANIKUJ przy -1%, -2%, -5% - to normalne wahania!
14. Lepiej stracić -15% raz niż -2% dziesięć razy!
"""


def execute_actions(actions: list):
    """Wykonuje akcje wydane przez AI z walidacją bezpieczeństwa"""
    
    for action in actions:
        action_type = action.get("type", "")
        
        # SECURITY: Validate action type
        if action_type not in {"START_BOT", "STOP_BOT", "UPDATE_CONFIG", "SELL_ALL", "RESTART_BOT"}:
            log(f"⚠️ Unknown action type: {action_type}, skipping")
            continue
        
        if action_type == "START_BOT":
            bot = action.get("bot", "")
            # SECURITY: Only allow whitelisted bots
            if bot not in ALLOWED_BOTS:
                log(f"⚠️ Bot '{bot}' not in whitelist, skipping")
                continue
            start_bot(bot)
            
        elif action_type == "STOP_BOT":
            bot = action.get("bot", "")
            if bot not in ALLOWED_BOTS:
                log(f"⚠️ Bot '{bot}' not in whitelist, skipping")
                continue
            stop_bot(bot)
            
        elif action_type == "UPDATE_CONFIG":
            key = action.get("key", "")
            value = action.get("value", "")
            # SECURITY: Only allow whitelisted config keys
            if key not in ALLOWED_CONFIG_KEYS:
                log(f"⚠️ Config key '{key}' not in whitelist, skipping")
                continue
            if key and value:
                update_env_config({key: value})
                
        elif action_type == "SELL_ALL":
            # SECURITY: Require confirmation file for dangerous actions
            if not CONFIRM_FILE.exists():
                log("🚨 SELL_ALL blocked - create .ai_confirm file to enable")
                send_telegram("⚠️ AI requested SELL_ALL but blocked. Create .ai_confirm to enable.")
                continue
            log("🚨 SELL_ALL triggered - running emergency sell...")
            try:
                subprocess.run(["python3", "emergency_sell_all.py"], 
                             cwd=str(BASE_DIR), timeout=300)
            except Exception as e:
                log(f"❌ Sell error: {e}")
                
        elif action_type == "RESTART_BOT":
            bot = action.get("bot", "")
            if bot not in ALLOWED_BOTS:
                log(f"⚠️ Bot '{bot}' not in whitelist, skipping")
                continue
            stop_bot(bot)
            import time
            time.sleep(2)
            start_bot(bot)


async def main_loop():
    """Główna pętla AI Master Controller"""
    
    log("=" * 60)
    log("🧠 AI MASTER CONTROLLER - DeepSeek V3")
    log("=" * 60)
    log(f"💰 Wallet: {WALLET_ADDRESS}")
    log(f"🔗 RPC: {MONAD_RPC_URL[:50]}...")
    log("=" * 60)
    
    # Wyślij start alert
    send_telegram("🧠 <b>AI MASTER CONTROLLER</b> uruchomiony!\n\nPełna kontrola nad systemem tradingowym.")
    
    check_interval = 120  # 2 minuty domyślnie
    
    while True:
        try:
            log("\n" + "━" * 50)
            log("🔍 Analizuję system...")
            
            # Zbierz stan
            state = get_system_state()
            log(f"💰 Balance: {state['wallet_balance_mon']:.2f} MON")
            log(f"📊 Pozycje: {state['positions_count']}")
            log(f"📈 Win Rate: {state['win_rate']:.1f}%")
            
            # Zbuduj prompt
            prompt = build_ai_prompt(state)
            
            # Wywołaj AI
            log("🧠 Konsultuję z DeepSeek...")
            response = call_deepseek(prompt, use_reasoner=False)
            decision = parse_ai_response(response)
            
            if "error" in decision:
                log(f"⚠️ AI Error: {decision.get('error')}")
                await asyncio.sleep(60)
                continue
            
            # Wyświetl analizę
            log(f"\n📋 ANALIZA: {decision.get('analysis', 'N/A')}")
            log(f"⚠️ RISK: {decision.get('risk_level', 'N/A')}")
            
            # Wykonaj akcje
            actions = decision.get("actions", [])
            if actions:
                log(f"\n🎯 Wykonuję {len(actions)} akcji:")
                execute_actions(actions)
            else:
                log("✅ Brak akcji do wykonania")
            
            # Telegram alert
            tg_msg = decision.get("telegram_alert")
            if tg_msg:
                send_telegram(f"🧠 AI MASTER:\n\n{tg_msg}")
            
            # Następny check
            check_interval = decision.get("next_check_seconds", 120)
            log(f"\n⏳ Następna analiza za {check_interval}s...")
            
            await asyncio.sleep(check_interval)
            
        except KeyboardInterrupt:
            log("\n🛑 AI Master Controller zatrzymany")
            send_telegram("🛑 AI MASTER CONTROLLER zatrzymany")
            break
        except Exception as e:
            log(f"❌ Error: {e}")
            await asyncio.sleep(60)


if __name__ == "__main__":
    asyncio.run(main_loop())
