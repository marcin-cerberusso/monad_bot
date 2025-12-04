#!/usr/bin/env python3
"""
💰 SELL TOKEN - Sprzedaje token przez NAD.FUN Router (pure web3.py)

NAD.FUN sell() function (from transaction analysis):
  Method ID: 0x5de3085d
  Params: (uint256 amount, uint256 minMonOut, address token, address recipient, uint256 deadline)
  
Usage: python3 sell_token.py <token_address> [percent]
       percent = 100 (default) means sell all
"""

import sys
import time
from pathlib import Path
from decimal import Decimal
from web3 import Web3
from eth_account import Account

BASE_DIR = Path(__file__).parent

# NAD.FUN Router address
ROUTER = "0x6F6B8F1a20703309951a5127c45B49b1CD981A22"

# Method ID from real NAD.FUN sell transactions
SELL_METHOD_ID = bytes.fromhex("5de3085d")

# ERC20 ABI
ERC20_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function"
    },
    {
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"}
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function"
    }
]


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


def encode_sell_calldata(amount: int, min_mon_out: int, token: str, recipient: str, deadline: int) -> bytes:
    """
    Encode sell() calldata exactly as real transactions.
    
    Calldata structure:
    - bytes 0-4: method ID (0x5de3085d)
    - bytes 4-36: amount (uint256)
    - bytes 36-68: minMonOut (uint256)
    - bytes 68-100: token address (address, padded to 32 bytes)
    - bytes 100-132: recipient address (address, padded to 32 bytes)
    - bytes 132-164: deadline (uint256)
    """
    calldata = bytearray()
    
    # Method ID
    calldata.extend(SELL_METHOD_ID)
    
    # Param 0: amount (uint256)
    calldata.extend(amount.to_bytes(32, 'big'))
    
    # Param 1: minMonOut (uint256)
    calldata.extend(min_mon_out.to_bytes(32, 'big'))
    
    # Param 2: token address (padded to 32 bytes)
    token_bytes = bytes.fromhex(token[2:].lower())
    calldata.extend(bytes(12))  # 12 zero bytes padding
    calldata.extend(token_bytes)  # 20 bytes address
    
    # Param 3: recipient address (padded to 32 bytes)
    recipient_bytes = bytes.fromhex(recipient[2:].lower())
    calldata.extend(bytes(12))
    calldata.extend(recipient_bytes)
    
    # Param 4: deadline (uint256)
    calldata.extend(deadline.to_bytes(32, 'big'))
    
    return bytes(calldata)


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 sell_token.py <token_address> [percent]")
        sys.exit(1)
    
    token = sys.argv[1]
    percent = int(sys.argv[2]) if len(sys.argv) > 2 else 100
    
    if percent <= 0 or percent > 100:
        print("Percent must be between 1 and 100")
        sys.exit(1)
    
    env = load_env()
    pk = env.get('PRIVATE_KEY')
    rpc = env.get('MONAD_RPC_URL')
    wallet = env.get('WALLET_ADDRESS', '0x7b2897EA9547a6BB3c147b3E262483ddAb132A7D')
    
    if not pk or not rpc:
        print("ERROR: Missing PRIVATE_KEY or MONAD_RPC_URL in .env")
        sys.exit(1)
    
    # Connect to Monad
    w3 = Web3(Web3.HTTPProvider(rpc))
    if not w3.is_connected():
        print("ERROR: Cannot connect to Monad RPC")
        sys.exit(1)
    
    token_checksum = Web3.to_checksum_address(token)
    wallet_checksum = Web3.to_checksum_address(wallet)
    router_checksum = Web3.to_checksum_address(ROUTER)
    
    # Get token balance
    token_contract = w3.eth.contract(address=token_checksum, abi=ERC20_ABI)
    balance = token_contract.functions.balanceOf(wallet_checksum).call()
    
    if balance == 0:
        print("No balance to sell!")
        sys.exit(0)
    
    # Calculate sell amount
    sell_amount = (balance * percent) // 100
    
    print(f"💰 Selling token: {token}")
    print(f"   Balance: {balance} ({balance/1e18:.4f} tokens)")
    print(f"   Selling: {percent}% = {sell_amount} ({sell_amount/1e18:.4f} tokens)")
    print(f"   Router: {ROUTER}")
    print(f"   Wallet: {wallet}")
    
    account = Account.from_key(pk)
    
    # Step 1: Approve router to spend tokens
    print(f"\n📝 Approving router...")
    try:
        nonce = w3.eth.get_transaction_count(account.address)
        
        approve_tx = token_contract.functions.approve(
            router_checksum,
            sell_amount
        ).build_transaction({
            'from': account.address,
            'gas': 100000,
            'gasPrice': w3.eth.gas_price,
            'nonce': nonce,
            'chainId': w3.eth.chain_id
        })
        
        signed_approve = w3.eth.account.sign_transaction(approve_tx, pk)
        approve_hash = w3.eth.send_raw_transaction(signed_approve.raw_transaction)
        
        print(f"   Approve TX: {approve_hash.hex()}")
        
        # Wait for approval
        approve_receipt = w3.eth.wait_for_transaction_receipt(approve_hash, timeout=30)
        if approve_receipt['status'] != 1:
            print("   ❌ Approve failed!")
            sys.exit(1)
        print("   ✅ Approved!")
        
    except Exception as e:
        print(f"   ❌ Approve error: {e}")
        sys.exit(1)
    
    # Step 2: Sell tokens
    print(f"\n🚀 Executing sell...")
    
    deadline = int(time.time()) + 300  # 5 minutes
    min_mon_out = 1  # Accept any amount (100% slippage - get whatever we can)
    
    # Build calldata
    calldata = encode_sell_calldata(sell_amount, min_mon_out, token, wallet, deadline)
    
    print(f"   Min MON out: {min_mon_out}")
    print(f"   Deadline: {deadline}")
    print(f"   Calldata: 0x{calldata[:20].hex()}...")
    
    try:
        nonce = w3.eth.get_transaction_count(account.address)
        
        # Build raw transaction (sell doesn't send value)
        tx = {
            'to': router_checksum,
            'from': account.address,
            'value': 0,  # No MON sent for sell
            'gas': 500000,
            'gasPrice': w3.eth.gas_price,
            'nonce': nonce,
            'chainId': w3.eth.chain_id,
            'data': calldata
        }
        
        # Sign and send
        signed_tx = w3.eth.account.sign_transaction(tx, pk)
        tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
        
        print(f"   TX sent: {tx_hash.hex()}")
        
        # Wait for receipt
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
        
        if receipt['status'] == 1:
            print(f"\n✅ SELL SUCCESS!")
            print(f"   TX: {tx_hash.hex()}")
            print(f"   Gas used: {receipt['gasUsed']}")
            
            # Check remaining balance
            new_balance = token_contract.functions.balanceOf(wallet_checksum).call()
            print(f"   Remaining balance: {new_balance} ({new_balance/1e18:.4f} tokens)")
            
            # Check MON balance
            mon_balance = w3.eth.get_balance(wallet_checksum)
            print(f"   MON balance: {mon_balance/1e18:.4f} MON")
        else:
            print(f"\n❌ TX reverted!")
            sys.exit(1)
            
    except Exception as e:
        print(f"\n❌ Sell failed!")
        print(f"   Error: {str(e)[:500]}")
        sys.exit(1)
    
    print("\n✅ Done!")


if __name__ == "__main__":
    main()
