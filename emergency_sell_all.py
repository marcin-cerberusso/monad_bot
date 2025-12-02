#!/usr/bin/env python3
"""
🚨 EMERGENCY SELL ALL POSITIONS
Sprzedaje wszystkie otwarte pozycje żeby odzyskać kapitał
"""

import json
import os
import time
from web3 import Web3
from eth_account import Account
from dotenv import load_dotenv
from file_utils import safe_load_json, safe_save_json

load_dotenv()

# Config
RPC_URL = os.getenv("MONAD_RPC_URL", "https://monad-mainnet.g.alchemy.com/v2/FPgsxxE5R86qHQ200z04i")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
ROUTER = "0x6F6B8F1a20703309951a5127c45B49b1CD981A22"

# ABI
ERC20_ABI = [
    {"name": "balanceOf", "type": "function", "inputs": [{"name": "account", "type": "address"}], "outputs": [{"type": "uint256"}]},
    {"name": "approve", "type": "function", "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}], "outputs": [{"type": "bool"}]},
]

ROUTER_ABI = [
    {
        "name": "sell",
        "type": "function",
        "inputs": [{
            "name": "params",
            "type": "tuple",
            "components": [
                {"name": "amountIn", "type": "uint256"},
                {"name": "amountOutMin", "type": "uint256"},
                {"name": "token", "type": "address"},
                {"name": "to", "type": "address"},
                {"name": "deadline", "type": "uint256"},
            ]
        }],
        "outputs": []
    }
]

def main():
    print("🚨 EMERGENCY SELL ALL POSITIONS")
    print("=" * 60)
    
    # Connect
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    if not w3.is_connected():
        print("❌ Cannot connect to RPC")
        return
    
    account = Account.from_key(PRIVATE_KEY)
    my_address = account.address
    print(f"👤 Wallet: {my_address}")
    
    balance = w3.eth.get_balance(my_address)
    print(f"💰 MON Balance: {w3.from_wei(balance, 'ether'):.4f}")
    
    # Load positions with file locking
    positions = safe_load_json("positions.json", {})
    
    if not positions:
        print("⚠️ No positions found or file empty")
        return
    
    print(f"📊 Positions to sell: {len(positions)}")
    print("=" * 60)
    
    router = w3.eth.contract(address=Web3.to_checksum_address(ROUTER), abi=ROUTER_ABI)
    
    sold = []
    failed = []
    total_recovered = 0
    
    for token_addr, pos in positions.items():
        token_name = pos.get("token_name", token_addr[:12])
        entry_mon = pos.get("amount_mon", 0)
        
        print(f"\n🔄 Selling {token_name}...")
        
        try:
            token_addr_cs = Web3.to_checksum_address(token_addr)
            token = w3.eth.contract(address=token_addr_cs, abi=ERC20_ABI)
            
            # Get balance
            token_balance = token.functions.balanceOf(my_address).call()
            if token_balance == 0:
                print(f"   ⚠️ No balance, skipping")
                continue
            
            print(f"   💰 Token balance: {token_balance / 1e18:.2f}")
            
            # Approve
            nonce = w3.eth.get_transaction_count(my_address)
            approve_tx = token.functions.approve(ROUTER, token_balance).build_transaction({
                'from': my_address,
                'gas': 100000,
                'gasPrice': w3.eth.gas_price,
                'nonce': nonce,
                'chainId': 143
            })
            signed = account.sign_transaction(approve_tx)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            print(f"   🔐 Approved")
            
            time.sleep(1)
            
            # Sell
            deadline = int(time.time()) + 300
            sell_params = (
                token_balance,      # amountIn
                1,                  # amountOutMin (accept any)
                token_addr_cs,      # token
                my_address,         # to
                deadline            # deadline
            )
            
            nonce = w3.eth.get_transaction_count(my_address)
            sell_tx = router.functions.sell(sell_params).build_transaction({
                'from': my_address,
                'gas': 500000,
                'gasPrice': w3.eth.gas_price + 100000000000,  # +100 gwei priority
                'nonce': nonce,
                'chainId': 143
            })
            signed = account.sign_transaction(sell_tx)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            
            if receipt['status'] == 1:
                print(f"   ✅ SOLD! TX: {tx_hash.hex()}")
                sold.append(token_addr)
            else:
                print(f"   ❌ TX failed")
                failed.append(token_addr)
                
        except Exception as e:
            print(f"   ❌ Error: {e}")
            failed.append(token_addr)
        
        time.sleep(0.5)
    
    # Update positions.json with file locking
    for addr in sold:
        if addr in positions:
            del positions[addr]
    
    safe_save_json("positions.json", positions)
    
    # Final balance
    final_balance = w3.eth.get_balance(my_address)
    recovered = w3.from_wei(final_balance - balance, 'ether')
    
    print("\n" + "=" * 60)
    print("📊 SUMMARY")
    print(f"   ✅ Sold: {len(sold)}")
    print(f"   ❌ Failed: {len(failed)}")
    print(f"   💰 Recovered: {recovered:.4f} MON")
    print(f"   💰 Final Balance: {w3.from_wei(final_balance, 'ether'):.4f} MON")

if __name__ == "__main__":
    main()
