#!/usr/bin/env python3
"""
👁️ DEV WALLET MONITOR - Monitoruje devy i reaguje na ich sprzedaże

Workflow:
1. Ładuje creators.json (token -> dev wallet mapping)
2. Monitoruje transakcje devów przez WebSocket
3. Gdy dev sprzedaje:
   - Block token (24h)
   - Emergency sell nasza pozycja
   - Alert do Telegram

Źródła dev wallets:
- creators.json (z whale_follower)
- NAD.FUN API (getTokenInfo)
"""

import asyncio
import json
import os
import sys
import time
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Dict, Set, Optional
from decimal import Decimal
from dotenv import load_dotenv
import aiohttp

load_dotenv()

# Import our modules
from blocklist import block_token, REASON_DEV_SELL
from risk_engine import RiskConfig

BASE_DIR = Path(__file__).parent
CREATORS_FILE = BASE_DIR / "creators.json"
POSITIONS_FILE = BASE_DIR / "positions.json"
DEV_MONITOR_LOG = BASE_DIR / "dev_monitor.log"

# Config
RPC_URL = os.getenv("MONAD_RPC_URL")
LENS_ADDRESS = "0x7e78A8DE94f21804F7a17F4E8BF9EC2c872187ea"
ROUTER_ADDRESS = "0x6F6B8F1a20703309951a5127c45B49b1CD981A22"

# Telegram
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")

# Check interval
CHECK_INTERVAL = 10  # seconds


def log(msg: str):
    """Log with timestamp"""
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        with open(DEV_MONITOR_LOG, "a") as f:
            f.write(line + "\n")
    except:
        pass


def load_creators() -> Dict[str, str]:
    """Load token -> creator mapping"""
    try:
        if CREATORS_FILE.exists():
            with open(CREATORS_FILE) as f:
                data = json.load(f)
                # Format: {token: {creator: addr, ...}} or {token: creator}
                result = {}
                for token, info in data.items():
                    if isinstance(info, dict):
                        result[token.lower()] = info.get("creator", "").lower()
                    else:
                        result[token.lower()] = str(info).lower()
                return result
    except Exception as e:
        log(f"Error loading creators: {e}")
    return {}


def load_positions() -> Dict[str, dict]:
    """Load current positions"""
    try:
        if POSITIONS_FILE.exists():
            with open(POSITIONS_FILE) as f:
                return json.load(f)
    except:
        pass
    return {}


def save_positions(positions: dict):
    """Save positions"""
    try:
        with open(POSITIONS_FILE, "w") as f:
            json.dump(positions, f, indent=2)
    except Exception as e:
        log(f"Error saving positions: {e}")


async def send_telegram(msg: str):
    """Send Telegram alert"""
    if not TG_TOKEN or not TG_CHAT:
        return
    
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        async with aiohttp.ClientSession() as session:
            await session.post(url, json={
                "chat_id": TG_CHAT,
                "text": msg,
                "parse_mode": "HTML"
            })
    except Exception as e:
        log(f"Telegram error: {e}")


async def get_token_balance(token: str, wallet: str) -> int:
    """Get token balance for wallet"""
    try:
        # balanceOf(address)
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_call",
            "params": [{
                "to": token,
                "data": f"0x70a08231000000000000000000000000{wallet[2:]}"
            }, "latest"]
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(RPC_URL, json=payload, timeout=10) as resp:
                data = await resp.json()
                result = data.get("result", "0x0")
                return int(result, 16) if result else 0
    except:
        return 0


async def check_dev_sells(creators: Dict[str, str], positions: Dict[str, dict]) -> list:
    """Check if any dev sold their tokens"""
    dev_sells = []
    
    # Get unique dev wallets we care about (only for our positions)
    position_tokens = set(t.lower() for t in positions.keys())
    
    for token in position_tokens:
        dev = creators.get(token)
        if not dev:
            continue
        
        # Check dev's token balance
        balance = await get_token_balance(token, dev)
        
        # Store last known balance
        pos = positions.get(token, {})
        last_dev_balance = pos.get("dev_balance", -1)
        
        if last_dev_balance == -1:
            # First check - record balance
            pos["dev_balance"] = balance
            positions[token] = pos
            log(f"📊 {token[:12]}... dev balance: {balance}")
        elif balance < last_dev_balance * 0.5:
            # Dev sold more than 50% - RED FLAG
            sold_pct = (1 - balance / last_dev_balance) * 100 if last_dev_balance > 0 else 100
            dev_sells.append({
                "token": token,
                "dev": dev,
                "sold_pct": sold_pct,
                "remaining": balance
            })
            log(f"🚨 DEV SELL: {token[:12]}... sold {sold_pct:.0f}%!")
        else:
            # Update balance
            pos["dev_balance"] = balance
            positions[token] = pos
    
    return dev_sells


async def emergency_sell(token: str) -> bool:
    """Emergency sell position"""
    log(f"🚨 EMERGENCY SELL: {token[:16]}...")
    
    try:
        sell_script = BASE_DIR / "sell_token.py"
        if not sell_script.exists():
            log(f"❌ sell_token.py not found")
            return False
        
        result = subprocess.run(
            ["python3", str(sell_script), token],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(BASE_DIR)
        )
        
        if result.returncode == 0:
            log(f"✅ Emergency sell executed")
            return True
        else:
            log(f"❌ Sell failed: {result.stderr[:200]}")
            return False
    except Exception as e:
        log(f"❌ Sell error: {e}")
        return False


async def handle_dev_sell(sell_info: dict, positions: dict):
    """Handle a dev sell event"""
    token = sell_info["token"]
    dev = sell_info["dev"]
    sold_pct = sell_info["sold_pct"]
    
    log(f"\n{'='*60}")
    log(f"🚨 DEV SELL DETECTED!")
    log(f"   Token: {token[:16]}...")
    log(f"   Dev: {dev[:12]}...")
    log(f"   Sold: {sold_pct:.0f}%")
    
    # 1. Block token immediately
    block_token(token, REASON_DEV_SELL, 86400 * 7)  # 7 days block
    log(f"   🚫 Token blocked for 7 days")
    
    # 2. Emergency sell our position
    if token in positions:
        success = await emergency_sell(token)
        
        if success:
            # Remove from positions
            del positions[token]
            save_positions(positions)
            log(f"   ✅ Position closed")
        else:
            log(f"   ⚠️ Could not sell - may be rug pulled")
            # Mark as rug
            positions[token]["status"] = "rug_dev_sell"
            positions[token]["rug_time"] = datetime.now().isoformat()
            save_positions(positions)
    
    # 3. Send Telegram alert
    msg = f"""🚨 <b>DEV SELL ALERT!</b>

Token: <code>{token[:20]}...</code>
Dev sold: <b>{sold_pct:.0f}%</b>

Action taken:
- Token blocked for 7 days
- Position closed (or marked as rug)

Stay safe! 🛡️"""
    
    await send_telegram(msg)


async def monitor_loop():
    """Main monitoring loop"""
    log("\n" + "="*70)
    log("👁️ DEV WALLET MONITOR STARTED")
    log("="*70)
    
    while True:
        try:
            # Load current state
            creators = load_creators()
            positions = load_positions()
            
            if not positions:
                log("No positions to monitor")
                await asyncio.sleep(CHECK_INTERVAL)
                continue
            
            log(f"\n📊 Checking {len(positions)} positions for dev sells...")
            
            # Check for dev sells
            dev_sells = await check_dev_sells(creators, positions)
            
            # Handle each dev sell
            for sell_info in dev_sells:
                await handle_dev_sell(sell_info, positions)
            
            # Save updated positions
            save_positions(positions)
            
            await asyncio.sleep(CHECK_INTERVAL)
            
        except Exception as e:
            log(f"❌ Monitor error: {e}")
            import traceback
            traceback.print_exc()
            await asyncio.sleep(CHECK_INTERVAL)


async def main():
    """Main entry"""
    print("👁️ Dev Wallet Monitor")
    print("Watching for dev sells...")
    print()
    
    try:
        await monitor_loop()
    except KeyboardInterrupt:
        print("\n🛑 Stopped")


if __name__ == "__main__":
    asyncio.run(main())
