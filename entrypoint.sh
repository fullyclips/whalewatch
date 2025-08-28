#!/usr/bin/env bash
set -euo pipefail

CONFIG_PATH="${CONFIG_PATH:-/data/config.yaml}"

mkdir -p /data

# Generate config only if it doesn't exist yet (so autolearn can append to it later)
if [ ! -f "$CONFIG_PATH" ]; then
  echo "[entrypoint] generating initial config at $CONFIG_PATH"
  python /app/generate_config.py --out "$CONFIG_PATH"
else
  echo "[entrypoint] using existing config at $CONFIG_PATH"
fi

# Run the watcher (it will read DISCORD_WEBHOOK_URL from env)
exec python /app/whale_watcher.py --config "$CONFIG_PATH"
