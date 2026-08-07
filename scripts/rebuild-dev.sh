#!/usr/bin/env bash
set -euo pipefail

# Schnelles Rebuild-Skript nur für den Dev-Bot (BrotBot)
# Auf der QNAP per SSH ausführen.

# === ANPASSEN ===
DEV_BOT_PATH="/share/Container/BrotBot"   # <-- Hier anpassen falls nötig
# ================

cd "$DEV_BOT_PATH"

echo "→ Building BrotBot…"
docker compose build --no-cache

echo "→ Restarting BrotBot…"
docker compose up -d --force-recreate

echo "→ Logs (Ctrl+C zum Beenden):"
docker compose logs -f --tail=50
