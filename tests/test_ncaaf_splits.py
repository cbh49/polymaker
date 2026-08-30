"""NCAAF / CFB team matching, sharp-money sources, and split parsers."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

_AGG = Path(__file__).resolve().parents[1] / "data-aggregation"
if str(_AGG) not in sys.path:
    sys.path.insert(0, str(_AGG))

from cfb_team_map import (  # noqa: E402
    canonical_abbr,
    canonical_name,
    match_matchup,
    names_match,
    poly_code,
)
from find_sharp_money import (  # noqa: E402
    _markets_from_arg,
    config_snapshot,
    primary_source_label,
    process_game,
    sources_for_league,
)
from polymaker.catalog.sports import is_moneyline_slug, look_ahead_days_for_series
from polymaker.trading.teams import resolve_team
from scrape_dk_splits import PAGE_URLS as DK_URLS
from scrape_dk_splits import _parse_when_date, parse_games
from scrape_sbd_splits import has_usable_splits, parse_event
from scrape_thespread_splits import PAGE_URLS as SPREAD_URLS


def test_cfb_hawaii_aliases() -> None:
    assert canonical_abbr("HAW") == "HAW"
    assert canonical_name("Hawai'i") == "Hawaii"
    assert canonical_name("Hawaii Rainbow Warriors") == "Hawaii"
    assert names_match("Hawai'i", "HAW")
    assert poly_code("HAW") == "hawaii"


def test_cfb_san_jose_state() -> None:
    assert canonical_name("San Jose ST Spartans") == "San Jose State"
    assert canonical_abbr("San Jose State") == "SJSU"
    assert names_match("San Jose State", "SJSU")


def test_cfb_nc_state() -> None:
    assert canonical_abbr("NCST") == "NCST"
    assert canonical_abbr("NC State") == "NCST"
    assert canonical_name("North Carolina State") == "NC State"
    assert names_match("NCST", "NC State")


def test_cfb_stanford_cardinal_mascot() -> None:
    """VSiN labels Stanford as 'Stanford Cardinal'; that must still map to STAN."""
    assert canonical_name("Stanford Cardinal") == "Stanford"
    assert canonical_abbr("Stanford Cardinal") == "STAN"
    assert names_match("Stanford", "Stanford Cardinal")
    assert names_match("HAW", "Hawaii")


def test_cfb_unknown_fcs_abbr_stays_uppercase() -> None:
    assert canonical_abbr("STON") == "STON"
    assert canonical_abbr("DSU") == "DSU"


def test_ncaaf_sharp_sources() -> None:
    assert sources_for_league("NCAAF") == ("primary", "vsin", "sbd")
    assert sources_for_league("CFB") == ("primary", "vsin", "sbd")
    assert primary_source_label("NCAAF") == "draftkings"
    assert primary_source_label("CFB") == "draftkings"
    assert _markets_from_arg("all") == ("moneyline", "spread", "total")
    cfg = config_snapshot(("moneyline", "spread", "total"), sources_for_league("NCAAF"), "NCAAF")
    assert cfg["strong_source_gap_threshold"] == 15.0
    assert cfg["low_prob_dog_odds_threshold"] == 200.0
    assert cfg["rlm_source_priority"] == ["eva", "thespread", "polymarket"]


def _side(*, public: int, handle: int, vsin_pub: int, vsin_h: int, sbd_pub: int, sbd_h: int, **extra):
    row = {
        "public_bet_pct": public,
        "handle_bet_pct": handle,
        "vsin_public_bet_pct": vsin_pub,
        "vsin_handle_bet_pct": vsin_h,
        "sbd_public_bet_pct": sbd_pub,
        "sbd_handle_bet_pct": sbd_h,
    }
    row.update(extra)
    return row


def test_ncaaf_process_game_moneyline() -> None:
    game = {
        "matchup": "UNC @ TCU",
        "away": "North Carolina",
        "home": "TCU",
        "date": "2026-08-29",
        "moneyline": {
            "away": _side(
                public=18,
                handle=38,
                vsin_pub=20,
                vsin_h=40,
                sbd_pub=19,
                sbd_h=41,
                selection="UNC",
                open=280,
                live=250,
            ),
            "home": _side(
                public=82,
                handle=62,
                vsin_pub=80,
                vsin_h=60,
                sbd_pub=81,
                sbd_h=59,
                selection="TCU",
                open=-355,
                live=-320,
            ),
        },
    }
    play = process_game(game, market="moneyline", sources=sources_for_league("NCAAF"))
    assert play is not None
    assert play["side"] == "UNC"
    assert play["home_away"] == "away"
    assert play["rlm_confirmed"] is True
    assert play["tier"] in {"A", "A+"}
    assert play["rlm_source_used"] == "thespread"
    assert play["sbd_override"] is False


def test_ncaaf_process_game_total() -> None:
    game = {
        "matchup": "MEM @ UNLV",
        "date": "2026-08-29",
        "total": {
            "over": _side(
                public=84,
                handle=19,
                vsin_pub=81,
                vsin_h=20,
                sbd_pub=80,
                sbd_h=18,
                selection="Over",
                open=57.5,
                live=56.5,
                live_odds=-108,
            ),
            "under": _side(
                public=16,
                handle=81,
                vsin_pub=19,
                vsin_h=80,
                sbd_pub=20,
                sbd_h=82,
                selection="Under",
                open=57.5,
                live=56.5,
                live_odds=-112,
            ),
        },
    }
    play = process_game(game, market="total", sources=sources_for_league("NCAAF"))
    assert play is not None
    assert play["home_away"] == "under"
    assert play["side"] == "Under"
    assert play["public_favors"] == "over"
    assert play["line_moved_toward"] == "under"
    assert play["rlm_confirmed"] is True
    assert play["tier"] in {"A", "A+", "B"}


def test_ncaaf_total_rlm_falls_back_to_eva() -> None:
    game = {
        "matchup": "HAW @ STAN",
        "date": "2026-08-29",
        "total": {
            "over": _side(
                public=34,
                handle=80,
                vsin_pub=37,
                vsin_h=78,
                sbd_pub=40,
                sbd_h=82,
                selection="Over",
                eva_open=48.5,
                eva_line=49.5,
                live_odds=-112,
            ),
            "under": _side(
                public=66,
                handle=20,
                vsin_pub=63,
                vsin_h=22,
                sbd_pub=60,
                sbd_h=18,
                selection="Under",
                eva_open=48.5,
                eva_line=49.5,
                live_odds=-108,
            ),
        },
    }
    play = process_game(game, market="total", sources=sources_for_league("NCAAF"))
    assert play is not None
    assert play["home_away"] == "over"
    assert play["line_moved_toward"] == "over"
    assert play["public_favors"] == "under"
    assert play["rlm_confirmed"] is True
    assert play["open"] == 48.5
    assert play["live"] == 49.5
    assert play["rlm_source_used"] == "eva"


def test_sbd_ncaaf_abbreviation_and_market() -> None:
    event = {
        "scheduled": "2026-08-29T23:00:00Z",
        "competitors": {
            "away": {"abbreviation": "HAW", "market": "Hawaii", "name": "Rainbow Warriors"},
            "home": {"abbreviation": "STAN", "market": "Stanford", "name": "Cardinal"},
        },
        "bettingSplits": {
            "moneyline": {
                "updated": "2026-08-27T12:00:00Z",
                "away": {"betsPercentage": 40, "stakePercentage": 56},
                "home": {"betsPercentage": 60, "stakePercentage": 44},
            },
            "spread": {
                "away": {"betsPercentage": 37, "stakePercentage": 45},
                "home": {"betsPercentage": 63, "stakePercentage": 55},
            },
            "total": {
                "over": {"betsPercentage": 66, "stakePercentage": 96},
                "under": {"betsPercentage": 34, "stakePercentage": 4},
            },
        },
        "markets": {"moneyline": {"books": []}, "spread": {"books": []}, "total": {"books": []}},
    }
    parsed = parse_event(event, {"HAW": "Hawaii", "STAN": "Stanford"}, [])
    assert parsed is not None
    assert parsed["away_abbr"] == "HAW"
    assert parsed["home_abbr"] == "STAN"
    assert parsed["total"]["over"]["sbd_public_bet_pct"] == 66
    assert parsed["total"]["over"]["sbd_handle_bet_pct"] == 96


def test_sbd_empty_string_splits_are_unusable() -> None:
    splits = {
        "moneyline": {
            "updated": "2026-08-27T12:00:00Z",
            "away": {"betsPercentage": "", "stakePercentage": ""},
            "home": {"betsPercentage": "", "stakePercentage": ""},
        }
    }
    assert has_usable_splits(splits) is False


def test_dk_and_thespread_ncaaf_urls() -> None:
    assert "NCAA+Football" in DK_URLS["NCAAF"]
    assert DK_URLS["NCAAF"].endswith("itm_content=NCAA+Football") or "tb_eg=NCAA+Football" in DK_URLS["NCAAF"]
    assert "ncaa-college-football-public-betting-chart" in SPREAD_URLS["NCAAF"]


def _dk_card(matchup: str, when: str, event_id: str = "1") -> str:
    return (
        '<div class="tb-se">'
        '<div class="tb-se-title">'
        f'<a href="/event/{event_id}">{matchup}</a>'
        f"<span>{when}</span>"
        "</div></div>"
    )


def test_dk_parse_when_date_finds_md_inside_prefix() -> None:
    assert _parse_when_date("Sat 8/30 7:00 PM", 2026) == date(2026, 8, 30)
    assert _parse_when_date("8/30 12:30 PM", 2026) == date(2026, 8, 30)
    assert _parse_when_date("8/29/2026 5:00 PM", 2026) == date(2026, 8, 29)
    assert _parse_when_date("7:00 PM", 2026) is None


def test_dk_ncaaf_keeps_card_date_not_request_day() -> None:
    html = _dk_card("Hawaii @ Stanford", "Sat 8/29 5:00 PM", "10") + _dk_card(
        "Alabama @ Florida State", "7:00 PM", "11"
    )
    games = parse_games(
        html,
        [],
        league="NCAAF",
        canonical_name_fn=canonical_name,
        canonical_abbr_fn=canonical_abbr,
        names_match_fn=names_match,
        match_matchup_fn=match_matchup,
        day=date(2026, 8, 27),
    )
    assert len(games) == 2
    haw = next(g for g in games if g["away_abbr"] == "HAW")
    assert haw["date"] == "2026-08-29"
    bama = next(g for g in games if g["away_abbr"] == "ALA")
    assert bama["date"] == "2026-08-27"


def test_dk_ncaaf_uses_cfb_match_matchup() -> None:
    html = _dk_card("Hawaii @ Stanford", "8/29 5:00 PM")
    matchups = [{"away": "Hawaii", "home": "Stanford", "espn_game_id": "401628001"}]
    games = parse_games(
        html,
        matchups,
        league="NCAAF",
        canonical_name_fn=canonical_name,
        canonical_abbr_fn=canonical_abbr,
        names_match_fn=names_match,
        match_matchup_fn=match_matchup,
        day=date(2026, 8, 27),
    )
    assert len(games) == 1
    assert games[0]["espn_game_id"] == "401628001"


def test_cfb_moneyline_slug() -> None:
    assert is_moneyline_slug("cfb-hawaii-stan-2026-08-29")
    assert not is_moneyline_slug("cfb-hawaii-stan-2026-08-29-total-49pt5")
    assert look_ahead_days_for_series("cfb-2026", 3) == 7


def test_resolve_team_ncaaf() -> None:
    haw = resolve_team("NCAAF", "HAW")
    assert haw is not None
    assert haw.poly_code == "hawaii"
    assert haw.full_name == "Hawaii"
    ncst = resolve_team("CFB", "NC State")
    assert ncst is not None
    assert ncst.betting_abbr == "NCST"


def test_ncaaf_process_game_spread() -> None:
    game = {
        "matchup": "HAW @ STAN",
        "date": "2026-08-29",
        "spread": {
            "away": _side(
                public=39,
                handle=77,
                vsin_pub=40,
                vsin_h=75,
                sbd_pub=38,
                sbd_h=70,
                selection="HAW",
                open=6.0,
                live=3.5,
                live_odds=-108,
            ),
            "home": _side(
                public=61,
                handle=23,
                vsin_pub=60,
                vsin_h=25,
                sbd_pub=62,
                sbd_h=30,
                selection="STAN",
                open=-6.0,
                live=-3.5,
                live_odds=-112,
            ),
        },
    }
    play = process_game(game, market="spread", sources=sources_for_league("NCAAF"))
    assert play is not None
    assert play["home_away"] == "away"
    assert play["side"] == "HAW"
    assert play["public_favors"] == "home"
    assert play["line_moved_toward"] == "away"
    assert play["rlm_confirmed"] is True
    assert play["market"] == "spread"
    assert play["rlm_source_used"] == "thespread"


def _poly(*, line_open: int, line_live: int, p_open: float, p_live: float, liquidity: float) -> dict:
    return {
        "line": line_live,
        "implied_prob_pct": p_live,
        "liquidity": liquidity,
        "volume_24hr": 50_000.0,
        "history": [
            {"ts": "2026-08-26T23:55:16-04:00", "line": line_open, "implied_prob_pct": p_open},
            {"ts": "2026-08-27T19:30:21-04:00", "line": line_live, "implied_prob_pct": p_live},
        ],
    }


def test_sbd_override_yields_tier_b_not_a() -> None:
    """VSiN+DK both ≥ 15 and agree; SBD votes the other way → keep as Tier B."""
    game = {
        "matchup": "UNC @ TCU",
        "date": "2026-08-29",
        "moneyline": {
            "away": _side(
                public=20,
                handle=45,
                vsin_pub=18,
                vsin_h=40,
                sbd_pub=80,
                sbd_h=30,
                selection="UNC",
                open=280,
                live=250,
            ),
            "home": _side(
                public=80,
                handle=55,
                vsin_pub=82,
                vsin_h=60,
                sbd_pub=20,
                sbd_h=70,
                selection="TCU",
                open=-355,
                live=-320,
            ),
        },
    }
    play = process_game(game, market="moneyline", sources=sources_for_league("NCAAF"))
    assert play is not None
    assert play["side"] == "UNC"
    assert play["tier"] == "B"
    assert play["sbd_override"] is True
    assert play["sbd_dissent_gap"] == 50  # home SBD 70 − 20
    assert play["agreeing_sources"] == ["primary", "vsin"]
    assert play["n_sources_agreeing"] == 2
    # SBD dropped from the sum: 1.0×25 + 1.5×22 = 58, not counted against the play
    assert play["composite_gap"] == 58.0
    assert "sbd" not in play["agreeing_sources"]


def test_sbd_dissent_without_strong_gaps_still_discards() -> None:
    """SBD veto still discards when VSiN's per-source gap is below the strong threshold."""
    game = {
        "matchup": "UNC @ TCU",
        "date": "2026-08-29",
        "moneyline": {
            "away": _side(
                public=17,
                handle=38,
                vsin_pub=14,
                vsin_h=22,
                sbd_pub=80,
                sbd_h=30,
                selection="UNC",
                open=280,
                live=250,
            ),
            "home": _side(
                public=83,
                handle=62,
                vsin_pub=86,
                vsin_h=78,
                sbd_pub=20,
                sbd_h=70,
                selection="TCU",
                open=-355,
                live=-320,
            ),
        },
    }
    # DK gap +21, VSiN gap +8 (< 15), SBD votes home
    assert process_game(game, market="moneyline", sources=sources_for_league("NCAAF")) is None


def test_sbd_missing_is_not_override() -> None:
    game = {
        "matchup": "UNC @ TCU",
        "date": "2026-08-29",
        "moneyline": {
            "away": {
                "selection": "UNC",
                "public_bet_pct": 20,
                "handle_bet_pct": 45,
                "vsin_public_bet_pct": 18,
                "vsin_handle_bet_pct": 40,
                "open": 280,
                "live": 250,
            },
            "home": {
                "selection": "TCU",
                "public_bet_pct": 80,
                "handle_bet_pct": 55,
                "vsin_public_bet_pct": 82,
                "vsin_handle_bet_pct": 60,
                "open": -355,
                "live": -320,
            },
        },
    }
    play = process_game(game, market="moneyline", sources=sources_for_league("NCAAF"))
    assert play is not None
    assert play["tier"] == "B"
    assert play["sbd_override"] is False
    assert play["n_sources_agreeing"] == 2


def test_ml_low_volume_dog_flag_and_spread_divergence() -> None:
    """+270 ML dog with a strong ML gap but a flat spread composite on the same side."""
    game = {
        "matchup": "UNC @ TCU",
        "date": "2026-08-29",
        "moneyline": {
            "away": _side(
                public=17,
                handle=38,
                vsin_pub=14,
                vsin_h=22,
                sbd_pub=6,
                sbd_h=23,
                selection="UNC",
                live=270,
                open=280,
            ),
            "home": _side(
                public=83,
                handle=62,
                vsin_pub=86,
                vsin_h=78,
                sbd_pub=94,
                sbd_h=77,
                selection="TCU",
                live=-340,
                open=-355,
            ),
        },
        "spread": {
            "away": _side(
                public=48,
                handle=50,
                vsin_pub=49,
                vsin_h=51,
                sbd_pub=50,
                sbd_h=51,
                selection="UNC",
                open=7.5,
                live=7.5,
            ),
            "home": _side(
                public=52,
                handle=50,
                vsin_pub=51,
                vsin_h=49,
                sbd_pub=50,
                sbd_h=49,
                selection="TCU",
                open=-7.5,
                live=-7.5,
            ),
        },
    }
    play = process_game(game, market="moneyline", sources=sources_for_league("NCAAF"))
    assert play is not None
    assert play["side"] == "UNC"
    assert play["low_volume_dog_flag"] is True
    assert play["ml_spread_divergence"] is True
    assert play["spread_composite_gap"] == 5.75  # 1.0×2 + 1.5×2 + 0.75×1
    assert play["confidence_note"] is not None
    assert "+200" in play["confidence_note"]
    assert "45.75" in play["confidence_note"]
    assert "5.75" in play["confidence_note"]


def test_rlm_prefers_eva_over_thespread_when_eva_moved() -> None:
    """EVA chart moved: use it even if TheSpread also has an open→live pair."""
    game = {
        "matchup": "HAW @ STAN",
        "date": "2026-08-29",
        "spread": {
            "away": _side(
                public=39,
                handle=77,
                vsin_pub=40,
                vsin_h=75,
                sbd_pub=38,
                sbd_h=70,
                selection="HAW",
                open=6.0,
                live=6.0,
                eva_open=6.0,
                eva_line=3.5,
            ),
            "home": _side(
                public=61,
                handle=23,
                vsin_pub=60,
                vsin_h=25,
                sbd_pub=62,
                sbd_h=30,
                selection="STAN",
                open=-6.0,
                live=-6.0,
                eva_open=-6.0,
                eva_line=-3.5,
            ),
        },
    }
    play = process_game(game, market="spread", sources=sources_for_league("NCAAF"))
    assert play is not None
    assert play["rlm_source_used"] == "eva"
    assert play["line_moved_toward"] == "away"
    assert play["open"] == 6.0
    assert play["live"] == 3.5


def test_rlm_falls_to_thespread_when_eva_is_flat() -> None:
    """EVA open==live: TheSpread open→live is the fallback."""
    game = {
        "matchup": "HAW @ STAN",
        "date": "2026-08-29",
        "spread": {
            "away": _side(
                public=39,
                handle=77,
                vsin_pub=40,
                vsin_h=75,
                sbd_pub=38,
                sbd_h=70,
                selection="HAW",
                open=6.0,
                live=3.5,
                eva_open=4.0,
                eva_line=4.0,
            ),
            "home": _side(
                public=61,
                handle=23,
                vsin_pub=60,
                vsin_h=25,
                sbd_pub=62,
                sbd_h=30,
                selection="STAN",
                open=-6.0,
                live=-3.5,
                eva_open=-4.0,
                eva_line=-4.0,
            ),
        },
    }
    play = process_game(game, market="spread", sources=sources_for_league("NCAAF"))
    assert play is not None
    assert play["rlm_source_used"] == "thespread"
    assert play["line_moved_toward"] == "away"


def test_rlm_prefers_polymarket_when_eva_flat_and_thespread_missing() -> None:
    """EVA flat and no TheSpread: Polymarket history (high liq) is used."""
    game = {
        "matchup": "UNC @ TCU",
        "date": "2026-08-29",
        "moneyline": {
            "away": _side(
                public=17,
                handle=38,
                vsin_pub=14,
                vsin_h=22,
                sbd_pub=6,
                sbd_h=23,
                selection="UNC",
                live=270,
                eva_open=280,
                eva_line=280,
                polymarket=_poly(
                    line_open=344,
                    line_live=250,
                    p_open=22.5,
                    p_live=28.5,
                    liquidity=15_000.0,
                ),
            ),
            "home": _side(
                public=83,
                handle=62,
                vsin_pub=86,
                vsin_h=78,
                sbd_pub=94,
                sbd_h=77,
                selection="TCU",
                live=-340,
                eva_open=-355,
                eva_line=-355,
                polymarket=_poly(
                    line_open=-344,
                    line_live=-203,
                    p_open=77.5,
                    p_live=67.0,
                    liquidity=15_000.0,
                ),
            ),
        },
    }
    play = process_game(game, market="moneyline", sources=sources_for_league("NCAAF"))
    assert play is not None
    assert play["rlm_source_used"] == "polymarket"
    assert play["line_moved_toward"] == "away"
    assert play["rlm_confirmed"] is True
    assert play["rlm_source_conflict"] is False
    assert play["eva_line_moved_toward"] is None
    assert play["polymarket_line_moved_toward"] == "away"
    assert play["polymarket_low_liquidity"] is False
    # Pair comes from Poly history, not eva_open 400 vs DK live 270
    assert play["open"] == 344
    assert play["live"] == 250


def test_rlm_low_liquidity_polymarket_falls_to_eva() -> None:
    game = {
        "matchup": "UNC @ TCU",
        "date": "2026-08-29",
        "moneyline": {
            "away": _side(
                public=17,
                handle=38,
                vsin_pub=14,
                vsin_h=22,
                sbd_pub=6,
                sbd_h=23,
                selection="UNC",
                live=270,
                eva_open=280,
                eva_line=250,
                polymarket=_poly(
                    line_open=344,
                    line_live=400,
                    p_open=28.5,
                    p_live=20.0,
                    liquidity=7.06,
                ),
            ),
            "home": _side(
                public=83,
                handle=62,
                vsin_pub=86,
                vsin_h=78,
                sbd_pub=94,
                sbd_h=77,
                selection="TCU",
                live=-340,
                eva_open=-335,
                eva_line=-310,
                polymarket=_poly(
                    line_open=-344,
                    line_live=-500,
                    p_open=71.5,
                    p_live=80.0,
                    liquidity=7.06,
                ),
            ),
        },
    }
    play = process_game(game, market="moneyline", sources=sources_for_league("NCAAF"))
    assert play is not None
    assert play["rlm_source_used"] == "eva"
    assert play["line_moved_toward"] == "away"
    assert play["rlm_source_conflict"] is True
    assert play["polymarket_low_liquidity"] is True
    assert play["polymarket_line_moved_toward"] == "home"
    assert play["open"] == 280
    assert play["live"] == 250
    assert play["live"] != 270  # DK live is not spliced into the EVA pair
