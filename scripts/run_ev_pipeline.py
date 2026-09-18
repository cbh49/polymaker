#!/usr/bin/env python3
"""Fetch Polymarket + Kalshi + ProphetX + SX Bet, then write output/ev/.

Venue adapters are stubs until API docs land. Same image as the rest of
trading-bot — on EC2:

  docker compose -f infra/docker-compose.yml run --rm ev

Locally:

  uv run python scripts/run_ev_pipeline.py
"""

from __future__ import annotations

import argparse

from ev_trading.config import EvConfig
from ev_trading.pipeline import run_sync


def main() -> None:
    parser = argparse.ArgumentParser(description="Cross-venue EV odds snapshot")
    parser.add_argument("--config-dir", default="config")
    parser.add_argument("--query", default=None)
    args = parser.parse_args()
    cfg = EvConfig.load(args.config_dir)
    stats = run_sync(cfg, query=args.query)
    print(f"quotes={stats['quotes']} matches={stats['matches']} → {cfg.out_dir}")


if __name__ == "__main__":
    main()
