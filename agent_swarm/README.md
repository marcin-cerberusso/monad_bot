# 🐝 AGENT SWARM - Multi-Agent Trading Architecture

## Architektura

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
