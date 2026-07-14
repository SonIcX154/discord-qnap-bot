#!/bin/bash

# ==============================================
# Rebuild Script für alle 3 Discord Bots
# Einfach die Pfade unten anpassen und ausführen
# ==============================================

BOT1_PATH="/share/Container/LostBot"
BOT2_PATH="/share/Container/JuliaNPC"
BOT3_PATH="/share/Container/TestBot"

BOTS=("$BOT1_PATH" "$BOT2_PATH" "$BOT3_PATH")

echo "🚀 Starte Rebuild aller Bots..."
echo "=============================="

for bot in "${BOTS[@]}"; do
    if [ -d "$bot" ]; then
        echo ""
        echo "🔄 Rebuild für: $bot"
        cd "$bot" || { echo "❌ Konnte Ordner nicht betreten"; continue; }

        docker compose down
        docker compose up --build -d

        echo "✅ $bot erfolgreich neu gebaut"
    else
        echo "⚠️  Ordner nicht gefunden: $bot"
    fi
done

echo ""
echo "🎉 Alle Bots wurden neu gebaut!"