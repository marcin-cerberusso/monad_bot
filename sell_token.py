import json
import subprocess
import sys
import time
import os

token = sys.argv[1] if len(sys.argv) > 1 else None
if not token:
    print('Usage: python3 sell_token.py <token_address>')
    sys.exit(1)

# Cast path
CAST = os.path.expanduser('~/.foundry/bin/cast')

# Load .env
env = {}
with open('.env') as f:
    for line in f:
        if '=' in line and not line.startswith('#'):
            k, v = line.strip().split('=', 1)
            env[k] = v

pk = env.get('PRIVATE_KEY')
rpc = env.get('MONAD_RPC_URL')
wallet = '0x7b2897EA9547a6BB3c147b3E262483ddAb132A7D'
router = '0x6F6B8F1a20703309951a5127c45B49b1CD981A22'

print(f'Selling token: {token}')
print(f'Router: {router}')

# Get balance first
cmd_bal = f'{CAST} call {token} "balanceOf(address)" {wallet} --rpc-url {rpc}'
result = subprocess.run(cmd_bal, shell=True, capture_output=True, text=True)
balance_hex = result.stdout.strip()
if not balance_hex or balance_hex == '0x' + '0' * 64:
    print('No balance to sell!')
    sys.exit(0)
    
balance = int(balance_hex, 16)
print(f'Balance: {balance} ({balance/1e18:.2f} tokens)')

if balance == 0:
    print('No balance to sell!')
    sys.exit(0)

# Approve router
print('Approving router...')
cmd_approve = f'{CAST} send {token} "approve(address,uint256)" {router} 115792089237316195423570985008687907853269984665640564039457584007913129639935 --private-key {pk} --rpc-url {rpc}'
result = subprocess.run(cmd_approve, shell=True, capture_output=True, text=True)
if result.returncode != 0:
    print(f'Approve error: {result.stderr}')
    sys.exit(1)
print('Approved!')

# Sell via router
deadline = int(time.time()) + 300
min_out = 1  # slippage 100% (wyciągnij co się da)

# NAD.FUN sell function: sell((token, amount, minOut, recipient, deadline))
cmd_sell = f'{CAST} send {router} "sell((address,uint256,uint256,address,uint256))" "({token},{balance},{min_out},{wallet},{deadline})" --private-key {pk} --rpc-url {rpc} --gas-limit 500000'

print(f'Selling {balance/1e18:.2f} tokens...')
result = subprocess.run(cmd_sell, shell=True, capture_output=True, text=True)
if result.returncode != 0:
    print(f'Sell error: {result.stderr}')
    sys.exit(1)

print(f'SOLD!')
print(result.stdout)
