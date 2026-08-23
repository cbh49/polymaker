"""
Smoke-check live Polymarket Data API + CLOB websocket schemas.

Usage (from trading-bot/):
    uv run python poly-sharp-finder/probe_feeds.py --watchlist poly-sharp-finder/watch_list.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import aiohttp
import websockets

from bootstrap import ensure_paths

ensure_paths()

# Allow running as script with sibling imports
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import ENDPOINTS  # noqa: E402
from registry import load_watch_list  # noqa: E402


async def probe_trades(condition_id: str) -> dict:
    params = {"market": condition_id, "limit": 5}
    async with aiohttp.ClientSession() as session:
        async with session.get(ENDPOINTS.data_api_trades, params=params) as resp:
            status = resp.status
            body = await resp.json(content_type=None)
    print(f"\n=== Data API /trades status={status} market={condition_id[:18]}… ===")
    if isinstance(body, list) and body:
        sample = body[0]
        print("keys:", sorted(sample.keys()))
        print("sample:", json.dumps(sample, indent=2, default=str)[:2000])
    else:
        print("body:", json.dumps(body, indent=2, default=str)[:1500])
    return {"status": status, "body": body}


async def probe_ws(token_id: str, timeout_sec: float = 15.0) -> dict | None:
    print(f"\n=== CLOB WS subscribe token={token_id[:18]}… (wait {timeout_sec}s) ===")
    try:
        async with websockets.connect(ENDPOINTS.clob_ws, ping_interval=10) as ws:
            await ws.send(json.dumps({"type": "market", "assets_ids": [token_id]}))
            deadline = asyncio.get_event_loop().time() + timeout_sec
            while asyncio.get_event_loop().time() < deadline:
                remaining = deadline - asyncio.get_event_loop().time()
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, remaining))
                except asyncio.TimeoutError:
                    break
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    print("non-json:", raw[:200])
                    continue
                if isinstance(msg, list):
                    print(f"list message len={len(msg)}; first keys:",
                          sorted(msg[0].keys()) if msg and isinstance(msg[0], dict) else type(msg[0]))
                    if msg and isinstance(msg[0], dict):
                        print(json.dumps(msg[0], indent=2, default=str)[:2000])
                        return msg[0]
                    continue
                if isinstance(msg, dict):
                    print("keys:", sorted(msg.keys()))
                    print("event_type:", msg.get("event_type"))
                    print(json.dumps(msg, indent=2, default=str)[:2000])
                    return msg
    except Exception as exc:  # noqa: BLE001
        print(f"WS probe failed: {exc}")
        return None
    print("No WS messages received within timeout.")
    return None


async def main_async(watchlist: str) -> int:
    markets = load_watch_list(watchlist)
    if not markets:
        print("Empty watch list — export one first.", file=sys.stderr)
        return 1
    m = markets[0]
    print(f"Probing with: {m.league} {m.label} ({m.condition_id[:18]}…)")
    await probe_trades(m.condition_id)
    await probe_ws(m.yes_token_id)
    print("\nProbe done. Adjust trade_poller._parse_trade / ws_client._handle_book if keys differ.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--watchlist",
        default="poly-sharp-finder/watch_list.json",
        help="path to watch_list.json",
    )
    args = parser.parse_args()
    path = Path(args.watchlist)
    if not path.is_file():
        print(f"watch list not found: {path}", file=sys.stderr)
        raise SystemExit(1)
    raise SystemExit(asyncio.run(main_async(str(path))))


if __name__ == "__main__":
    main()
