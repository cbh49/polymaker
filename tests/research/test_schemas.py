"""Tests for loading llm_best_plays.json schemas."""

from __future__ import annotations

import json
from pathlib import Path

from polymaker.research.load_plays import iter_plays, load_best_plays


def test_load_best_plays_from_fixture(tmp_path: Path) -> None:
    path = tmp_path / "llm_best_plays.json"
    path.write_text(
        json.dumps(
            {
                "ml_best_plays": [
                    {
                        "matchup": "Detroit Tigers @ San Francisco Giants",
                        "time": "7:15PM",
                        "pick": "Detroit Tigers ML",
                        "bet_type": "MONEYLINE",
                    }
                ],
                "ou_best_plays": [
                    {
                        "matchup": "Baltimore Orioles @ Texas Rangers",
                        "time": "7:15PM",
                        "pick": "UNDER 7.5",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    plays = load_best_plays(path)
    assert len(plays.ml_best_plays) == 1
    assert plays.ml_best_plays[0].bet_type == "MONEYLINE"
    assert plays.ml_best_plays[0].category == "ml"
    assert plays.ou_best_plays[0].bet_type == "TOTAL"
    assert plays.ou_best_plays[0].category == "ou"
    assert len(iter_plays(plays)) == 2


def test_load_repo_llm_best_plays_if_present() -> None:
    repo = Path(__file__).resolve().parents[3]
    path = repo / "MLB" / "static-json" / "llm_best_plays.json"
    if not path.is_file():
        return
    plays = load_best_plays(path)
    assert plays.ml_best_plays or plays.ou_best_plays
