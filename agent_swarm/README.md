# 🐝 AGENT SWARM V2 - Multi-Agent Trading Architecture

## Overview

Agent Swarm V2 uses **Dragonfly** (Redis-compatible) as the central message bus for real-time inter-agent communication.

```
┌─────────────────────────────────────────────────────────────────┐
│                    🐉 DRAGONFLY MESSAGE BUS                      │
│         Real-time pub/sub, state management, consensus          │
└─────────────────────────────────────────────────────────────────┘
                              │
        ┌─────────────────────┼─────────────────────┐
        │                     │                     │
        ▼                     ▼                     ▼
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│ 🎭 ORCHESTR  │     │ 💰 SELL EXEC │     │ 🚀 LAUNCHER  │
│   V2         │     │   V2         │     │   V2         │
├──────────────┤     ├──────────────┤     ├──────────────┤
│ Routing      │     │ Trade exec   │     │ Price feed   │
│ Consensus    │     │ TX handling  │     │ Signal watch │
│ Analysis     │     │ Telegram     │     │ Health check │
└──────────────┘     └──────────────┘     └──────────────┘
```

## Components

### 🐉 Message Bus (`message_bus.py`)
- Dragonfly (Redis) connection with in-memory fallback
- Pub/sub channels per agent
- Consensus management
- State storage

### 📨 Message Types (`message_types.py`)
- `PRICE_UPDATE` - Token price changes
- `WHALE_ALERT` - Whale activity detected
- `NEW_TOKEN` - New token discovered
- `ANALYSIS_REQUEST/RESULT` - Analysis pipeline
- `TRADE_SIGNAL/EXECUTED` - Trade lifecycle
- `RISK_ALERT` - Risk warnings
- `CONSENSUS_REQUEST/VOTE/RESULT` - Voting

### 🎭 Orchestrator V2 (`orchestrator_v2.py`)
- Receives whale alerts & new tokens
- Requests analysis (DeepSeek)
- Manages consensus for high-value trades
- Routes trade signals

### 💰 Sell Executor V2 (`sell_executor_v2.py`)
- Listens for TRADE_SIGNAL (action=sell)
- Executes via Rust position_manager
- Broadcasts TRADE_EXECUTED
- Emergency sell handling

### 🚀 Launcher V2 (`launcher_v2.py`)
- CDN Price Feed
- File-based signal watchers (legacy)
- Health checks
- Heartbeat loop

## Quick Start

```bash
# Test Dragonfly connection
python agent_swarm/swarm_v2.py --test

# Run all components
python agent_swarm/swarm_v2.py

# Run individual components
python agent_swarm/swarm_v2.py --orchestrator
python agent_swarm/swarm_v2.py --sell-executor
python agent_swarm/swarm_v2.py --launcher
```

## Message Flow

```
whale_follower → WHALE_ALERT → Orchestrator
                                   │
                                   ▼
                          ANALYSIS_REQUEST
                                   │
                                   ▼ (DeepSeek)
                          ANALYSIS_RESULT
                                   │
                        ┌──────────┴──────────┐
                        ▼                     ▼
              (score < 70)              (score >= 70)
                 IGNORE              CONSENSUS_REQUEST
                                          │
                                    ┌─────┴─────┐
                                    ▼           ▼
                              VOTE approve  VOTE reject
                                    │           │
                                    └─────┬─────┘
                                          ▼
                              CONSENSUS_RESULT (approved?)
                                          │
                                          ▼
                                   TRADE_SIGNAL
                                          │
                                          ▼ (Sell Executor)
                                   TRADE_EXECUTED
```

## Configuration

```bash
# .env
DRAGONFLY_URL=rediss://default:xxx@xxx.dragonflydb.cloud:6385
WHALE_MIN_AMOUNT=5000
CONSENSUS_MIN_APPROVALS=2
HIGH_VALUE_THRESHOLD=500
```

## Architektura (Legacy)

```
┌─────────────────────────────────────────────────────────────────┐
│                    🧠 ORCHESTRATOR                               │
│         Zarządza komunikacją, routingiem, consensus             │
└─────────────────────────────────────────────────────────────────┘
                              │
                    ┌─────────▼─────────┐
                    │  📡 CDN LAYER     │
                    │  (Price Feed)     │
                    └─────────┬─────────┘
                              │
        ┌─────────────┬───────┴───────┬─────────────┐
        ▼             ▼               ▼             ▼
┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
│ 🔍 SCANNER   │ │ 📊 ANALYST   │ │ 💰 TRADER    │ │ 🛡️ RISK      │
│   Agent      │ │   Agent      │ │   Agent      │ │   Agent      │
├──────────────┤ ├──────────────┤ ├──────────────┤ ├──────────────┤
│ IZOLOWANY    │ │ IZOLOWANY    │ │ IZOLOWANY    │ │ IZOLOWANY    │
│ SANDBOX      │ │ SANDBOX      │ │ SANDBOX      │ │ SANDBOX      │
│ (HARD)       │ │ (HARD)       │ │ (STRICT)     │ │ (STRICT)     │
└──────────────┘ └──────────────┘ └──────────────┘ └──────────────┘
```

## Agenci

### 🔍 Scanner Agent
- Monitoring cen 24/7
- Wykrywanie whale movements
- Znajdowanie nowych tokenów
- **Isolation**: HARD

### 📊 Analyst Agent
- Analiza techniczna/fundamentalna
- Scoring tokenów (0-100)
- Sentiment analysis
- **Isolation**: HARD

### 💰 Trader Agent
- Wykonywanie transakcji
- Position sizing
- Entry/exit timing
- **Isolation**: STRICT (wymaga consensus!)

### 🛡️ Risk Agent
- Monitoring wszystkich pozycji
- Stop loss enforcement
- Portfolio exposure control
- **VETO power** dla dużych transakcji
- **Isolation**: STRICT

## Izolacja

Każdy agent działa w izolowanym sandbox:

```python
IsolationLevel.NONE   # Brak izolacji (niebezpieczne!)
IsolationLevel.SOFT   # Osobny context, wspólna pamięć
IsolationLevel.HARD   # Całkowita izolacja
IsolationLevel.STRICT # Izolacja + audyt komunikacji
```

### Korzyści izolacji:
1. **Brak cross-contamination** - agenci nie "zarażają" się swoimi halucynacjami
2. **Osobna pamięć** - każdy agent ma własny context
3. **Rate limiting** - każdy agent ma własny limit API
4. **Error boundary** - błąd jednego agenta nie psuje innych

## Komunikacja

Agenci komunikują się TYLKO przez Message Bus:

```
Scanner → Analyst      ✅ (whale alert)
Analyst → Trader       ✅ (analysis result)
Trader → Risk          ✅ (trade request)
Scanner → Trader       ❌ (forbidden!)
```

## Consensus

Dla ważnych decyzji (jak kupno) wymagany jest consensus:

1. Scanner wykrywa whale buy
2. Analyst analizuje i daje BUY signal
3. Trader prosi o consensus
4. Risk Agent głosuje (APPROVE/VETO)
5. Jeśli 2+ approvals → execute trade

## Uruchomienie

```bash
cd agent_swarm
python launcher.py
```

## CDN Price Feed

Real-time ceny z:
- NAD.FUN API
- On-chain events
- Whale transactions

```python
from cdn_price_feed import get_price_feed

feed = get_price_feed()
price = await feed.get_token_price("0x...")
```

## Pliki

- `orchestrator.py` - Główny orkiestrator i definicje agentów
- `cdn_price_feed.py` - Real-time price monitoring
- `agent_isolation.py` - System izolacji i sandbox
- `launcher.py` - Starter systemu
