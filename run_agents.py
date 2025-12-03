"""
🚀 MONAD BOT - Run All AI Agents

Usage:
    python3 run_agents.py          # Run all agents
    python3 run_agents.py --whale  # Run only whale agent
    python3 run_agents.py --test   # Test mode (no trades)
"""
import sys
import os

# Add agents to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import asyncio
from agents.orchestrator import main

if __name__ == "__main__":
    print("""
╔══════════════════════════════════════════════════════════════╗
║  🤖 MONAD BOT - AI AGENT TRADING SYSTEM                     ║
║                                                              ║
║  Agents:                                                     ║
║    🐳 WhaleAgent    - Detect whale buys                      ║
║    🛡️ RiskAgent     - Honeypot/FOMO check                    ║
║    🧠 AIAgent       - DeepSeek/Gemini analysis               ║
║    💰 TraderAgent   - Execute buy/sell                       ║
║    📊 PositionAgent - TP/SL/Trailing management              ║
║                                                              ║
║  Press Ctrl+C to stop                                        ║
╚══════════════════════════════════════════════════════════════╝
""")
    
    asyncio.run(main())
