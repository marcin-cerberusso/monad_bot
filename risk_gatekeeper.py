#!/usr/bin/env python3
"""
🛡️ RISK GATEKEEPER - Centralny strażnik przed złymi tokenami

Monitoruje sygnały od whale_follower i przepuszcza tylko te,
które przejdą przez risk_engine.

Workflow:
1. Whale_follower zapisuje sygnał do whale_signals.json
2. Risk Gatekeeper czyta sygnał
3. Sprawdza przez risk_engine (blocklist, slippage, FOMO, bundle)
4. Jeśli OK → wykonuje buy przez subprocess
5. Rejestruje pozycję w positions.json
"""

import asyncio
import json
import os
import sys
import time
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Dict, Optional
from dotenv import load_dotenv

load_dotenv()

# Import our risk modules
from risk_engine import RiskConfig, full_risk_check, TradeMetrics
from blocklist import is_blocked, block_token, REASON_HONEYPOT, REASON_RUG

BASE_DIR = Path(__file__).parent
WHALE_SIGNALS_FILE = BASE_DIR / "whale_signals.json"
POSITIONS_FILE = BASE_DIR / "positions.json"
GATEKEEPER_LOG = BASE_DIR / "gatekeeper.log"

# Config from env
CHECK_INTERVAL = 2.0  # seconds between signal checks
MAX_BUYS_PER_MINUTE = 3


def log(msg: str):
    """Log message with timestamp"""
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        with open(GATEKEEPER_LOG, "a") as f:
            f.write(line + "\n")
    except:
        pass


def load_signals() -> list:
    """Load pending signals from whale_follower"""
    try:
        if WHALE_SIGNALS_FILE.exists():
            with open(WHALE_SIGNALS_FILE) as f:
                data = json.load(f)
                return data.get("pending", [])
    except:
        pass
    return []


def clear_signal(signal: dict):
    """Remove processed signal"""
    try:
        data = {"pending": [], "processed": []}
        if WHALE_SIGNALS_FILE.exists():
            with open(WHALE_SIGNALS_FILE) as f:
                data = json.load(f)
        
        pending = data.get("pending", [])
        # Remove matching signal
        pending = [s for s in pending if s.get("token") != signal.get("token")]
        data["pending"] = pending
        
        # Add to processed
        processed = data.get("processed", [])
        processed.append({
            **signal,
            "processed_at": datetime.now().isoformat()
        })
        data["processed"] = processed[-100:]  # Keep last 100
        
        with open(WHALE_SIGNALS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log(f"Error clearing signal: {e}")


def load_positions() -> dict:
    """Load current positions"""
    try:
        if POSITIONS_FILE.exists():
            with open(POSITIONS_FILE) as f:
                return json.load(f)
    except:
        pass
    return {}


def save_position(token: str, position_data: dict):
    """Save new position"""
    try:
        positions = load_positions()
        positions[token.lower()] = position_data
        with open(POSITIONS_FILE, "w") as f:
            json.dump(positions, f, indent=2)
        log(f"✅ Position saved: {token[:12]}...")
    except Exception as e:
        log(f"Error saving position: {e}")


async def execute_buy(token: str, amount_mon: float) -> bool:
    """Execute buy through Rust binary or Python script"""
    log(f"🛒 Executing BUY: {token[:16]}... amount={amount_mon:.4f} MON")
    
    try:
        # Use Python buy script if available, else Rust binary
        buy_script = BASE_DIR / "buy_token.py"
        
        if buy_script.exists():
            result = subprocess.run(
                ["python3", str(buy_script), token, str(amount_mon)],
                capture_output=True,
                text=True,
                timeout=60,
                cwd=str(BASE_DIR)
            )
            if result.returncode == 0:
                log(f"✅ Buy executed via Python: {result.stdout[:200]}")
                return True
            else:
                log(f"❌ Buy failed: {result.stderr[:200]}")
                return False
        else:
            # Fallback to Rust copy_trader or other binary
            log(f"⚠️ No buy_token.py found, skipping execution")
            return False
            
    except subprocess.TimeoutExpired:
        log(f"⏱️ Buy timed out after 60s")
        return False
    except Exception as e:
        log(f"❌ Buy error: {e}")
        return False


async def process_signal(signal: dict, config: RiskConfig) -> bool:
    """Process a single whale signal through risk checks"""
    token = signal.get("token", "").lower()
    whale = signal.get("whale", "")
    amount = signal.get("amount", 0)
    timestamp = signal.get("timestamp", "")
    
    log(f"\n{'='*60}")
    log(f"📨 New signal: {token[:16]}...")
    log(f"   Whale: {whale[:12]}...")
    log(f"   Amount: {amount:.2f} MON")
    
    # 1. Check blocklist first (fastest)
    blocked, reason = is_blocked(token)
    if blocked:
        log(f"🚫 BLOCKED: {reason}")
        clear_signal(signal)
        return False
    
    # 2. Check if we already have position
    positions = load_positions()
    if token in positions:
        log(f"⚠️ Already have position in this token")
        clear_signal(signal)
        return False
    
    # 3. Full risk check
    try:
        log(f"🔍 Running risk checks...")
        metrics = await full_risk_check(token, amount, config)
        
        log(f"   Risk Score: {metrics.risk_score}")
        log(f"   Liquidity: ${metrics.liquidity_usd:.2f}")
        log(f"   Slippage: {metrics.slippage_percent:.1f}%")
        log(f"   FOMO (pump): {metrics.already_pumped}")
        log(f"   Bundles 1h: {metrics.bundles_1h}")
        
        # Get decision
        if not metrics.should_trade:
            log(f"❌ REJECTED: {metrics.rejection_reason}")
            
            # Auto-block if serious issue
            if "honeypot" in metrics.rejection_reason.lower():
                block_token(token, REASON_HONEYPOT, 86400)
            elif "liquidity" in metrics.rejection_reason.lower():
                block_token(token, REASON_RUG, 3600)
                
            clear_signal(signal)
            return False
            
        # 4. Passed all checks - calculate position size
        from risk_engine import PositionManager
        pm = PositionManager(config)
        
        position_size = pm.calculate_position_size(
            liquidity_usd=metrics.liquidity_usd,
            risk_score=metrics.risk_score
        )
        
        log(f"✅ APPROVED: position_size={position_size:.4f} MON")
        
        # 5. Execute buy
        success = await execute_buy(token, position_size)
        
        if success:
            # 6. Save position with dynamic TP/SL
            tp_percent, sl_percent = pm.get_tp_sl_for_liquidity(metrics.liquidity_usd)
            
            position_data = {
                "token": token,
                "entry_price": metrics.current_price_mon,
                "entry_time": datetime.now().isoformat(),
                "amount_mon": position_size,
                "liquidity_usd": metrics.liquidity_usd,
                "risk_score": metrics.risk_score,
                "whale": whale,
                "tp_percent": tp_percent,
                "sl_percent": sl_percent,
                "highest_price": metrics.current_price_mon,
                "trailing_activated": False
            }
            save_position(token, position_data)
            
        clear_signal(signal)
        return success
        
    except Exception as e:
        log(f"❌ Risk check error: {e}")
        import traceback
        traceback.print_exc()
        clear_signal(signal)
        return False


async def main_loop():
    """Main gatekeeper loop"""
    log("\n" + "="*70)
    log("🛡️ RISK GATEKEEPER STARTED")
    log("="*70)
    
    config = RiskConfig()
    log(f"Config loaded:")
    log(f"  Max position: {config.max_position_percent*100:.0f}%")
    log(f"  Max slippage: {config.max_slippage_percent:.0f}%")
    log(f"  FOMO threshold: {config.fomo_threshold_percent:.0f}%")
    log(f"  Low liquidity: ${config.low_liquidity_threshold_usd}")
    log(f"  Medium liquidity: ${config.medium_liquidity_threshold_usd}")
    
    buys_this_minute = 0
    minute_start = time.time()
    
    while True:
        try:
            # Rate limit check
            if time.time() - minute_start > 60:
                minute_start = time.time()
                buys_this_minute = 0
            
            if buys_this_minute >= MAX_BUYS_PER_MINUTE:
                await asyncio.sleep(1)
                continue
            
            # Load pending signals
            signals = load_signals()
            
            for signal in signals:
                success = await process_signal(signal, config)
                if success:
                    buys_this_minute += 1
                    
                    if buys_this_minute >= MAX_BUYS_PER_MINUTE:
                        log(f"⏸️ Rate limit: {MAX_BUYS_PER_MINUTE}/min reached")
                        break
                
                await asyncio.sleep(0.5)  # Small delay between signals
            
            await asyncio.sleep(CHECK_INTERVAL)
            
        except KeyboardInterrupt:
            log("\n🛑 Shutting down...")
            break
        except Exception as e:
            log(f"❌ Loop error: {e}")
            await asyncio.sleep(5)


if __name__ == "__main__":
    print("🛡️ Risk Gatekeeper v1.0")
    print("Kupuj taniej, sprzedawaj drożej!")
    print()
    
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        print("\nBye!")
