#!/usr/bin/env python3
"""
🛒 BUY TOKEN - Wykonuje zakup tokena przez NAD.FUN Router (pure web3.py)

NAD.FUN buy() function (from transaction analysis):
  Method ID: 0x6df9e92b
  Params: (uint256 minTokensOut, address token, address referrer, uint256 deadline)
  
Usage: python3 buy_token.py <token_address> <amount_mon>
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

# Method ID from real NAD.FUN transactions
BUY_METHOD_ID = bytes.fromhex("6df9e92b")

# ERC20 ABI for balance check
ERC20_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
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


def encode_buy_calldata(min_tokens_out: int, token: str, referrer: str, deadline: int) -> bytes:
    """
    Encode buy() calldata exactly as whales do.
    
    Calldata structure:
    - bytes 0-4: method ID (0x6df9e92b)
    - bytes 4-36: minTokensOut (uint256, padded to 32 bytes)
    - bytes 36-68: token address (address, padded to 32 bytes)
    - bytes 68-100: referrer address (address, padded to 32 bytes)
    - bytes 100-132: deadline (uint256, padded to 32 bytes)
    """
    calldata = bytearray()
    
    # Method ID
    calldata.extend(BUY_METHOD_ID)
    
    # Param 0: minTokensOut (uint256)
    calldata.extend(min_tokens_out.to_bytes(32, 'big'))
    
    # Param 1: token address (padded to 32 bytes)
    token_bytes = bytes.fromhex(token[2:].lower())  # Remove 0x prefix
    calldata.extend(bytes(12))  # 12 zero bytes padding
    calldata.extend(token_bytes)  # 20 bytes address
    
    # Param 2: referrer address (padded to 32 bytes)
    referrer_bytes = bytes.fromhex(referrer[2:].lower())
    calldata.extend(bytes(12))  # 12 zero bytes padding
    calldata.extend(referrer_bytes)  # 20 bytes address
    
    # Param 3: deadline (uint256)
    calldata.extend(deadline.to_bytes(32, 'big'))
    
    return bytes(calldata)


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
    
    # Connect to Monad
    w3 = Web3(Web3.HTTPProvider(rpc))
    if not w3.is_connected():
        print("ERROR: Cannot connect to Monad RPC")
        sys.exit(1)
    
    print(f"🛒 Buying token: {token}")
    print(f"   Amount: {amount_mon} MON")
    print(f"   Router: {ROUTER}")
    print(f"   Wallet: {wallet}")
    
    # Calculate amounts
    amount_wei = int(Decimal(str(amount_mon)) * Decimal(10**18))
    deadline = int(time.time()) + 300  # 5 minutes
    
    # Min tokens out = 0 (same as whales)
    min_tokens_out = 0
    
    # Referrer = our wallet
    referrer = wallet
    
    # Build calldata exactly like whale transactions
    calldata = encode_buy_calldata(min_tokens_out, token, referrer, deadline)
    
    print(f"\n🚀 Executing buy...")
    print(f"   Min tokens out: {min_tokens_out}")
    print(f"   Referrer: {referrer}")
    print(f"   Deadline: {deadline}")
    print(f"   Calldata: 0x{calldata[:20].hex()}...")
    
    try:
        account = Account.from_key(pk)
        nonce = w3.eth.get_transaction_count(account.address)
        
        # Build raw transaction
        tx = {
            'to': Web3.to_checksum_address(ROUTER),
            'from': account.address,
            'value': amount_wei,
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
            print(f"\n✅ BUY SUCCESS!")
            print(f"   TX: {tx_hash.hex()}")
            print(f"   Gas used: {receipt['gasUsed']}")
            
            # Check token balance
            token_checksum = Web3.to_checksum_address(token)
            wallet_checksum = Web3.to_checksum_address(wallet)
            token_contract = w3.eth.contract(address=token_checksum, abi=ERC20_ABI)
            balance = token_contract.functions.balanceOf(wallet_checksum).call()
            print(f"   Token balance: {balance}")
        else:
            print(f"\n❌ TX reverted!")
            sys.exit(1)
            
    except Exception as e:
        print(f"\n❌ Buy failed!")
        print(f"   Error: {str(e)[:500]}")
        sys.exit(1)
    
    print("\n✅ Done!")


if __name__ == "__main__":
    main()
        sys.exit(1)
    
    print("\n✅ Done!")


if __name__ == "__main__":
    main()
