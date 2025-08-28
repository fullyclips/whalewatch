#!/usr/bin/env bash
set -euo pipefail

CONFIG_PATH="${CONFIG_PATH:-/data/config.yaml}"
PING_ON_BOOT="${PING_ON_BOOT:-1}"

mkdir -p /data

# One-shot rebuild of /data/config.yaml if requested
if [[ "${REGENERATE_CONFIG:-0}" == "1" ]]; then
  echo "[entrypoint] REGENERATE_CONFIG=1 -> removing $CONFIG_PATH"
  rm -f "$CONFIG_PATH"
fi

# (Re)generate config if missing
if [ ! -f "$CONFIG_PATH" ]; then
  echo "[entrypoint] generating initial config at $CONFIG_PATH"
  python /app/generate_config.py --out "$CONFIG_PATH"
else
  echo "[entrypoint] using existing config at $CONFIG_PATH"
fi

# Optional: append Solana whales via env var (comma-separated list)
if [[ -n "${APPEND_WHALES_SOL:-}" ]]; then
python - <<'PY'
import os, yaml
cfg_path = os.environ.get("CONFIG_PATH","/data/config.yaml")
append_csv = os.environ.get("APPEND_WHALES_SOL","")
addrs = [a.strip() for a in append_csv.replace("\n","").split(",") if a.strip()]
cfg = yaml.safe_load(open(cfg_path, "r")) or {}
wl = cfg.setdefault("whales_solana", [])
seen = {a.lower() for a in wl}
new = []
for a in addrs:
    if a.lower() not in seen:
        wl.append(a)
        seen.add(a.lower())
        new.append(a)
with open(cfg_path, "w") as f:
    yaml.safe_dump(cfg, f, sort_keys=False)
print(f"[entrypoint] appended {len(new)} Solana whales")
PY
fi

# ---- Boot ping to Discord (simple content message) ----
if [[ "$PING_ON_BOOT" == "1" && -n "${DISCORD_WEBHOOK_URL:-}" ]]; then
  now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  msg="🟢 WhaleWatch is alive — booted at ${now} (UTC)"
  curl -s -X POST -H "Content-Type: application/json" \
       -d "{\"content\":\"${msg}\"}" \
       "${DISCORD_WEBHOOK_URL}" >/dev/null || true
fi

exec python /app/whale_watcher.py --config "$CONFIG_PATH"
