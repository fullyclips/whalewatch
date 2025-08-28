import os, argparse, yaml, json, sys
from pathlib import Path

# Defaults for DEX program IDs on Solana
DEFAULT_SOL_PROGRAMS = [
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",  # Raydium AMM v4
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",  # Raydium CLMM
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C",  # Raydium CPMM
    "whirLbMiicVdio4qvUfM5KAg6Ct8bWpYzGfF3uctyCc",  # Orca Whirlpools
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",  # Jupiter swap
]

def env(name, default=None, required=False):
    v = os.getenv(name, default)
    if required and not v:
        print(f"[generate_config] Missing required env: {name}", file=sys.stderr)
        sys.exit(1)
    return v

def csv_env(name):
    v = os.getenv(name, "")
    return [x.strip() for x in v.split(",") if x.strip()]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    # EVM RPC (WS + HTTP)
    ETH_WS   = env("ETH_WS",   required=True)
    ETH_HTTP = env("ETH_HTTP", required=True)
    BASE_WS  = env("BASE_WS",  required=True)
    BASE_HTTP= env("BASE_HTTP",required=True)

    # Solana RPC (Helius)
    SOL_WSS  = env("HELIUS_WS",  required=True)   # e.g. wss://mainnet.helius-rpc.com/?api-key=XXXXX
    SOL_HTTP = env("HELIUS_HTTP", required=True)  # e.g. https://mainnet.helius-rpc.com/?api-key=XXXXX

    # Thresholds
    MIN_USD     = float(env("THRESHOLD_MIN_USD", "50000"))
    MIN_NATIVE  = float(env("THRESHOLD_MIN_NATIVE", "10"))

    # Whales lists (optional)
    WHALES_EVM = [w.lower() for w in csv_env("WHALES_EVM")]
    WHALES_SOL = csv_env("WHALES_SOL")

    # Auto-learn
    AUTO_ENABLED   = env("AUTOLEARN_ENABLED", "true").lower() in ("1","true","yes")
    AUTO_MIN_USD   = float(env("AUTOLEARN_MIN_USD", "250000"))
    AUTO_OCCURS    = int(env("AUTOLEARN_OCCURRENCES", "3"))
    AUTO_WINDOW_H  = int(env("AUTOLEARN_WINDOW_HOURS", "24"))
    AUTO_MAX_DAILY = int(env("AUTOLEARN_MAX_NEW_PER_DAY", "5"))
    AUTO_STATE     = env("AUTOLEARN_STATE_FILE", "/data/autolearn_state.json")
    AUTO_PERSIST   = env("AUTOLEARN_PERSIST_TO_CONFIG", "true").lower() in ("1","true","yes")

    cfg = {
        "chains": {
            "ethereum": {
                "ws": ETH_WS,
                "http": ETH_HTTP,
                "explorer": "https://etherscan.io/tx/",
                "native_symbol": "ETH",
                "native_coingecko": "ethereum",
                "routers": [
                    {"name":"UniswapV2", "address":"0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D"},
                    {"name":"UniswapV3_SwapRouter02", "address":"0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45"}
                ],
            },
            "base": {
                "ws": BASE_WS,
                "http": BASE_HTTP,
                "explorer": "https://basescan.org/tx/",
                "native_symbol": "ETH",
                "native_coingecko": "ethereum",
                "routers": [
                    {"name":"UniswapV3_Base", "address":"0x2626664c2603336E57B271c5C0b26F421741e481"}
                ],
            },
        },
        "solana": {
            "http": SOL_HTTP,
            "wss": SOL_WSS,
            "explorer_tx": "https://solscan.io/tx/",
            "program_ids": DEFAULT_SOL_PROGRAMS,
        },
        "whales_evm": WHALES_EVM,
        "whales_solana": WHALES_SOL,
        "thresholds": {"min_usd": MIN_USD, "min_native": MIN_NATIVE},
        "autolearn": {
            "enabled": AUTO_ENABLED,
            "min_usd": AUTO_MIN_USD,
            "occurrences": AUTO_OCCURS,
            "window_hours": AUTO_WINDOW_H,
            "max_new_per_day": AUTO_MAX_DAILY,
            "state_file": AUTO_STATE,
            "persist_to_config": AUTO_PERSIST,
        },
    }

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, sort_keys=False)
    print(f"[generate_config] wrote {args.out}")

if __name__ == "__main__":
    main()
