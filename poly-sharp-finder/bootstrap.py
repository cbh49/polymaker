"""Ensure trading-bot root + src/ are on sys.path for polymaker imports."""

from __future__ import annotations

import sys
from pathlib import Path

_FINDER_DIR = Path(__file__).resolve().parent
_TRADING_BOT = _FINDER_DIR.parent
_SRC = _TRADING_BOT / "src"


def ensure_paths() -> Path:
    """Insert trading-bot and src onto sys.path; return trading-bot root."""
    root = str(_TRADING_BOT)
    src = str(_SRC)
    if src not in sys.path:
        sys.path.insert(0, src)
    if root not in sys.path:
        sys.path.insert(0, root)
    return _TRADING_BOT
