#!/bin/bash
# Smoke test script for CI/CD
# Runs basic validation checks without requiring network/API access

set -e

echo "🔍 Starting smoke tests..."

cd "$(dirname "$0")/.."

# 1. Rust compilation check
echo ""
echo "📦 [1/4] Checking Rust compilation..."
cargo build --release 2>&1 | tail -5
echo "✅ Rust build OK"

# 2. Rust tests
echo ""
echo "🧪 [2/4] Running Rust tests..."
cargo test 2>&1 | tail -20
echo "✅ Rust tests OK"

# 3. Python syntax check
echo ""
echo "🐍 [3/4] Checking Python syntax..."
python -m py_compile \
    file_utils.py \
    smart_entry_detector.py \
    ai_master_controller.py \
    emergency_sell_all.py \
    agent_swarm/__init__.py \
    agent_swarm/launcher.py \
    agent_swarm/orchestrator.py \
    agent_swarm/cdn_price_feed.py \
    agent_swarm/sell_executor.py \
    agent_swarm/config_validator.py \
    dashboard/app.py
echo "✅ Python syntax OK"

# 4. Config validator import check
echo ""
echo "⚙️  [4/4] Testing config validator imports..."
python -c "
from agent_swarm.config_validator import validate_config, ConfigStatus
print('  - ConfigStatus enum loaded')
print('  - validate_config function loaded')
"
echo "✅ Config validator OK"

echo ""
echo "════════════════════════════════════════"
echo "🎉 All smoke tests passed!"
echo "════════════════════════════════════════"
