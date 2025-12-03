#!/usr/bin/env python3
"""
🐳 WHALE FOLLOWER V2 - Python version with full risk management

Monitoruje transakcje wielorybów przez WebSocket i:
1. Wykrywa duże zakupy (>100 MON)
2. Sprawdza przez risk_engine (blocklist, slippage, FOMO, bundle)
3. Tylko jeśli OK → kupuje przez NAD.FUN router

Features:
- Live WebSocket monitoring (QuickNode)
- Honeypot detection (test sell)
- FOMO filter (already pumped?)
- Bundle detection (wash trading?)
- Dynamic position sizing
- Dynamic TP/SL per liquidity
"""

import asyncio
import json
import os
import sys
import time
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Dict, Optional, Set
from decimal import Decimal
from dotenv import load_dotenv
import websockets
import aiohttp

load_dotenv()

# Import our risk modules
from risk_engine import RiskConfig, full_risk_check, TradeMetrics, BlockBuyTracker
from blocklist import is_blocked, block_token, REASON_HONEYPOT, REASON_RUG, REASON_BUNDLE

BASE_DIR = Path(__file__).parent
POSITIONS_FILE = BASE_DIR / "positions.json"
WHALE_LOG = BASE_DIR / "whale_v2.log"
CREATORS_FILE = BASE_DIR / "creators.json"

# Config
WS_URL = os.getenv("MONAD_WS_URL", "wss://monad-mainnet.blockvision.org/v1/2fUJ5WqdKbiT8XeZQS2GBNEa0n8")
RPC_URL = os.getenv("MONAD_RPC_URL", "https://practical-neat-telescope.monad-mainnet.quiknode.pro/730346a87672e9b4d50429263f445f1192e7ca71")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
WALLET = os.getenv("WALLET_ADDRESS", "0x7b2897EA9547a6BB3c147b3E262483ddAb132A7D")

# NAD.FUN Router
ROUTER = "0x6F6B8F1a20703309951a5127c45B49b1CD981A22"
LENS = "0x7e78A8DE94f21804F7a17F4E8BF9EC2c872187ea"

# Thresholds
MIN_WHALE_SIZE_MON = 100  # Only follow buys > 100 MON
MAX_FOLLOW_SIZE_MON = 30  # Max we invest per trade
MIN_WHALE_FOLLOW_SIZE_MON = 500  # Big whales we auto-trust
COOLDOWN_SECONDS = 30  # Cooldown between buys on same token


def log(msg: str):
    """Log with timestamp"""
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    line = f"[{ts}] {msg}"
    print(line)
    try:
        with open(WHALE_LOG, "a") as f:
            f.write(line + "\n")
    except:
        pass


def load_positions() -> dict:
    """Load current positions"""
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


def load_creators() -> dict:
    """Load token creators (for dev wallet monitoring)"""
    try:
        if CREATORS_FILE.exists():
            with open(CREATORS_FILE) as f:
                return json.load(f)
    except:
        pass
    return {}


class WhaleFollowerV2:
    """Main whale follower with risk management"""
    
    def __init__(self):
        self.config = RiskConfig()
        self.bundle_tracker = BlockBuyTracker()
        self.running = False
        self.ws = None
        self.session = None
        
        # Rate limiting
        self.last_buy_time: Dict[str, float] = {}  # token -> timestamp
        self.buys_today = 0
        self.day_start = time.time()
        
        # Stats
        self.whales_seen = 0
        self.whales_followed = 0
        self.whales_skipped = 0
        
    async def start(self):
        """Start the follower"""
        log("="*70)
        log("🐳 WHALE FOLLOWER V2 - Python with Risk Management")
        log("="*70)
        log(f"Config:")
        log(f"  Min whale size: {MIN_WHALE_SIZE_MON} MON")
        log(f"  Max follow size: {MAX_FOLLOW_SIZE_MON} MON")
        log(f"  Max slippage: {self.config.max_slippage_percent}%")
        log(f"  FOMO threshold: {self.config.fomo_threshold_percent}%")
        log(f"  WS URL: {WS_URL[:50]}...")
        log("")
        
        self.running = True
        self.session = aiohttp.ClientSession()
        
        # Start WebSocket loop with reconnection
        while self.running:
            try:
                await self._ws_loop()
            except websockets.exceptions.ConnectionClosed:
                log("⚠️ WebSocket disconnected, reconnecting in 5s...")
                await asyncio.sleep(5)
            except Exception as e:
                log(f"❌ WS error: {e}, reconnecting in 5s...")
                await asyncio.sleep(5)
    
    async def stop(self):
        """Stop the follower"""
        self.running = False
        if self.ws:
            await self.ws.close()
        if self.session:
            await self.session.close()
        log("🛑 Whale Follower stopped")
    
    async def _ws_loop(self):
        """Main WebSocket loop"""
        log("🔌 Connecting to WebSocket...")
        
        async with websockets.connect(WS_URL, ping_interval=30) as ws:
            self.ws = ws
            log("✅ Connected!")
            
            # Subscribe to pending transactions
            subscribe = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "eth_subscribe",
                "params": ["newPendingTransactions"]
            }
            await ws.send(json.dumps(subscribe))
            
            response = await ws.recv()
            sub_id = json.loads(response).get("result")
            log(f"📡 Subscribed to pending txs: {sub_id}")
            
            # Process messages
            async for msg in ws:
                try:
                    data = json.loads(msg)
                    
                    if "params" in data:
                        tx_hash = data["params"].get("result")
                        if tx_hash:
                            asyncio.create_task(self._check_tx(tx_hash))
                            
                except json.JSONDecodeError:
                    pass
                except Exception as e:
                    log(f"Error processing msg: {e}")
    
    async def _check_tx(self, tx_hash: str):
        """Check if transaction is a whale buy"""
        try:
            # Get transaction details
            tx = await self._get_tx(tx_hash)
            if not tx:
                return
            
            to = tx.get("to", "").lower()
            value_hex = tx.get("value", "0x0")
            input_data = tx.get("input", "")
            from_addr = tx.get("from", "").lower()
            
            # Check if it's a NAD.FUN buy (to router, with value)
            if to != ROUTER.lower():
                return
            
            # Parse value
            try:
                value_wei = int(value_hex, 16)
                value_mon = value_wei / 10**18
            except:
                return
            
            # Skip small buys
            if value_mon < MIN_WHALE_SIZE_MON:
                return
            
            # Extract token from input data (buy function selector + params)
            token = self._extract_token_from_input(input_data)
            if not token:
                return
            
            self.whales_seen += 1
            
            log("")
            log("━"*60)
            log(f"🐳 WHALE BUY DETECTED!")
            log(f"   Whale: {from_addr[:12]}...")
            log(f"   Amount: {value_mon:.2f} MON")
            log(f"   Token: {token[:16]}...")
            
            # Process whale buy
            await self._process_whale_buy(from_addr, token, value_mon)
            
        except Exception as e:
            pass  # Ignore errors for non-relevant txs
    
    async def _get_tx(self, tx_hash: str) -> Optional[dict]:
        """Get transaction by hash"""
        try:
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "eth_getTransactionByHash",
                "params": [tx_hash]
            }
            
            async with self.session.post(RPC_URL, json=payload, timeout=5) as resp:
                data = await resp.json()
                return data.get("result")
        except:
            return None
    
    def _extract_token_from_input(self, input_data: str) -> Optional[str]:
        """Extract token address from buy() input data"""
        # buy((address,uint256,address,uint256)) = 0xd96a094a...
        # Token is first param (after selector + offset)
        
        if len(input_data) < 138:  # 0x + 4 bytes selector + 32 bytes
            return None
        
        try:
            # Skip selector (0x + 8 chars) and get first address
            # For tuple input, first 32 bytes is usually the address (padded)
            address_part = input_data[10:74]  # Skip 0x + selector
            address = "0x" + address_part[-40:]  # Last 40 chars are address
            
            # Validate address format
            if len(address) == 42 and address.startswith("0x"):
                return address.lower()
        except:
            pass
        
        return None
    
    async def _process_whale_buy(self, whale: str, token: str, amount_mon: float):
        """Process a whale buy through risk checks"""
        
        # 1. Check blocklist first
        blocked, reason = is_blocked(token)
        if blocked:
            log(f"   🚫 BLOCKED: {reason}")
            self.whales_skipped += 1
            return
        
        # 2. Check if already have position
        positions = load_positions()
        if token in positions:
            log(f"   ⚠️ Already have position")
            return
        
        # 3. Cooldown check
        last_buy = self.last_buy_time.get(token, 0)
        if time.time() - last_buy < COOLDOWN_SECONDS:
            log(f"   ⏱️ Cooldown active")
            return
        
        # 4. Daily limit check
        if time.time() - self.day_start > 86400:
            self.day_start = time.time()
            self.buys_today = 0
        
        if self.buys_today >= self.config.max_parallel_positions:
            log(f"   ⏸️ Daily position limit reached")
            return
        
        # 5. Track bundle (for detection)
        self.bundle_tracker.record_buy(token)
        
        # 6. Full risk check
        log(f"   🔍 Running risk checks...")
        
        try:
            metrics = await full_risk_check(token, MAX_FOLLOW_SIZE_MON, self.config)
            
            log(f"   📊 Risk Score: {metrics.risk_score}")
            log(f"   📊 Liquidity: ${metrics.liquidity_usd:.0f}")
            log(f"   📊 Slippage: {metrics.slippage_percent:.1f}%")
            log(f"   📊 FOMO: {metrics.already_pumped}")
            log(f"   📊 Bundles 1h: {metrics.bundles_1h}")
            
            if not metrics.should_trade:
                log(f"   ❌ REJECTED: {metrics.rejection_reason}")
                self.whales_skipped += 1
                
                # Auto-block if serious
                if "honeypot" in metrics.rejection_reason.lower():
                    block_token(token, REASON_HONEYPOT, 86400)
                elif "bundle" in metrics.rejection_reason.lower():
                    block_token(token, REASON_BUNDLE, 3600)
                    
                return
            
            # 7. Calculate position size
            from risk_engine import PositionManager
            pm = PositionManager(self.config)
            
            position_size = pm.calculate_position_size(
                liquidity_usd=metrics.liquidity_usd,
                risk_score=metrics.risk_score
            )
            position_size = min(position_size, MAX_FOLLOW_SIZE_MON)
            
            log(f"   ✅ APPROVED: size={position_size:.2f} MON")
            
            # 8. Execute buy
            success = await self._execute_buy(token, position_size)
            
            if success:
                self.whales_followed += 1
                self.buys_today += 1
                self.last_buy_time[token] = time.time()
                
                # 9. Save position with dynamic TP/SL
                tp_pct, sl_pct = pm.get_tp_sl_for_liquidity(metrics.liquidity_usd)
                
                position_data = {
                    "token": token,
                    "entry_price": metrics.current_price_mon,
                    "entry_time": datetime.now().isoformat(),
                    "amount_mon": position_size,
                    "liquidity_usd": metrics.liquidity_usd,
                    "risk_score": metrics.risk_score,
                    "whale": whale,
                    "whale_size_mon": amount_mon,
                    "tp_percent": tp_pct,
                    "sl_percent": sl_pct,
                    "highest_price": metrics.current_price_mon,
                    "trailing_activated": False
                }
                save_position(token, position_data)
                
                log(f"   🎉 Position saved! TP={tp_pct}% SL={sl_pct}%")
                
            self._print_stats()
            
        except Exception as e:
            log(f"   ❌ Risk check error: {e}")
            import traceback
            traceback.print_exc()
    
    async def _execute_buy(self, token: str, amount_mon: float) -> bool:
        """Execute buy through NAD.FUN router"""
        log(f"   🛒 Executing buy: {amount_mon:.2f} MON...")
        
        try:
            result = subprocess.run(
                ["python3", str(BASE_DIR / "buy_token.py"), token, str(amount_mon)],
                capture_output=True,
                text=True,
                timeout=60,
                cwd=str(BASE_DIR)
            )
            
            if result.returncode == 0:
                log(f"   ✅ Buy success!")
                return True
            else:
                log(f"   ❌ Buy failed: {result.stderr[:200]}")
                return False
                
        except subprocess.TimeoutExpired:
            log(f"   ⏱️ Buy timeout")
            return False
        except Exception as e:
            log(f"   ❌ Buy error: {e}")
            return False
    
    def _print_stats(self):
        """Print current stats"""
        log(f"   📊 Stats: Seen={self.whales_seen} Followed={self.whales_followed} Skipped={self.whales_skipped}")


async def test_honeypot(token: str) -> bool:
    """Test if token is honeypot by simulating sell"""
    log(f"🧪 Testing honeypot for {token[:16]}...")
    
    try:
        # Get buy quote for small amount
        amount_wei = int(0.1 * 10**18)  # 0.1 MON
        
        cmd_buy = f'cast call {LENS} "getTokenBuyQuote(address,uint256)" {token} {amount_wei} --rpc-url {RPC_URL}'
        result = subprocess.run(cmd_buy, shell=True, capture_output=True, text=True, timeout=10)
        
        if result.returncode != 0:
            log(f"   ❌ Can't get buy quote")
            return True  # Assume honeypot
        
        tokens_out = int(result.stdout.strip(), 16) if result.stdout.strip() else 0
        if tokens_out == 0:
            log(f"   ❌ Zero tokens from buy")
            return True
        
        # Get sell quote
        cmd_sell = f'cast call {LENS} "getTokenSellQuote(address,uint256)" {token} {tokens_out} --rpc-url {RPC_URL}'
        result = subprocess.run(cmd_sell, shell=True, capture_output=True, text=True, timeout=10)
        
        if result.returncode != 0:
            log(f"   ❌ Can't get sell quote")
            return True
        
        mon_back = int(result.stdout.strip(), 16) if result.stdout.strip() else 0
        mon_back_float = mon_back / 10**18
        
        # Calculate tax
        tax = 1 - (mon_back_float / 0.1) if mon_back_float > 0 else 1
        
        log(f"   Buy: 0.1 MON → {tokens_out} tokens")
        log(f"   Sell: {tokens_out} tokens → {mon_back_float:.6f} MON")
        log(f"   Tax: {tax*100:.1f}%")
        
        if tax > 0.15:  # More than 15% tax = honeypot
            log(f"   🚫 HONEYPOT DETECTED!")
            return True
        
        log(f"   ✅ Not a honeypot")
        return False
        
    except Exception as e:
        log(f"   ❌ Honeypot test error: {e}")
        return True  # Assume honeypot on error


async def main():
    """Main entry point"""
    print("🐳 Whale Follower V2")
    print("With full risk management!")
    print()
    
    if not PRIVATE_KEY:
        print("❌ ERROR: PRIVATE_KEY not set in .env")
        sys.exit(1)
    
    follower = WhaleFollowerV2()
    
    try:
        await follower.start()
    except KeyboardInterrupt:
        print("\n🛑 Stopping...")
        await follower.stop()


if __name__ == "__main__":
    asyncio.run(main())
