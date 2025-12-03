#!/usr/bin/env python3
"""
🔍 WHALE LOG PARSER - Parsuje logi Rust whale_follower
i przepuszcza przez risk_engine przed kupnem

Rust whale_follower ma honeypot detection ale brakuje:
- FOMO filter (already pumped?)
- Bundle detection (wash trading?)
- Dynamic TP/SL
- Position sizing per liquidity
- Blocklist check

Ten skrypt to post-filter:
1. Monitoruje whale.log w real-time
2. Gdy widzi "FOLLOWING WHALE" - sprawdza risk
3. Jeśli risk OK - pozwala
4. Jeśli risk NOT OK - natychmiast sprzedaje pozycję
"""

import asyncio
import json
import os
import re
import sys
import time
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Optional, Tuple
from dotenv import load_dotenv

load_dotenv()

# Import risk modules
from risk_engine import RiskConfig, full_risk_check
from blocklist import is_blocked, block_token, REASON_BUNDLE, REASON_RUG

BASE_DIR = Path(__file__).parent
WHALE_LOG = BASE_DIR / "whale.log"
POSITIONS_FILE = BASE_DIR / "positions.json"
PARSER_LOG = BASE_DIR / "parser.log"

# Patterns to detect
PATTERN_FOLLOWING = r"FOLLOWING WHALE.*actual_follow_amount=([0-9.]+).*whale_size=([0-9.]+)"
PATTERN_BUY_TX = r"BUY TX SENT.*buy_tx_hash=(0x[a-f0-9]+)"
PATTERN_SUCCESS = r"SUCCESS.*Followed whale"
PATTERN_TOKEN = r"token=(0x[a-f0-9]+)"
PATTERN_WHALE = r"whale=(0x[a-f0-9]+)"


def log(msg: str):
    """Log with timestamp"""
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        with open(PARSER_LOG, "a") as f:
            f.write(line + "\n")
    except:
        pass


def load_positions() -> dict:
    """Load positions"""
    try:
        if POSITIONS_FILE.exists():
            with open(POSITIONS_FILE) as f:
                return json.load(f)
    except:
        pass
    return {}


def save_position(token: str, data: dict):
    """Save position"""
    try:
        positions = load_positions()
        positions[token.lower()] = data
        with open(POSITIONS_FILE, "w") as f:
            json.dump(positions, f, indent=2)
    except Exception as e:
        log(f"Error saving position: {e}")


async def emergency_sell(token: str) -> bool:
    """Emergency sell a position that failed risk check"""
    log(f"🚨 EMERGENCY SELL: {token[:16]}...")
    
    try:
        result = subprocess.run(
            ["python3", str(BASE_DIR / "sell_token.py"), token],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(BASE_DIR)
        )
        
        if result.returncode == 0:
            log(f"✅ Emergency sell success")
            return True
        else:
            log(f"❌ Emergency sell failed: {result.stderr[:200]}")
            return False
    except Exception as e:
        log(f"❌ Emergency sell error: {e}")
        return False


async def validate_position(token: str, amount_mon: float, whale_size: float) -> bool:
    """Validate a position through risk checks"""
    log(f"\n{'='*60}")
    log(f"🔍 Validating new position: {token[:16]}...")
    log(f"   Amount: {amount_mon:.2f} MON, Whale: {whale_size:.0f} MON")
    
    # 1. Check blocklist
    blocked, reason = is_blocked(token)
    if blocked:
        log(f"   🚫 BLOCKED: {reason}")
        return False
    
    # 2. Full risk check
    config = RiskConfig()
    try:
        metrics = await full_risk_check(token, amount_mon, config)
        
        log(f"   📊 Risk Score: {metrics.risk_score}")
        log(f"   📊 Liquidity: ${metrics.liquidity_usd:.0f}")
        log(f"   📊 Slippage: {metrics.slippage_percent:.1f}%")
        log(f"   📊 FOMO (pumped): {metrics.already_pumped}")
        log(f"   📊 Bundles 1h: {metrics.bundles_1h}")
        
        if not metrics.should_trade:
            log(f"   ❌ RISK FAILED: {metrics.rejection_reason}")
            
            # Block for future
            if "bundle" in metrics.rejection_reason.lower():
                block_token(token, REASON_BUNDLE, 3600)
                
            return False
        
        # 3. Update position with proper TP/SL
        from risk_engine import PositionManager
        pm = PositionManager(config)
        tp_pct, sl_pct = pm.get_tp_sl_for_liquidity(metrics.liquidity_usd)
        
        position_data = {
            "token": token,
            "entry_time": datetime.now().isoformat(),
            "amount_mon": amount_mon,
            "whale_size_mon": whale_size,
            "liquidity_usd": metrics.liquidity_usd,
            "risk_score": metrics.risk_score,
            "tp_percent": tp_pct,
            "sl_percent": sl_pct,
            "validated": True
        }
        save_position(token, position_data)
        
        log(f"   ✅ VALIDATED! TP={tp_pct}% SL={sl_pct}%")
        return True
        
    except Exception as e:
        log(f"   ❌ Validation error: {e}")
        import traceback
        traceback.print_exc()
        return False


async def tail_log():
    """Tail whale.log and process new entries"""
    log("🔍 WHALE LOG PARSER STARTED")
    log(f"   Monitoring: {WHALE_LOG}")
    
    if not WHALE_LOG.exists():
        log("⚠️ whale.log not found, waiting...")
        while not WHALE_LOG.exists():
            await asyncio.sleep(5)
    
    # Get current end of file
    with open(WHALE_LOG, 'r') as f:
        f.seek(0, 2)  # Go to end
        position = f.tell()
    
    log(f"   Starting from position: {position}")
    
    pending_token = None
    pending_amount = None
    pending_whale_size = None
    
    while True:
        try:
            with open(WHALE_LOG, 'r') as f:
                f.seek(position)
                new_lines = f.read()
                position = f.tell()
            
            if new_lines:
                for line in new_lines.split('\n'):
                    if not line.strip():
                        continue
                    
                    # Check for FOLLOWING WHALE
                    match = re.search(PATTERN_FOLLOWING, line)
                    if match:
                        pending_amount = float(match.group(1))
                        pending_whale_size = float(match.group(2))
                        
                        # Extract token from same or previous lines
                        token_match = re.search(PATTERN_TOKEN, line)
                        if token_match:
                            pending_token = token_match.group(1)
                        
                        log(f"📨 Detected whale follow: amount={pending_amount} whale={pending_whale_size}")
                        continue
                    
                    # Check for SUCCESS
                    if "SUCCESS" in line and "Followed whale" in line:
                        if pending_token:
                            log(f"📨 Buy confirmed for {pending_token[:16]}...")
                            
                            # Validate through risk
                            ok = await validate_position(
                                pending_token, 
                                pending_amount or 20.0,
                                pending_whale_size or 0
                            )
                            
                            if not ok:
                                log(f"🚨 Risk check failed - selling position!")
                                await emergency_sell(pending_token)
                            
                            pending_token = None
                            pending_amount = None
                            pending_whale_size = None
                    
                    # Extract token for next SUCCESS
                    token_match = re.search(r"token=(0x[a-f0-9]{40})", line, re.IGNORECASE)
                    if token_match:
                        pending_token = token_match.group(1).lower()
            
            await asyncio.sleep(0.5)
            
        except Exception as e:
            log(f"Error reading log: {e}")
            await asyncio.sleep(5)


async def main():
    """Main entry"""
    print("🔍 Whale Log Parser")
    print("Post-filter for Rust whale_follower")
    print()
    
    try:
        await tail_log()
    except KeyboardInterrupt:
        print("\n🛑 Stopped")


if __name__ == "__main__":
    asyncio.run(main())
