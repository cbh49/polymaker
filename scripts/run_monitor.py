#!/usr/bin/env python3
"""Always-on poly-sharp-finder wrapper. Adds --live when POLYMAKER_LIVE=1."""

from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_MAIN = _ROOT / "poly-sharp-finder" / "main.py"


def _live() -> bool:
    return os.environ.get("POLYMAKER_LIVE", "").strip().lower() in {"1", "true", "yes"}


def main() -> None:
    args = [
        sys.executable,
        str(_MAIN),
        "--watchlist",
        str(_ROOT / "poly-sharp-finder" / "watch_list.json"),
        "--config-dir",
        str(_ROOT / "config"),
        *sys.argv[1:],
    ]
    if _live() and "--live" not in args and "--no-trade" not in args:
        args.append("--live")
    os.chdir(_ROOT)
    os.execv(sys.executable, args)


if __name__ == "__main__":
    main()
