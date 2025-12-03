#!/usr/bin/env python3
"""
🛒 BUY TOKEN - Wykonuje zakup tokena przez NAD.FUN Router

Usage: python3 buy_token.py <token_address> <amount_mon>
"""

import json
import subprocess
import sys
import time
from pathlib import Path
from decimal import Decimal

BASE_DIR = Path(__file__).parent

def load_env():
    """Load .env file"""
    env = {}
    env_file = BASE_DIR / ".env"
    try:
        with open(env_file) as f:
            for line in f:
                if '=' in line and not line.startswith('#'):
                    k, v = line.strip().split('=', 1)
                    env[k] = v
    except:
        pass
    return env


def get_live_quote(token: str, amount_mon: float, rpc_url: str) -> tuple:
    """Get live quote from NAD.FUN Lens for expected tokens out"""
    lens = "0x7e78A8DE94f21804F7a17F4E8BF9EC2c872187ea"
    amount_wei = int(Decimal(str(amount_mon)) * Decimal(10**18))
    
    # getTokenBuyQuote(address token, uint256 monAmount)
    cmd = f'cast call {lens} "getTokenBuyQuote(address,uint256)" {token} {amount_wei} --rpc-url {rpc_url}'
    
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
        if result.returncode == 0 and result.stdout.strip():
            tokens_out = int(result.stdout.strip(), 16)
            return tokens_out, None
    except Exception as e:
        return 0, str(e)
    
    return 0, "Failed to get quote"


def main():
    if len(sys.argv) < 3:
        print("Usage: python3 buy_token.py <token_address> <amount_mon>")
        sys.exit(1)
    
    token = sys.argv[1]
    try:
        amount_mon = float(sys.argv[2])
    except:
        print(f"Invalid amount: {sys.argv[2]}")
        sys.exit(1)
    
    if amount_mon <= 0:
        print("Amount must be positive")
        sys.exit(1)
    
    env = load_env()
    pk = env.get('PRIVATE_KEY')
    rpc = env.get('MONAD_RPC_URL')
    wallet = env.get('WALLET_ADDRESS', '0x7b2897EA9547a6BB3c147b3E262483ddAb132A7D')
    
    if not pk or not rpc:
        print("ERROR: Missing PRIVATE_KEY or MONAD_RPC_URL in .env")
        sys.exit(1)
    
    # NAD.FUN Router
    router = "0x6F6B8F1a20703309951a5127c45B49b1CD981A22"
    
    print(f"🛒 Buying token: {token}")
    print(f"   Amount: {amount_mon} MON")
    print(f"   Router: {router}")
    
    # 1. Get live quote
    print("\n📊 Getting quote...")
    expected_tokens, err = get_live_quote(token, amount_mon, rpc)
    if err:
        print(f"⚠️ Quote error (continuing anyway): {err}")
        expected_tokens = 0
    else:
        print(f"   Expected tokens: {expected_tokens}")
    
    # 2. Calculate minimum out with slippage (5%)
    slippage = 0.05
    min_tokens_out = int(expected_tokens * (1 - slippage)) if expected_tokens > 0 else 1
    
    # 3. Prepare buy call
    amount_wei = int(Decimal(str(amount_mon)) * Decimal(10**18))
    deadline = int(time.time()) + 300  # 5 minutes
    
    # buy((address token, uint256 amountOutMin, address to, uint256 deadline)) payable
    # The value is sent as MON
    buy_params = f"({token},{min_tokens_out},{wallet},{deadline})"
    
    cmd_buy = (
        f'cast send {router} '
        f'"buy((address,uint256,address,uint256))" '
        f'"{buy_params}" '
        f'--value {amount_wei} '
        f'--private-key {pk} '
        f'--rpc-url {rpc} '
        f'--gas-limit 500000'
    )
    
    print(f"\n🚀 Executing buy...")
    print(f"   Min tokens out: {min_tokens_out}")
    print(f"   Deadline: {deadline}")
    
    result = subprocess.run(cmd_buy, shell=True, capture_output=True, text=True, timeout=60)
    
    if result.returncode != 0:
        print(f"\n❌ Buy failed!")
        print(f"   Error: {result.stderr[:500]}")
        sys.exit(1)
    
    print(f"\n✅ BUY SUCCESS!")
    
    # Parse tx hash from output
    if "transactionHash" in result.stdout or "0x" in result.stdout:
        lines = result.stdout.strip().split('\n')
        for line in lines:
            if "transactionHash" in line or (line.startswith("0x") and len(line) == 66):
                print(f"   TX: {line.strip()[:70]}")
                break
    
    # 4. Verify balance
    print("\n📊 Checking balance...")
    cmd_bal = f'cast call {token} "balanceOf(address)" {wallet} --rpc-url {rpc}'
    result = subprocess.run(cmd_bal, shell=True, capture_output=True, text=True, timeout=10)
    
    if result.returncode == 0 and result.stdout.strip():
        balance = int(result.stdout.strip(), 16)
        print(f"   Token balance: {balance}")
    
    print("\n✅ Done!")


if __name__ == "__main__":
    main()
