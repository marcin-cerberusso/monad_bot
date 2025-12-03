# Komunikacja między agentami Monad

System składa się z 5 niezależnych agentów komunikujących się przez Redis (lub in-memory bus).

## Przepływ danych (Flow)

1. **WhaleAgent** (`monad:whale`)
   - Nasłuchuje na WebSocket (`newPendingTransactions`).
   - Wykrywa duże zakupy (>200 MON) na routerze NAD.FUN.
   - Publikuje zdarzenie `WHALE_BUY` na kanał `monad:risk`.

2. **RiskAgent** (`monad:risk`)
   - Odbiera `WHALE_BUY`.
   - Sprawdza bezpieczeństwo tokena:
     - Honeypot (symulacja kupna/sprzedaży).
     - Płynność (DexScreener).
     - FOMO (czy cena nie wzrosła zbyt mocno w 1h).
   - Jeśli bezpieczny, publikuje `AI_ANALYZE` na kanał `monad:ai`.

3. **AIAgent** (`monad:ai`)
   - Odbiera `AI_ANALYZE`.
   - Analizuje token przy użyciu LLM (DeepSeek lub Gemini) lub reguł.
   - Decyduje o kupnie (BUY/SKIP).
   - Jeśli decyzja to BUY, publikuje `BUY_ORDER` na kanał `monad:trader`.

4. **TraderAgent** (`monad:trader`)
   - Odbiera `BUY_ORDER` lub `SELL_ORDER`.
   - Wykonuje transakcję na blockchainie (używając skryptów `buy_token.py` / `sell_token.py`).
   - Po sukcesie publikuje `TRADE_EXECUTED` na kanał `monad:position`.

5. **PositionAgent** (`monad:position`)
   - Monitoruje otwarte pozycje.
   - Sprawdza warunki wyjścia:
     - Stop Loss (-25%).
     - Take Profit (+30%, +60%).
     - Trailing Stop.
   - Gdy warunek spełniony, publikuje `SELL_ORDER` na kanał `monad:trader`.

## Struktura wiadomości

Każda wiadomość ma format JSON:
```json
{
  "id": "unique_id",
  "type": "message_type",
  "data": { ... },
  "sender": "AgentName",
  "timestamp": "ISO8601"
}
```

## Konfiguracja

Agenty korzystają z plików w katalogu `monad_engine/`:
- `buy_token.py` - skrypt kupna
- `sell_token.py` - skrypt sprzedaży
- `positions.json` - baza otwartych pozycji

## Uruchamianie

Uruchomienie wszystkich agentów przez orkiestratora:
```bash
python3 -m monad_engine.agents.orchestrator
```
