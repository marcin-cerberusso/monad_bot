#!/usr/bin/env python3
"""
📊 TRADING METRICS DASHBOARD - Real-time trading stats

Wyświetla:
- Portfolio value
- Open positions with P&L
- Trading stats (wins/losses)
- Risk metrics (blocked tokens, etc.)
- System health
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List
from dotenv import load_dotenv
import aiohttp

load_dotenv()

BASE_DIR = Path(__file__).parent
POSITIONS_FILE = BASE_DIR / "positions.json"
TRADES_FILE = BASE_DIR / "trades_history.json"
BLOCKED_FILE = BASE_DIR / "blocked_tokens.json"
METRICS_FILE = BASE_DIR / "risk_metrics.json"

RPC_URL = os.getenv("MONAD_RPC_URL")
WALLET = os.getenv("WALLET_ADDRESS", "0x7b2897EA9547a6BB3c147b3E262483ddAb132A7D")
LENS = "0x7e78A8DE94f21804F7a17F4E8BF9EC2c872187ea"


def clear_screen():
    """Clear terminal"""
    os.system('clear' if os.name != 'nt' else 'cls')


def load_json(path: Path) -> dict:
    """Load JSON file"""
    try:
        if path.exists():
            with open(path) as f:
                return json.load(f)
    except:
        pass
    return {}


async def get_mon_balance() -> float:
    """Get MON balance"""
    try:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_getBalance",
            "params": [WALLET, "latest"]
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(RPC_URL, json=payload, timeout=10) as resp:
                data = await resp.json()
                result = data.get("result", "0x0")
                return int(result, 16) / 10**18
    except:
        return 0


async def get_token_value(token: str, balance: int) -> float:
    """Get token value in MON"""
    if balance == 0:
        return 0
    
    try:
        # getTokenSellQuote(address token, uint256 amount)
        balance_hex = hex(balance)[2:].zfill(64)
        token_padded = token[2:].zfill(64)
        data = f"0x1b3f7e01{token_padded}{balance_hex}"
        
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_call",
            "params": [{"to": LENS, "data": data}, "latest"]
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(RPC_URL, json=payload, timeout=10) as resp:
                result_data = await resp.json()
                result = result_data.get("result", "0x0")
                return int(result, 16) / 10**18
    except:
        return 0


async def get_token_balance(token: str) -> int:
    """Get our token balance"""
    try:
        wallet_padded = WALLET[2:].lower().zfill(64)
        data = f"0x70a08231{wallet_padded}"
        
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_call",
            "params": [{"to": token, "data": data}, "latest"]
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(RPC_URL, json=payload, timeout=10) as resp:
                result_data = await resp.json()
                result = result_data.get("result", "0x0")
                return int(result, 16)
    except:
        return 0


def calculate_trade_stats(trades: list) -> dict:
    """Calculate trading statistics"""
    if not trades:
        return {
            "total": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0,
            "total_pnl": 0,
            "avg_pnl": 0,
            "best": 0,
            "worst": 0
        }
    
    wins = sum(1 for t in trades if t.get("pnl", 0) > 0)
    losses = sum(1 for t in trades if t.get("pnl", 0) < 0)
    pnls = [t.get("pnl", 0) for t in trades]
    
    return {
        "total": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate": wins / len(trades) * 100 if trades else 0,
        "total_pnl": sum(pnls),
        "avg_pnl": sum(pnls) / len(pnls) if pnls else 0,
        "best": max(pnls) if pnls else 0,
        "worst": min(pnls) if pnls else 0
    }


def get_blocked_count() -> tuple:
    """Get blocked token stats"""
    data = load_json(BLOCKED_FILE)
    blocked = data.get("blocked", {})
    
    now = time.time()
    active = sum(1 for b in blocked.values() if b.get("expires_at", 0) > now)
    
    # Count by reason
    reasons = {}
    for b in blocked.values():
        if b.get("expires_at", 0) > now:
            r = b.get("reason", "unknown")
            reasons[r] = reasons.get(r, 0) + 1
    
    return active, reasons


def get_screen_status() -> Dict[str, str]:
    """Get status of screen sessions"""
    import subprocess
    try:
        result = subprocess.run(
            ["screen", "-ls"],
            capture_output=True,
            text=True,
            timeout=5
        )
        
        screens = {}
        for line in result.stdout.split('\n'):
            if '.' in line and ('Detached' in line or 'Attached' in line):
                parts = line.strip().split('.')
                if len(parts) >= 2:
                    name = parts[1].split()[0]
                    status = "🟢" if "Detached" in line else "🟡"
                    screens[name] = status
        return screens
    except:
        return {}


async def render_dashboard():
    """Render the dashboard"""
    clear_screen()
    
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    print("=" * 70)
    print(f"  📊 MONAD TRADING DASHBOARD                       {now}")
    print("=" * 70)
    
    # Portfolio
    print("\n💰 PORTFOLIO")
    print("-" * 40)
    
    mon_balance = await get_mon_balance()
    print(f"  MON Balance:     {mon_balance:>12.4f} MON")
    
    # Positions
    positions = load_json(POSITIONS_FILE)
    total_invested = 0
    total_value = 0
    
    print(f"\n📈 OPEN POSITIONS ({len(positions)})")
    print("-" * 70)
    
    if positions:
        print(f"  {'Token':<20} {'Invested':>10} {'Value':>10} {'P&L':>10} {'%':>8}")
        print(f"  {'-'*20} {'-'*10} {'-'*10} {'-'*10} {'-'*8}")
        
        for token, pos in positions.items():
            invested = pos.get("amount_mon", pos.get("entry_price_mon", 0))
            total_invested += invested
            
            # Get current value
            balance = await get_token_balance(token)
            value = await get_token_value(token, balance)
            total_value += value
            
            pnl = value - invested
            pnl_pct = (pnl / invested * 100) if invested > 0 else 0
            
            emoji = "🟢" if pnl > 0 else "🔴" if pnl < 0 else "⚪"
            name = pos.get("token_name", token[:12] + "...")[:20]
            
            print(f"  {emoji} {name:<18} {invested:>10.2f} {value:>10.2f} {pnl:>+10.2f} {pnl_pct:>+7.1f}%")
        
        print(f"  {'-'*20} {'-'*10} {'-'*10} {'-'*10} {'-'*8}")
        total_pnl = total_value - total_invested
        total_pnl_pct = (total_pnl / total_invested * 100) if total_invested > 0 else 0
        print(f"  {'TOTAL':<20} {total_invested:>10.2f} {total_value:>10.2f} {total_pnl:>+10.2f} {total_pnl_pct:>+7.1f}%")
    else:
        print("  No open positions")
    
    # Trading Stats
    trades = load_json(TRADES_FILE)
    if isinstance(trades, list):
        stats = calculate_trade_stats(trades)
    else:
        stats = calculate_trade_stats(trades.get("trades", []))
    
    print(f"\n📊 TRADING STATS (All Time)")
    print("-" * 40)
    print(f"  Total Trades:    {stats['total']:>6}")
    print(f"  Wins / Losses:   {stats['wins']:>3} / {stats['losses']}")
    print(f"  Win Rate:        {stats['win_rate']:>6.1f}%")
    print(f"  Total P&L:       {stats['total_pnl']:>+10.2f} MON")
    print(f"  Avg P&L:         {stats['avg_pnl']:>+10.2f} MON")
    print(f"  Best Trade:      {stats['best']:>+10.2f} MON")
    print(f"  Worst Trade:     {stats['worst']:>+10.2f} MON")
    
    # Risk Metrics
    blocked_count, blocked_reasons = get_blocked_count()
    
    print(f"\n🛡️ RISK METRICS")
    print("-" * 40)
    print(f"  Blocked Tokens:  {blocked_count:>6}")
    for reason, count in sorted(blocked_reasons.items()):
        print(f"    - {reason}: {count}")
    
    # System Health
    screens = get_screen_status()
    
    print(f"\n⚙️ SYSTEM STATUS")
    print("-" * 40)
    
    expected = ["whale", "sniper", "pm_v2", "parser", "dev_mon"]
    for name in expected:
        status = screens.get(name, "🔴")
        print(f"  {status} {name}")
    
    # Net Worth
    net_worth = mon_balance + total_value
    print(f"\n{'='*70}")
    print(f"  💎 NET WORTH: {net_worth:.4f} MON")
    print(f"{'='*70}")
    
    print("\n  Press Ctrl+C to exit. Refreshing every 30s...")


async def main():
    """Main loop"""
    while True:
        try:
            await render_dashboard()
            await asyncio.sleep(30)
        except KeyboardInterrupt:
            print("\n\nBye! 👋")
            break
        except Exception as e:
            print(f"\n❌ Error: {e}")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
