# 🤖 MONAD BOT - AI Agent Architecture

## 🏗️ System Overview

The system is built as a swarm of autonomous AI agents communicating via a message bus (Redis/Dragonfly or In-Memory).

### 🔄 Flow of Operations

1.  **🐳 WhaleAgent**
    *   **Role:** Listener
    *   **Input:** WebSocket stream from Monad Blockchain (`newPendingTransactions`)
    *   **Action:** Detects large buy transactions (> 200 MON) on NAD.FUN router.
    *   **Output:** Publishes `whale_buy` event to `monad:risk` channel.

2.  **🛡️ RiskAgent**
    *   **Role:** Gatekeeper
    *   **Input:** `whale_buy` events
    *   **Action:**
        *   Checks if token is a Honeypot (simulates buy/sell via `cast`).
        *   Checks Liquidity (via DexScreener API).
        *   Checks FOMO (1h price change).
    *   **Output:** If safe, publishes `risk_verified` event to `monad:ai` channel.

3.  **🧠 AIAgent**
    *   **Role:** Analyst
    *   **Input:** `risk_verified` events
    *   **Action:**
        *   Constructs a prompt with token data.
        *   Queries DeepSeek (Reasoning) or Gemini (Flash) for analysis.
        *   Decides `BUY` or `SKIP` based on confidence score.
    *   **Output:** If `BUY`, publishes `buy_order` event to `monad:trader` channel.

4.  **💰 TraderAgent**
    *   **Role:** Executor
    *   **Input:** `buy_order` or `sell_order` events
    *   **Action:**
        *   Executes transaction via `buy_token.py` or `sell_token.py` (using `cast`).
        *   Manages nonce and gas.
    *   **Output:** Publishes `trade_executed` event to `monad:position` channel.

5.  **📊 PositionAgent**
    *   **Role:** Manager
    *   **Input:** `trade_executed` events & `positions.json` file
    *   **Action:**
        *   Monitors active positions every 30s.
        *   Updates highest price for Trailing Stop.
        *   Checks Take Profit (TP1: +50%, TP2: +100%).
        *   Checks Stop Loss (-15%).
        *   Checks Trailing Stop (20% drop from ATH).
    *   **Output:** Publishes `sell_order` event to `monad:trader` channel when exit condition is met.

---

## 📂 Directory Structure

```
monad_bot/
├── agents/                 # AI Agent Modules
│   ├── __init__.py
│   ├── base_agent.py       # Base class & Message Bus
│   ├── whale_agent.py      # Whale Detection
│   ├── risk_agent.py       # Risk & Security
│   ├── ai_agent.py         # LLM Analysis
│   ├── trader_agent.py     # Execution
│   ├── position_agent.py   # Portfolio Management
│   ├── orchestrator.py     # System Manager
│   └── config.py           # Strategy Configuration
├── run_agents.py           # Main Entry Point
├── buy_token.py            # Buy Script (Cast wrapper)
├── sell_token.py           # Sell Script (Cast wrapper)
├── positions.json          # Active Positions State
├── portfolio.json          # Portfolio Stats
├── trades_history.json     # Trade Log
└── .env                    # Secrets (Keys, RPCs)
```

## 🚀 How to Run

```bash
# Start the full system
python3 run_agents.py

# Run in background (screen)
screen -dmS agents python3 run_agents.py
```
