#!/usr/bin/env python3
"""
🐳 WHALE FOLLOWER DASHBOARD
Inspired by Jesse Trading Bot - with Real-time Logs
"""

from flask import Flask, render_template, jsonify, Response, request
from flask_cors import CORS
import json
import os
import sys
import re
import subprocess
from datetime import datetime
from pathlib import Path
from collections import deque
import threading
import time

# Add parent dir for file_utils
sys.path.insert(0, str(Path(__file__).parent.parent))
from file_utils import safe_load_json

app = Flask(__name__)
CORS(app)

# ═══════════════════════════════════════════════════════════════════════════════
# 📜 LOG MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════════

# Circular buffer for logs (last 500 lines per bot)
MAX_LOG_LINES = 500
logs_buffer = {
    'whale_follower': deque(maxlen=MAX_LOG_LINES),
    'position_manager': deque(maxlen=MAX_LOG_LINES),
    'take_profits': deque(maxlen=MAX_LOG_LINES),
}

# Log file paths (adjust for remote server)
LOG_FILES = {
    'whale_follower': Path.home() / 'monad_bot' / 'whale.log',
    'position_manager': Path.home() / 'monad_bot' / 'pm.log',
    'take_profits': Path.home() / 'monad_bot' / 'tp.log',
}

def parse_log_line(line: str) -> dict:
    """Parse a log line into structured format"""
    # Remove ANSI color codes first
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    clean_line = ansi_escape.sub('', line.strip())
    
    # Pattern: 2025-12-02T21:55:03.263985651+00:00  INFO whale_follower: message
    pattern = r'^(\d{4}-\d{2}-\d{2}T[\d:.]+)\+\d+:\d+\s+(\w+)\s+(\w+):\s*(.*)$'
    match = re.match(pattern, clean_line)
    
    if match:
        timestamp, level, source, message = match.groups()
        # Determine log type based on emojis/keywords
        log_type = 'info'
        if '❌' in message or 'ERROR' in level or 'SKIP' in message:
            log_type = 'error'
        elif '✅' in message or 'SUCCESS' in message or 'FOLLOWED' in message:
            log_type = 'success'
        elif '⚠️' in message or 'WARN' in level:
            log_type = 'warning'
        elif '🐳' in message or 'WHALE' in message:
            log_type = 'whale'
        elif '📊' in message:
            log_type = 'stats'
        elif '💰' in message or 'PROFIT' in message:
            log_type = 'profit'
            
        return {
            'timestamp': timestamp,
            'level': level,
            'source': source,
            'message': message,
            'type': log_type,
            'raw': clean_line
        }
    
    # Fallback for unstructured logs
    return {
        'timestamp': datetime.now().isoformat(),
        'level': 'INFO',
        'source': 'unknown',
        'message': clean_line,
        'type': 'info',
        'raw': clean_line
    }

def load_recent_logs(bot_name: str, lines: int = 100) -> list:
    """Load recent logs from file"""
    log_file = LOG_FILES.get(bot_name)
    if not log_file or not log_file.exists():
        return []
    
    try:
        # Use tail for efficiency
        result = subprocess.run(
            ['tail', '-n', str(lines), str(log_file)],
            capture_output=True,
            text=True,
            timeout=5
        )
        log_lines = result.stdout.strip().split('\n')
        return [parse_log_line(line) for line in log_lines if line.strip()]
    except Exception as e:
        return [{'timestamp': datetime.now().isoformat(), 'level': 'ERROR', 
                 'source': bot_name, 'message': f'Error reading logs: {e}', 
                 'type': 'error', 'raw': str(e)}]

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

# ═══════════════════════════════════════════════════════════════════════════════
# 📜 LOG ROUTES
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/api/logs/<bot_name>')
def api_logs(bot_name):
    """Get recent logs for a specific bot"""
    if bot_name not in LOG_FILES:
        return jsonify({'error': f'Unknown bot: {bot_name}'}), 404
    
    lines = request.args.get('lines', 100, type=int)
    lines = min(lines, 500)  # Cap at 500
    
    logs = load_recent_logs(bot_name, lines)
    return jsonify(logs)

@app.route('/api/logs/all')
def api_logs_all():
    """Get combined logs from all bots"""
    lines = request.args.get('lines', 50, type=int)
    all_logs = []
    
    for bot_name in LOG_FILES:
        bot_logs = load_recent_logs(bot_name, lines)
        all_logs.extend(bot_logs)
    
    # Sort by timestamp (newest first)
    all_logs.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
    return jsonify(all_logs[:lines * 2])

@app.route('/api/logs/stream/<bot_name>')
def api_logs_stream(bot_name):
    """Server-Sent Events stream for real-time logs"""
    if bot_name not in LOG_FILES:
        return jsonify({'error': f'Unknown bot: {bot_name}'}), 404
    
    def generate():
        log_file = LOG_FILES[bot_name]
        if not log_file.exists():
            yield f"data: {json.dumps({'error': 'Log file not found'})}\n\n"
            return
        
        # Use tail -f for streaming
        process = subprocess.Popen(
            ['tail', '-f', '-n', '20', str(log_file)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        
        try:
            for line in iter(process.stdout.readline, ''):
                if line.strip():
                    log_entry = parse_log_line(line)
                    yield f"data: {json.dumps(log_entry)}\n\n"
        finally:
            process.kill()
    
    return Response(generate(), mimetype='text/event-stream')

if __name__ == '__main__':
    print("🐳 Whale Follower Dashboard starting...")
    print(f"📁 Positions file: {POSITIONS_FILE}")
    print(f"📁 Trades file: {TRADES_FILE}")
    print(f"📜 Log files: {list(LOG_FILES.keys())}")
    app.run(host='0.0.0.0', port=5000, debug=True, threaded=True)
