#!/usr/bin/env python3
"""
🐳 WHALE FOLLOWER DASHBOARD
Inspired by Jesse Trading Bot
"""

from flask import Flask, render_template, jsonify
from flask_cors import CORS
import json
import os
import sys
from datetime import datetime
from pathlib import Path

# Add parent dir for file_utils
sys.path.insert(0, str(Path(__file__).parent.parent))
from file_utils import safe_load_json

app = Flask(__name__)
CORS(app)

# Paths
BASE_DIR = Path(__file__).parent.parent
POSITIONS_FILE = BASE_DIR / "positions.json"
TRADES_FILE = BASE_DIR / "trades_history.json"
CONFIG_FILE = BASE_DIR / "config.json"

def load_json(filepath, default=None):
    """Safely load JSON file with file locking"""
    return safe_load_json(filepath, default if default is not None else {})

def get_positions():
    """Get current open positions with P&L calculations"""
    positions = load_json(POSITIONS_FILE, {})
    result = []
    
    for addr, pos in positions.items():
        # Calculate time held
        timestamp = pos.get('timestamp', 0)
        if timestamp:
            time_held = datetime.now() - datetime.fromtimestamp(timestamp)
            hours = time_held.total_seconds() / 3600
            time_str = f"{hours:.1f}h"
        else:
            time_str = "?"
        
        result.append({
            'token_address': addr,
            'token_name': pos.get('token_name', addr[:12]),
            'entry_mon': pos.get('amount_mon', 0),
            'current_value_mon': pos.get('current_value_mon', pos.get('amount_mon', 0)),
            'pnl': pos.get('pnl', 0),
            'pnl_pct': pos.get('pnl_pct', 0),
            'highest_value_mon': pos.get('highest_value_mon', 0),
            'score': pos.get('score', 0),
            'whale_buy_mon': pos.get('whale_buy_mon', 0),
            'time_held': time_str,
            'tp_level_1_taken': pos.get('tp_level_1_taken', False),
            'tp_level_2_taken': pos.get('tp_level_2_taken', False),
            'tp_level_3_taken': pos.get('tp_level_3_taken', False),
            'moonbag_secured': pos.get('moonbag_secured', False),
        })
    
    # Sort by P&L
    result.sort(key=lambda x: x['pnl_pct'], reverse=True)
    return result

def get_trades_history():
    """Get closed trades history"""
    trades = load_json(TRADES_FILE, [])
    return trades[-50:]  # Last 50 trades

def get_metrics():
    """Calculate portfolio metrics"""
    trades = load_json(TRADES_FILE, [])
    positions = load_json(POSITIONS_FILE, {})
    
    if not trades:
        return {
            'total_trades': 0,
            'winning_trades': 0,
            'losing_trades': 0,
            'win_rate': 0,
            'total_profit_mon': 0,
            'total_profit_pct': 0,
            'avg_win': 0,
            'avg_loss': 0,
            'largest_win': 0,
            'largest_loss': 0,
            'open_positions': len(positions),
            'open_pnl': 0,
        }
    
    wins = [t for t in trades if t.get('pnl', 0) > 0]
    losses = [t for t in trades if t.get('pnl', 0) < 0]
    
    total_profit = sum(t.get('pnl', 0) for t in trades)
    
    # Open positions P&L
    open_pnl = sum(pos.get('pnl', 0) for pos in positions.values())
    
    return {
        'total_trades': len(trades),
        'winning_trades': len(wins),
        'losing_trades': len(losses),
        'win_rate': (len(wins) / len(trades) * 100) if trades else 0,
        'total_profit_mon': total_profit,
        'total_profit_pct': (total_profit / 1000 * 100) if total_profit else 0,  # Assume 1000 MON starting
        'avg_win': (sum(t.get('pnl', 0) for t in wins) / len(wins)) if wins else 0,
        'avg_loss': (sum(t.get('pnl', 0) for t in losses) / len(losses)) if losses else 0,
        'largest_win': max((t.get('pnl', 0) for t in wins), default=0),
        'largest_loss': min((t.get('pnl', 0) for t in losses), default=0),
        'open_positions': len(positions),
        'open_pnl': open_pnl,
    }

# ═══════════════════════════════════════════════════════════════════════════════
# 🌐 ROUTES
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/')
def index():
    """Main dashboard page"""
    return render_template('dashboard.html')

@app.route('/api/positions')
def api_positions():
    """Get current positions"""
    return jsonify(get_positions())

@app.route('/api/trades')
def api_trades():
    """Get trade history"""
    return jsonify(get_trades_history())

@app.route('/api/metrics')
def api_metrics():
    """Get portfolio metrics"""
    return jsonify(get_metrics())

@app.route('/api/status')
def api_status():
    """Get bot status"""
    return jsonify({
        'status': 'running',
        'timestamp': datetime.now().isoformat(),
        'positions_count': len(load_json(POSITIONS_FILE, {})),
    })

if __name__ == '__main__':
    print("🐳 Whale Follower Dashboard starting...")
    print(f"📁 Positions file: {POSITIONS_FILE}")
    print(f"📁 Trades file: {TRADES_FILE}")
    app.run(host='0.0.0.0', port=5000, debug=True)
