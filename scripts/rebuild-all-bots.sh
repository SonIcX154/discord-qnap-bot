#!/usr/bin/env bash
set -euo pipefail

# Rebuild aller Bot-Container auf der QNAP.
# Pfade bei Bedarf anpassen.

# === ANPASSEN ===
BOT1_PATH="/share/Container/Bot1"          # <-- Hier anpassen
BOT2_PATH="/share/Container/Bot2"          # <-- Hier anpassen
BOT3_PATH="/share/Container/BrotBot"       # <-- Hier anpassen
# ================

rebuild_one() {
  local path="$1"
  local name
  name="$(basename "$path")"
  if [[ ! -d "$path" ]]; then
    echo "⚠  Skip $name — path not found: $path"
    return 0
  fi
  echo "→ Building $name…"
  (cd "$path" && docker compose build --no-cache && docker compose up -d --force-recreate)
  echo "✓ $name done"
}

rebuild_one "$BOT1_PATH"
rebuild_one "$BOT2_PATH"
rebuild_one "$BOT3_PATH"

echo "All rebuilds finished."
