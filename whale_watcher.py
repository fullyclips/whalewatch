# whale_watcher.py
# Discord alerts for whale flow across EVM (pending mempool) + Solana (logs).
# Uses websockets for EVM subscriptions (works with Web3 v6+), HTTP RPC for tx lookups.
# Includes: ABI decode shim, auto-learn (EVM), dotenv loader, robust reconnect loops, verbose boot logs.

import argparse
import asyncio
import json
import time
import yaml
import os
import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Any, Optional, List

import requests
import websockets

from dotenv import load_dotenv
from web3 import Web3

# -------------------- Load .env next to this script --------------------
load_dotenv(dotenv_path=Path(__file__).with_name(".env"))
WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL")

print("[boot] file:", __file__)
print("[boot] webhook loaded?", bool(WEBHOOK))

# -------------------- Discord helper --------------------
def discord_send(content: str = "", embeds: Optional[List[dict]] = None):
    """Send a Discord message or embeds via webhook."""
    if not WEBHOOK:
        print("[WARN] DISCORD_WEBHOOK_URL not set. Skipping Discord send.")
        return
    payload = {"content": content[:1900]}
    if embeds:
        payload["embeds"] = embeds[:10]  # Discord limit
    try:
        r = requests.post(WEBHOOK, json=payload, timeout=10)
        if r.status_code >= 300:
            print("[ERR] Discord webhook:", r.status_code, r.text[:300])
    except Exception as e:
        print("[ERR] Discord send failed:", e)

# -------------------- ABI decode shim (eth-abi v2/v3/v4/v5) --------------------
try:
    # eth-abi >= 4
    from eth_abi import decode as _abi_decode
except Exception:  # pragma: no cover
    # eth-abi <= 3
    from eth_abi import decode_abi as _abi_decode

# -------------------- Minimal selectors for Uniswap-style routers --------------------
from web3 import Web3 as _W3
SIG_V2_SWAP_EXACT_ETH_FOR_TOKENS = _W3.keccak(
    text="swapExactETHForTokens(uint256,address[],address,uint256)"
)[:4].hex()
SIG_V2_SWAP_EXACT_TOKENS_FOR_TOKENS = _W3.keccak(
    text="swapExactTokensForTokens(uint256,uint256,address[],address,uint256)"
)[:4].hex()
SIG_V3_EXACT_INPUT_SINGLE = _W3.keccak(
    text="exactInputSingle((address,address,uint24,address,uint256,uint256,uint256,uint160))"
)[:4].hex()

def parse_uniswap_call(input_data: bytes) -> Optional[Dict[str, Any]]:
    """Decode a few common swap methods to identify token_in/token_out."""
    if len(input_data) < 4:
        return None
    sig = input_data[:4].hex()
    data = input_data[4:]

    if sig == SIG_V2_SWAP_EXACT_ETH_FOR_TOKENS:
        try:
            types = ["uint256", "address[]", "address", "uint256"]
            vals = _abi_decode(types, bytes.fromhex(data.hex()))
            path = [Web3.to_checksum_address(p.hex()) for p in vals[1]]
            return {"dex": "V2", "method": "swapExactETHForTokens", "token_in": path[0], "token_out": path[-1]}
        except Exception:
            return None

    if sig == SIG_V2_SWAP_EXACT_TOKENS_FOR_TOKENS:
        try:
            types = ["uint256", "uint256", "address[]", "address", "uint256"]
            vals = _abi_decode(types, bytes.fromhex(data.hex()))
            path = [Web3.to_checksum_address(p.hex()) for p in vals[2]]
            return {"dex": "V2", "method": "swapExactTokensForTokens", "token_in": path[0], "token_out": path[-1]}
        except Exception:
            return None

    if sig == SIG_V3_EXACT_INPUT_SINGLE:
        try:
            types = ["address","address","uint24","address","uint256","uint256","uint256","uint160"]
            vals = _abi_decode(types, bytes.fromhex(data.hex()))
            token_in  = Web3.to_checksum_address(vals[0].hex())
            token_out = Web3.to_checksum_address(vals[1].hex())
            return {"dex": "V3", "method": "exactInputSingle", "token_in": token_in, "token_out": token_out}
        except Exception:
            return None

    return None

# -------------------- Simple native-coin USD pricing via CoinGecko --------------------
_price_cache: Dict[str, Dict[str, float]] = {}

def get_native_usd(coingecko_id: str, cache_seconds: int = 60) -> Optional[float]:
    if not coingecko_id:
        return None
    now = time.time()
    if coingecko_id in _price_cache and now - _price_cache[coingecko_id]["t"] < cache_seconds:
        return _price_cache[coingecko_id]["p"]
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": coingecko_id, "vs_currencies": "usd"},
            timeout=10,
        )
        usd = r.json()[coingecko_id]["usd"]
        _price_cache[coingecko_id] = {"p": usd, "t": now}
        return usd
    except Exception:
        return None

# -------------------- Config dataclasses --------------------
@dataclass
class EvmChainCfg:
    name: str
    ws: str              # WebSocket RPC (for eth_subscribe)
    http: Optional[str]  # HTTP RPC (for eth_getTransactionByHash) - strongly recommended
    explorer: str
    native_symbol: str
    native_coingecko: str
    routers: Dict[str, str]
    min_usd: float
    min_native: float
    whales: set

@dataclass
class SolanaCfg:
    http: str
    wss: str
    explorer_tx: str
    program_ids: List[str]
    whales: List[str]

# -------------------- Auto-learn (EVM) --------------------
class AutoLearn:
    """Promote repeat large EVM buyers to whales_evm and persist to config.yaml."""
    def __init__(self, cfg: dict, config_path: str, whales_set: set):
        a = cfg.get("autolearn", {})
        self.enabled = bool(a.get("enabled", False))
        self.min_usd = float(a.get("min_usd", 250000))
        self.occurrences = int(a.get("occurrences", 3))
        self.window_hours = int(a.get("window_hours", 24))
        self.max_new_per_day = int(a.get("max_new_per_day", 5))
        self.state_file = a.get("state_file", "autolearn_state.json")
        self.persist_to_config = bool(a.get("persist_to_config", True))
        self.config_path = config_path
        self.whales_set = whales_set
        self.state = {"counters": {}, "day": self._today(), "added_today": 0}
        try:
            if os.path.exists(self.state_file):
                import json as _json
                with open(self.state_file, "r", encoding="utf-8") as f:
                    self.state = _json.load(f)
        except Exception:
            pass

    def _today(self) -> str:
        return datetime.date.today().isoformat()

    def _save_state(self):
        try:
            import json as _json
            with open(self.state_file, "w", encoding="utf-8") as f:
                _json.dump(self.state, f)
        except Exception as e:
            print("[autolearn] save state error:", e)

    def _reset_day_if_needed(self):
        today = self._today()
        if self.state.get("day") != today:
            self.state["day"] = today
            self.state["added_today"] = 0

    def consider(self, address: str, est_usd: Optional[float]) -> Optional[str]:
        """Return address if newly learned; else None."""
        if not self.enabled:
            return None
        addr = (address or "").lower()
        if addr in self.whales_set:
            return None
        if (est_usd or 0) < self.min_usd:
            return None

        now = time.time()
        window = self.window_hours * 3600
        c = self.state.setdefault("counters", {}).setdefault(addr, [])
        # prune & append
        c = [t for t in c if now - t <= window]
        c.append(now)
        self.state["counters"][addr] = c
        self._reset_day_if_needed()

        if len(c) >= self.occurrences and self.state["added_today"] < self.max_new_per_day:
            # promote
            self.whales_set.add(addr)
            self.state["added_today"] += 1
            if self.persist_to_config:
                try:
                    with open(self.config_path, "r", encoding="utf-8") as f:
                        cfg_full = yaml.safe_load(f) or {}
                    lst = cfg_full.get("whales_evm", [])
                    if addr not in [a.lower() for a in lst]:
                        lst.append(address)  # preserve original case
                        cfg_full["whales_evm"] = lst
                        with open(self.config_path, "w", encoding="utf-8") as f:
                            yaml.safe_dump(cfg_full, f, sort_keys=False)
                except Exception as e:
                    print("[autolearn] persist-to-config error:", e)
            self._save_state()
            return address
        else:
            self._save_state()
            return None

# -------------------- EVM: WS subscribe helper --------------------
async def evm_ws_sub(ws_url: str):
    """
    Async generator yielding pending tx hashes from eth_subscribe 'newPendingTransactions'.
    Reconnects on failures.
    """
    backoff = 2
    while True:
        try:
            print("[evm_sub] connecting:", ws_url)
            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=20) as ws:
                sub_req = {"jsonrpc":"2.0","id":1,"method":"eth_subscribe","params":["newPendingTransactions"]}
                await ws.send(json.dumps(sub_req))
                print("[evm_sub] subscribed to newPendingTransactions")
                while True:
                    raw = await ws.recv()
                    msg = json.loads(raw)
                    if msg.get("method") == "eth_subscription":
                        params = msg.get("params", {})
                        txh = params.get("result")
                        if isinstance(txh, str) and txh.startswith("0x") and len(txh) == 66:
                            yield txh
        except Exception as e:
            print("[evm_sub] error, will reconnect:", e)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

# -------------------- EVM watcher --------------------
async def watch_evm(cfg: EvmChainCfg, price_cache_id: str, autolearn: Optional[AutoLearn]):
    print(f"[{cfg.name}] starting watcher…")
    # HTTP provider for tx details
    w3_http = None
    if cfg.http:
        try:
            w3_http = Web3(Web3.HTTPProvider(cfg.http, request_kwargs={"timeout": 15}))
            ok = w3_http.is_connected()
            print(f"[{cfg.name}] HTTP connected? ->", ok)
        except Exception as e:
            print(f"[{cfg.name}][WARN] HTTP provider init failed:", e)

    native_usd = get_native_usd(price_cache_id) or 0.0
    routers_lc = {addr.lower(): name for name, addr in cfg.routers.items()}
    whales_lc = set(a.lower() for a in cfg.whales)

    async for tx_hash in evm_ws_sub(cfg.ws):
        try:
            # fetch tx via HTTP RPC if available
            tx = None
            if w3_http:
                try:
                    tx = w3_http.eth.get_transaction(tx_hash)
                except Exception:
                    tx = None
            if not tx:
                # fallback: fetch via JSON-RPC over HTTP using requests (if cfg.http provided)
                if cfg.http:
                    try:
                        r = requests.post(cfg.http, json={"jsonrpc":"2.0","id":1,"method":"eth_getTransactionByHash","params":[tx_hash]}, timeout=10)
                        tx = r.json().get("result")
                    except Exception:
                        tx = None
                # else: no HTTP → skip (we avoid mixing requests on the ws subscription connection)
            if not tx:
                continue

            # normalize fields whether tx is dict from Web3 or raw JSON
            to_addr = (tx.get("to") or "").lower()
            frm     = (tx.get("from") or "").lower()
            is_router = to_addr in routers_lc

            # value
            try:
                # if from Web3: int; if raw JSON: hex
                v = tx.get("value", 0)
                if isinstance(v, str):
                    v = int(v, 16)
                value_native = float(Web3.from_wei(v, "ether"))
            except Exception:
                value_native = 0.0
            est_usd = value_native * native_usd if native_usd else None

            # input data for swap parsing
            inp = tx.get("input") or ""
            swap_info = None
            if is_router and isinstance(inp, str) and inp.startswith("0x") and len(inp) >= 10:
                try:
                    swap_info = parse_uniswap_call(bytes.fromhex(inp[2:]))
                except Exception:
                    swap_info = None

            is_whale = frm in whales_lc
            big_native = value_native >= cfg.min_native
            big_usd = (est_usd or 0) >= cfg.min_usd

            if is_router and (is_whale or big_native or big_usd or swap_info):
                link = f"{cfg.explorer}{tx_hash}"
                usd_str = f" (~${est_usd:,.0f})" if est_usd else ""
                desc = (
                    f"**From:** `{frm}`\n"
                    f"**To:** {routers_lc.get(to_addr,'Router')} (`{to_addr}`)\n"
                    f"**Value:** {value_native:.4f} {cfg.native_symbol}{usd_str}\n"
                )
                if swap_info:
                    desc += f"**Method:** {swap_info['method']} • **TokenOut:** `{swap_info['token_out']}`\n"

                embed = {
                    "title": f"{cfg.name.upper()} • Possible Whale Buy",
                    "description": desc,
                    "color": 0x2ECC71,
                    "url": link,
                    "footer": {"text": tx_hash},
                }
                discord_send(embeds=[embed])
                print(f"[{cfg.name}] alert sent:", tx_hash)

                if autolearn:
                    learned = autolearn.consider(frm, est_usd)
                    if learned:
                        discord_send(
                            f"🧠 **Auto-learned new EVM whale:** `{learned}` "
                            f"(≥{autolearn.min_usd:,.0f} USD x{autolearn.occurrences} in {autolearn.window_hours}h). "
                            f"Added to `whales_evm`."
                        )
        except Exception as e:
            print(f"[{cfg.name}] loop error:", e)

# -------------------- Solana logs watcher --------------------
async def solana_logs_sub(ws_url: str, filter_obj: dict):
    """Async generator yielding logs notifications for a given 'mentions' filter with reconnect."""
    backoff = 2
    while True:
        try:
            print("[sol_sub] connecting:", ws_url, "| filter:", filter_obj)
            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=20) as ws:
                await ws.send(json.dumps({"jsonrpc":"2.0","id":1,"method":"logsSubscribe","params":[filter_obj, {"commitment":"confirmed"}]}))
                print("[sol_sub] subscribed")
                while True:
                    raw = await ws.recv()
                    msg = json.loads(raw)
                    if msg.get("method") == "logsNotification":
                        yield msg["params"]["result"]
        except Exception as e:
            print("[sol_sub] error, will reconnect:", e)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

async def watch_solana(cfg):
    print("[solana] starting logs watcher…")
    filters = [{"mentions": pk} for pk in (cfg.program_ids + cfg.whales)]
    async def handle_filter(filter_obj):
        async for res in solana_logs_sub(cfg.wss, filter_obj):
            try:
                sig = res.get("signature")
                if not sig:
                    continue
                log_lines = res.get("logs") or []
                first_line = (log_lines[0] if log_lines else "")[:180]
                link = f"{cfg.explorer_tx}{sig}"
                desc = f"**Signature:** `{sig}`\n**First log:** `{first_line}`\n"
                embed = {
                    "title": "SOLANA • Whale/DEX Mention",
                    "description": desc,
                    "color": 0x5865F2,
                    "url": link,
                    "footer": {"text": "logsSubscribe mention"},
                }
                discord_send(embeds=[embed])
                print("[solana] alert:", sig)
            except Exception as e:
                print("[solana] handle_filter error:", e)
    await asyncio.gather(*(handle_filter(f) for f in filters))

# -------------------- Config loaders --------------------
def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}

def build_evm_cfgs(c: dict) -> List[EvmChainCfg]:
    out: List[EvmChainCfg] = []
    thresholds = c.get("thresholds", {})
    whales = set(a.lower() for a in c.get("whales_evm", []))
    chains = c.get("chains", {}) or {}
    for name, cc in chains.items():
        routers = {r["name"]: Web3.to_checksum_address(r["address"]) for r in cc.get("routers", [])}
        out.append(
            EvmChainCfg(
                name=name,
                ws=cc["ws"],
                http=cc.get("http"),  # new optional HTTP field
                explorer=cc["explorer"],
                native_symbol=cc.get("native_symbol", "ETH"),
                native_coingecko=cc.get("native_coingecko", "ethereum"),
                routers=routers,
                min_usd=float(thresholds.get("min_usd", 50000)),
                min_native=float(thresholds.get("min_native", 10)),
                whales=whales,
            )
        )
    return out

@dataclass
class _SolCfg(SolanaCfg):
    pass

def build_solana_cfg(c: dict) -> Optional[_SolCfg]:
    sc = c.get("solana")
    if not sc:
        return None
    return _SolCfg(
        http=sc["http"],
        wss=sc["wss"],
        explorer_tx=sc.get("explorer_tx", "https://solscan.io/tx/"),
        program_ids=sc.get("program_ids", []),
        whales=c.get("whales_solana", []),
    )

# -------------------- Main --------------------
async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True, help="Path to config.yaml")
    args = ap.parse_args()

    cfg_path = args.config
    print("[boot] loading config from:", cfg_path)

    try:
        cfg = load_config(cfg_path)
    except Exception as e:
        print("[boot][ERROR] failed to read config:", e)
        import traceback as _tb
        _tb.print_exc()
        return

    print("[boot] chains:", list((cfg.get("chains") or {}).keys()), " solana:", bool(cfg.get("solana")))

    evm_cfgs = build_evm_cfgs(cfg)
    sol_cfg = build_solana_cfg(cfg)

    if not evm_cfgs and not sol_cfg:
        print("[boot][ERROR] No chains configured. Exiting.")
        return

    # Quick WS connectivity prints (best-effort)
    for ch in evm_cfgs:
        print(f"[boot] {ch.name} ws:", ch.ws)
        if ch.http:
            try:
                ok = Web3(Web3.HTTPProvider(ch.http, request_kwargs={"timeout": 10})).is_connected()
                print(f"[boot] {ch.name} HTTP connected? ->", ok)
            except Exception as e:
                print(f"[boot] {ch.name} HTTP check failed:", e)

    if sol_cfg:
        print("[boot] solana wss:", sol_cfg.wss)

    autolearn = AutoLearn(cfg, cfg_path, set(cfg.get("whales_evm", []))) if "autolearn" in cfg else None

    tasks: List[asyncio.Task] = []
    for ch in evm_cfgs:
        tasks.append(asyncio.create_task(watch_evm(ch, ch.native_coingecko, autolearn)))
    if sol_cfg:
        tasks.append(asyncio.create_task(watch_solana(sol_cfg)))

    print("[boot] starting tasks…")
    try:
        await asyncio.gather(*tasks)
    except Exception as e:
        print("[boot][ERROR] task crashed:", e)
        import traceback as _tb
        _tb.print_exc()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Exiting…")
