#!/bin/bash

# ==============================================
# Schnelles Rebuild-Skript nur für den Dev-Bot (TestBot)
# ==============================================

export PATH=$PATH:/share/CACHEDEV1_DATA/.qpkg/container-station/bin

DEV_BOT_PATH="/share/Container/TestBot"   # <-- Hier anpassen falls nötig

echo "🚀 Starte Rebuild des Dev-Bots..."

if [ -d "$DEV_BOT_PATH" ]; then
    docker compose -f "$DEV_BOT_PATH/docker-compose.yml" down
    docker compose -f "$DEV_BOT_PATH/docker-compose.yml" up --build -d
    echo "✅ Dev-Bot erfolgreich neu gebaut"
else
    echo "❌ Dev-Bot Ordner nicht gefunden!"
fi