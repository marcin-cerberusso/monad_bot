# 🕵️ Monad Copy Trader

Bot do śledzenia i kopiowania ruchów "Smart Money" na sieci Monad.

## 🚀 Jak uruchomić

1. **Upewnij się że masz .env** skonfigurowany (RPC i Private Key).
2. **Uruchom bota:**

```bash
cd monad_engine
cargo run --release --bin copy_trader
```

## ⚙️ Konfiguracja

Edytuj plik `.env` i ustaw adres portfela, który chcesz śledzić:

```env
TARGET_WALLET=0x...TWOJ_ADRES_DO_SLEDZENIA...
```

## 📝 Jak to działa

1. Bot łączy się z węzłem Monad.
2. Nasłuchuje nowych bloków.
3. Sprawdza każdą transakcję w bloku.
4. Jeśli nadawcą jest śledzony portfel -> Alarmuje (w przyszłości: kopiuje).
