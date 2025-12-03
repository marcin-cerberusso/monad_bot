import json
import subprocess
import sys

token = sys.argv[1] if len(sys.argv) > 1 else None
if not token:
    print('Usage: python3 sell_token.py <token_address>')
    sys.exit(1)

# Load .env
env = {}
with open('.env') as f:
    for line in f:
        if '=' in line and not line.startswith('#'):
            k, v = line.strip().split('=', 1)
            env[k] = v

pk = env.get('PRIVATE_KEY')
rpc = env.get('MONAD_RPC_URL')
router = '0x6F6B8F1a20703309951a5127c45B49b1CD981A22'

print(f'Selling token: {token}')
print(f'Router: {router}')

# Use cast to sell
# First approve
cmd_approve = f'cast send {token} "approve(address,uint256)" {router} 115792089237316195423570985008687907853269984665640564039457584007913129639935 --private-key {pk} --rpc-url {rpc}'
print('Approving...')
result = subprocess.run(cmd_approve, shell=True, capture_output=True, text=True)
if result.returncode != 0:
    print(f'Approve error: {result.stderr}')
else:
    print('Approved!')

# Get balance
cmd_bal = f'cast call {token} "balanceOf(address)" 0x7b2897ea9547a6bb3c147b3e262483ddab132a7d --rpc-url {rpc}'
result = subprocess.run(cmd_bal, shell=True, capture_output=True, text=True)
balance = int(result.stdout.strip(), 16) if result.stdout.strip() else 0
print(f'Balance: {balance}')

if balance == 0:
    print('No balance to sell!')
    sys.exit(0)

# Sell via router
import time
deadline = int(time.time()) + 300

# Encode sell params
# sell((address token, uint256 amount, uint256 amountOutMin, address to, uint256 deadline))
cmd_sell = f'cast send {router} "sell((address,uint256,uint256,address,uint256))" "({token},{balance},1,0x7b2897ea9547a6bb3c147b3e262483ddab132a7d,{deadline})" --private-key {pk} --rpc-url {rpc} --gas-limit 500000'
print('Selling...')
result = subprocess.run(cmd_sell, shell=True, capture_output=True, text=True)
if result.returncode != 0:
    print(f'Sell error: {result.stderr}')
else:
    print(f'SOLD! {result.stdout}')
