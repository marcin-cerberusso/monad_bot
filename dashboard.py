#!/usr/bin/env python3
"""
📊 MONAD BOT DASHBOARD - Real-time monitoring CLI
"""
import os
import sys
import json
import time
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.layout import Layout
    from rich.live import Live
    from rich.text import Text
    from rich import box
except ImportError:
    print("Installing rich...")
    subprocess.run([sys.executable, "-m", "pip", "install", "rich", "--break-system-packages"], 
                   capture_output=True)
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.layout import Layout
    from rich.live import Live
    from rich.text import Text
    from rich import box

from web3 import Web3

console = Console()

# Config
BASE_DIR = Path(__file__).parent
POSITIONS_FILE = BASE_DIR / "positions.json"
TRADES_FILE = BASE_DIR / "trades.json"
LOG_FILE = BASE_DIR / "bot.log"

# Load env
env = {}
try:
    with open(BASE_DIR / ".env") as f:
        for line in f:
            if '=' in line and not line.startswith('#'):
                k, v = line.strip().split('=', 1)
                env[k] = v
except:
    pass

RPC_URL = env.get("ALCHEMY_RPC") or env.get("RPC_URL") or "https://monad-mainnet.g.alchemy.com/v2/FPgsxxE5R86qHQ200z04i"
WALLET = env.get("WALLET_ADDRESS") or "0x7b2897EA9547a6BB3c147b3E262483ddAb132A7D"


def get_wallet_balance() -> float:
    """Get wallet MON balance"""
    try:
        w3 = Web3(Web3.HTTPProvider(RPC_URL))
        balance = w3.eth.get_balance(Web3.to_checksum_address(WALLET))
        return balance / 10**18
    except:
        return 0.0


def load_positions() -> Dict:
    """Load positions from JSON"""
    try:
        if POSITIONS_FILE.exists():
            with open(POSITIONS_FILE) as f:
                return json.load(f)
    except:
        pass
    return {}


def load_trades() -> list:
    """Load trade history"""
    try:
        if TRADES_FILE.exists():
            with open(TRADES_FILE) as f:
                return json.load(f)
    except:
        pass
    return []


def get_recent_logs(n: int = 15) -> list:
    """Get last N log lines"""
    try:
        if LOG_FILE.exists():
            with open(LOG_FILE) as f:
                lines = f.readlines()
                return [l.strip() for l in lines[-n:]]
    except:
        pass
    return []


def is_bot_running() -> bool:
    """Check if bot process is running"""
    try:
        result = subprocess.run(
            ["pgrep", "-f", "agents.orchestrator"],
            capture_output=True, text=True
        )
        return result.returncode == 0
    except:
        return False


def format_time_ago(iso_time: str) -> str:
    """Format time as 'X ago'"""
    try:
        dt = datetime.fromisoformat(iso_time.replace('Z', '+00:00'))
        diff = datetime.now() - dt
        
        if diff.total_seconds() < 60:
            return f"{int(diff.total_seconds())}s ago"
        elif diff.total_seconds() < 3600:
            return f"{int(diff.total_seconds() / 60)}m ago"
        elif diff.total_seconds() < 86400:
            return f"{int(diff.total_seconds() / 3600)}h ago"
        else:
            return f"{int(diff.total_seconds() / 86400)}d ago"
    except:
        return "unknown"


def create_header() -> Panel:
    """Create header panel"""
    bot_status = "🟢 RUNNING" if is_bot_running() else "🔴 STOPPED"
    balance = get_wallet_balance()
    
    header_text = Text()
    header_text.append("🤖 MONAD BOT DASHBOARD\n", style="bold cyan")
    header_text.append(f"Status: {bot_status}  ", style="bold")
    header_text.append(f"Balance: {balance:.2f} MON  ", style="green")
    header_text.append(f"Time: {datetime.now().strftime('%H:%M:%S')}")
    
    return Panel(header_text, box=box.DOUBLE)


def create_positions_table() -> Table:
    """Create positions table"""
    positions = load_positions()
    
    table = Table(title="📊 Open Positions", box=box.ROUNDED, show_header=True)
    table.add_column("Token", style="cyan", width=12)
    table.add_column("Entry", justify="right", style="green")
    table.add_column("Current", justify="right")
    table.add_column("PnL %", justify="right")
    table.add_column("Time", style="dim")
    table.add_column("Status", justify="center")
    
    for token, pos in positions.items():
        entry = pos.get('entry_value', pos.get('amount_mon', 0))
        current = pos.get('current_value', entry)
        pnl = pos.get('pnl_percent', 0)
        entry_time = pos.get('entry_time', '')
        
        # PnL color
        pnl_style = "green" if pnl >= 0 else "red"
        pnl_text = f"{pnl:+.1f}%"
        
        # Status based on TP/SL
        status = "⏳"
        if pnl >= 100:
            status = "🚀 TP2"
        elif pnl >= 50:
            status = "💰 TP1"
        elif pnl <= -15:
            status = "🛑 SL"
        
        table.add_row(
            token[:10] + "...",
            f"{entry:.2f}",
            f"{current:.2f}",
            Text(pnl_text, style=pnl_style),
            format_time_ago(entry_time),
            status
        )
    
    if not positions:
        table.add_row("No positions", "", "", "", "", "")
    
    return table


def create_logs_panel() -> Panel:
    """Create logs panel"""
    logs = get_recent_logs(12)
    
    log_text = Text()
    for log in logs:
        # Color based on content
        if "ERROR" in log or "❌" in log:
            style = "red"
        elif "✅" in log or "SUCCESS" in log:
            style = "green"
        elif "🐳" in log or "WHALE" in log:
            style = "cyan"
        elif "🛒" in log or "BUY" in log:
            style = "yellow"
        elif "💸" in log or "SELL" in log:
            style = "magenta"
        else:
            style = "dim"
        
        # Truncate long lines
        if len(log) > 80:
            log = log[:77] + "..."
        log_text.append(log + "\n", style=style)
    
    return Panel(log_text, title="📜 Recent Logs", box=box.ROUNDED)


def create_stats_panel() -> Panel:
    """Create stats panel"""
    positions = load_positions()
    trades = load_trades()
    
    total_invested = sum(p.get('entry_value', p.get('amount_mon', 0)) for p in positions.values())
    total_current = sum(p.get('current_value', p.get('amount_mon', 0)) for p in positions.values())
    unrealized_pnl = total_current - total_invested
    
    # Calculate realized PnL from trades
    realized_pnl = 0
    wins = 0
    losses = 0
    for trade in trades:
        if trade.get('action') == 'sell':
            pnl = trade.get('pnl', 0)
            realized_pnl += pnl
            if pnl > 0:
                wins += 1
            else:
                losses += 1
    
    win_rate = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0
    
    stats_text = Text()
    stats_text.append(f"Open Positions: {len(positions)}\n", style="cyan")
    stats_text.append(f"Total Invested: {total_invested:.2f} MON\n", style="yellow")
    stats_text.append(f"Current Value:  {total_current:.2f} MON\n")
    
    pnl_style = "green" if unrealized_pnl >= 0 else "red"
    stats_text.append(f"Unrealized PnL: {unrealized_pnl:+.2f} MON\n", style=pnl_style)
    
    real_style = "green" if realized_pnl >= 0 else "red"
    stats_text.append(f"Realized PnL:   {realized_pnl:+.2f} MON\n", style=real_style)
    
    stats_text.append(f"\nWin Rate: {win_rate:.0f}% ({wins}W/{losses}L)")
    
    return Panel(stats_text, title="📈 Stats", box=box.ROUNDED)


def main():
    """Main dashboard loop"""
    console.clear()
    
    try:
        with Live(console=console, refresh_per_second=0.5) as live:
            while True:
                # Create layout
                layout = Layout()
                layout.split_column(
                    Layout(name="header", size=4),
                    Layout(name="body")
                )
                layout["body"].split_row(
                    Layout(name="left"),
                    Layout(name="right", size=35)
                )
                layout["left"].split_column(
                    Layout(name="positions"),
                    Layout(name="logs")
                )
                
                # Fill layout
                layout["header"].update(create_header())
                layout["positions"].update(create_positions_table())
                layout["logs"].update(create_logs_panel())
                layout["right"].update(create_stats_panel())
                
                live.update(layout)
                time.sleep(2)
                
    except KeyboardInterrupt:
        console.print("\n[dim]Dashboard closed[/dim]")
        sys.exit(0)


if __name__ == "__main__":
    main()
