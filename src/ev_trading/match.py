"""Turn free-text questions into a join key across venues."""

from __future__ import annotations

import re
import unicodedata

_PUNCT = re.compile(r"[^a-z0-9\s]+")
_SPACE = re.compile(r"\s+")
_YES_NO = re.compile(r"^(yes|no|y|n)$")


def normalize_text(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = text.encode("ascii", "ignore").decode("ascii")
    text = _PUNCT.sub(" ", text.lower())
    return _SPACE.sub(" ", text).strip()


def normalize_outcome(value: str) -> str:
    key = normalize_text(value)
    if _YES_NO.match(key):
        return "yes" if key.startswith("y") else "no"
    return key


def match_key(question: str, outcome: str) -> str:
    """Stable join key: normalized question + outcome."""
    return f"{normalize_text(question)}|{normalize_outcome(outcome)}"
