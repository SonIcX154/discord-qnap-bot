#!/bin/bash

# ==============================================
# Rebuild Script für alle Discord Bots (QNAP-kompatibel)
# ==============================================

export PATH=$PATH:/share/CACHEDEV1_DATA/.qpkg/container-station/bin

# Pfade zu deinen Bot-Ordnern (einfach anpassen)
BOT1_PATH="/share/Container/LostBot"      # <-- Hier anpassen
BOT2_PATH="/share/Container/JuliaNPC"     # <-- Hier anpassen
BOT3_PATH="/share/Container/TestBot"      # <-- Hier anpassen

BOTS=("$BOT1_PATH" "$BOT2_PATH" "$BOT3_PATH")

echo "🚀 Starte Rebuild aller Bots..."
echo "=============================="

for bot in "${BOTS[@]}"; do
    if [ -d "$bot" ]; then
        echo ""
        echo "🔄 Rebuild für: $bot"

        docker compose -f "$bot/docker-compose.yml" down
        docker compose -f "$bot/docker-compose.yml" up --build -d

        echo "✅ $bot erfolgreich neu gebaut"
    else
        echo "⚠️  Ordner nicht gefunden: $bot"
    fi
done

echo ""
echo "🎉 Alle Bots wurden neu gebaut!"