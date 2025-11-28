#!/bin/bash
# Monad Auto-Sniper - Launcher
# Made by AI for easy bot management

cd "$(dirname "$0")"

case "$1" in
    start)
        echo "🚀 Uruchamiam Monad Auto-Sniper..."
        cargo run --release --bin copy_trader
        ;;
    
    bg)
        echo "🚀 Uruchamiam w tle (background)..."
        nohup cargo run --release --bin copy_trader > sniper.log 2>&1 &
        echo $! > sniper.pid
        echo "✅ Bot uruchomiony! PID: $(cat sniper.pid)"
        echo "📝 Logi: tail -f sniper.log"
        ;;
    
    stop)
        if [ -f sniper.pid ]; then
            PID=$(cat sniper.pid)
            echo "🛑 Zatrzymuję bota (PID: $PID)..."
            kill $PID 2>/dev/null
            rm sniper.pid
            echo "✅ Bot zatrzymany"
        else
            echo "⚠️  Bot nie działa (brak sniper.pid)"
        fi
        ;;
    
    status)
        if [ -f sniper.pid ]; then
            PID=$(cat sniper.pid)
            if ps -p $PID > /dev/null; then
                echo "🟢 Bot DZIAŁA (PID: $PID)"
                echo "📊 Ostatnie logi:"
                tail -n 10 sniper.log 2>/dev/null || echo "Brak logów"
            else
                echo "🔴 Bot NIE DZIAŁA (stary PID w pliku)"
                rm sniper.pid
            fi
        else
            echo "🔴 Bot NIE DZIAŁA"
        fi
        ;;
    
    logs)
        if [ -f sniper.log ]; then
            tail -f sniper.log
        else
            echo "⚠️  Brak logów (sniper.log)"
        fi
        ;;
    
    restart)
        echo "🔄 Restartuję bota..."
        $0 stop
        sleep 2
        $0 bg
        ;;
    
    stats)
        echo "📊 Statystyki:"
        if [ -f stats.json ]; then
            cat stats.json
        else
            echo "⚠️  Brak pliku stats.json"
        fi
        echo ""
        echo "📂 Pozycje:"
        if [ -f positions.json ]; then
            cat positions.json
        else
            echo "⚠️  Brak pliku positions.json"
        fi
        ;;
    
    *)
        echo "🎮 Monad Auto-Sniper v3.0 - Launcher"
        echo ""
        echo "Użycie: ./sniper.sh [komenda]"
        echo ""
        echo "Komendy:"
        echo "  start      - Uruchom bota (foreground)"
        echo "  bg         - Uruchom w tle (background)"
        echo "  stop       - Zatrzymaj bota"
        echo "  restart    - Restart bota"
        echo "  status     - Sprawdź czy działa"
        echo "  logs       - Zobacz logi na żywo"
        echo "  stats      - Pokaż statystyki i pozycje"
        echo ""
        echo "Przykład: ./sniper.sh bg"
        ;;
esac
