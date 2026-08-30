#!/usr/bin/env python3
"""
Scrape consensus odds + timestamped line-history charts from EV Analytics.

Source pages:
  MLB:   https://evanalytics.com/mlb/odds
  WNBA:  https://evanalytics.com/wnba/odds
  NCAAF: https://evanalytics.com/ncaaf/odds

Line History charts are loaded from:
  POST /modules/odds/data/chart.php?sport=mlb|wnba|ncaaf&gid=...&tid=...&cid=...&parent_cid=...

Only the full-game board (category "Game Line") is kept so the series map
onto moneyline / spread / total in the combined splits JSON.

Chart payload (timestamps are Eastern, no year):
  moneyline2 = away American odds
  moneyline1 = home American odds
  spread     = home line (away is the negation)
  total      = game total number

Fields are prefixed with eva_ when merged:
  eva_line / eva_odds / eva_win_prob_pct / eva_open / eva_history

Usage:
  python scrape_eva_splits.py
  python scrape_eva_splits.py --league WNBA --out output/eva_wnba_betting_splits.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from mlb_team_map import DEFAULT_ABBREVS, load_abbr_to_name

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUT = {
    "MLB": SCRIPT_DIR / "output" / "eva_betting_splits.json",
    "WNBA": SCRIPT_DIR / "output" / "eva_wnba_betting_splits.json",
    "NCAAF": SCRIPT_DIR / "output" / "eva_ncaaf_betting_splits.json",
}

PAGE_URLS = {
    "MLB": "https://evanalytics.com/mlb/odds",
    "WNBA": "https://evanalytics.com/wnba/odds",
    "NCAAF": "https://evanalytics.com/ncaaf/odds",
}
CHART_URL = "https://evanalytics.com/modules/odds/data/chart.php"
CHART_TZ = ZoneInfo("America/New_York")
SPORT_CODES = {"MLB": "mlb", "WNBA": "wnba", "NCAAF": "ncaaf"}
# SYN to evanalytics.com can sit in SYN_SENT past requests' timeout and stall
# the whole NCAAF/WNBA/MLB aggregator with no log output. Cap each call and
# skip remaining charts after a couple of consecutive hangs.
HTTP_TIMEOUT = (5.0, 10.0)
HTTP_DEADLINE_S = 12.0
CHART_FAIL_FAST_AFTER = 2
_T = TypeVar("_T")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html;q=0.9",
}

ABBR_ALIASES: dict[str, str] = {
    "AZ": "Arizona Diamondbacks",
    "ARI": "Arizona Diamondbacks",
    "CWS": "Chicago White Sox",
    "CHW": "Chicago White Sox",
    "WSH": "Washington Nationals",
    "WAS": "Washington Nationals",
    "ATH": "Athletics",
    "OAK": "Athletics",
}

DATE_HEADER_RE = re.compile(
    r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),"
    r"\s+([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4})"
)


def load_abbr_to_team(path: Path) -> dict[str, str]:
    mapping = load_abbr_to_name(path)
    mapping.update(ABBR_ALIASES)
    return mapping


def team_name_from_abbr(abbr: str, abbr_map: dict[str, str]) -> str | None:
    return abbr_map.get(abbr.strip().upper())


def _parse_number(value: Any) -> float | int | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        num = float(value)
        return int(num) if num.is_integer() else num
    text = str(value).strip().replace("+", "").replace("o", "").replace("u", "")
    text = text.replace("%", "").replace(",", "")
    try:
        num = float(text)
        return int(num) if num.is_integer() else num
    except ValueError:
        return None


def _span_texts(td: Any) -> list[str]:
    if td is None:
        return []
    return [s.get_text(strip=True) for s in td.find_all("span") if s.get_text(strip=True)]


def _parse_date_header(text: str) -> date | None:
    m = DATE_HEADER_RE.search(text or "")
    if not m:
        return None
    try:
        return datetime.strptime(
            f"{m.group(2)} {m.group(3)} {m.group(4)}", "%B %d %Y"
        ).date()
    except ValueError:
        return None


def _parse_chart_ts(text: str, year: int) -> str | None:
    """Convert '08/18 07:19 PM' to an America/New_York ISO timestamp."""
    cleaned = (text or "").strip()
    if not cleaned:
        return None
    try:
        dt = datetime.strptime(f"{year}/{cleaned}", "%Y/%m/%d %I:%M %p")
    except ValueError:
        return None
    return dt.replace(tzinfo=CHART_TZ).isoformat()


def _history_points(
    rows: list[dict[str, Any]] | None,
    value_key: str,
    year: int,
    *,
    negate: bool = False,
) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    if not isinstance(rows, list):
        return points
    for row in rows:
        if not isinstance(row, dict):
            continue
        line = _parse_number(row.get(value_key))
        if line is None:
            continue
        if negate:
            line = -line if line != 0 else 0
        ts = _parse_chart_ts(str(row.get("update_date") or ""), year)
        point: dict[str, Any] = {"ts": ts, "line": line}
        if not ts:
            point["ts_raw"] = row.get("update_date")
        points.append(point)
    return points


def _side(
    selection: str,
    line: float | int | None,
    odds: float | int | None = None,
    history: list[dict[str, Any]] | None = None,
    win_prob_pct: float | int | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {"selection": selection, "eva_line": line}
    if odds is not None:
        row["eva_odds"] = odds
    if win_prob_pct is not None:
        row["eva_win_prob_pct"] = win_prob_pct
    hist = history or []
    if hist:
        row["eva_open"] = hist[0].get("line")
        row["eva_history"] = hist
    return row


def _call_with_deadline(fn: Callable[[], _T], timeout_s: float) -> _T:
    """Run `fn` on a daemon thread so a hung DNS/SSL connect cannot stall us."""
    box: list[_T] = []
    err: list[Exception] = []

    def _run() -> None:
        try:
            box.append(fn())
        except Exception as exc:  # noqa: BLE001
            err.append(exc)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout_s)
    if thread.is_alive():
        raise TimeoutError(f"timed out after {timeout_s:.0f}s")
    if err:
        raise err[0]
    if not box:
        raise TimeoutError(f"timed out after {timeout_s:.0f}s")
    return box[0]


def fetch_html(page_url: str) -> str:
    def _get() -> str:
        headers = {**HEADERS, "Referer": page_url}
        with requests.Session() as session:
            session.trust_env = False
            resp = session.get(page_url, headers=headers, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        return resp.text

    return _call_with_deadline(_get, HTTP_DEADLINE_S)


def fetch_chart(
    session: requests.Session,
    sport: str,
    gid: str,
    tid: str,
    cid: str,
    parent_cid: str,
    page_url: str,
) -> dict[str, Any]:
    params = {
        "sport": sport,
        "gid": gid,
        "tid": tid,
        "cid": cid,
        "parent_cid": parent_cid,
    }
    headers = {**HEADERS, "Referer": page_url, "Accept": "application/json"}

    def _post() -> dict[str, Any]:
        resp = session.post(CHART_URL, params=params, headers=headers, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else {}

    return _call_with_deadline(_post, HTTP_DEADLINE_S)


def _parse_data_chart(raw: str) -> dict[str, str] | None:
    # 0|mlb|gid|cid|parent_cid|tid|hteam|ateam|hprob|aprob
    parts = (raw or "").split("|")
    if len(parts) < 8:
        return None
    return {
        "sport": (parts[1] or "").lower(),
        "gid": parts[2],
        "cid": parts[3],
        "parent_cid": parts[4],
        "tid": parts[5],
        "home_abbr": parts[6].upper(),
        "away_abbr": parts[7].upper(),
        "home_win_prob": parts[8] if len(parts) > 8 else "",
        "away_win_prob": parts[9] if len(parts) > 9 else "",
    }


def parse_game_line_rows(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    section_day: date | None = None
    rows: list[dict[str, Any]] = []

    for tr in soup.find_all("tr"):
        classes = tr.get("class") or []
        if "eva-odds-date" in classes:
            section_day = _parse_date_header(tr.get_text(" ", strip=True))
            continue
        if tr.get("data-category") != "Game Line":
            continue
        if "eva-odds-row-link-container" in classes:
            continue

        tds = tr.find_all("td", recursive=False)
        if len(tds) < 8:
            continue
        link = tr.select_one(".eva-odds-row-link[data-chart]")
        chart_meta = _parse_data_chart(link.get("data-chart") if link else "")
        if not chart_meta:
            continue

        team_imgs = tds[1].find_all("img", class_="eva-odds-teamlogo")
        away_name = (team_imgs[0].get("alt") or "").strip() if len(team_imgs) >= 2 else None
        home_name = (team_imgs[1].get("alt") or "").strip() if len(team_imgs) >= 2 else None
        away_abbr = chart_meta["away_abbr"]
        home_abbr = chart_meta["home_abbr"]

        spread_txt = _span_texts(tds[3])
        total_txt = _span_texts(tds[4])
        ml_txt = _span_texts(tds[5])
        wp_txt = _span_texts(tds[6])
        time_txt = _span_texts(tds[0])

        away_spread = _parse_number(spread_txt[0]) if len(spread_txt) >= 2 else None
        away_spread_odds = _parse_number(spread_txt[1]) if len(spread_txt) >= 2 else None
        home_spread = _parse_number(spread_txt[2]) if len(spread_txt) >= 4 else None
        home_spread_odds = _parse_number(spread_txt[3]) if len(spread_txt) >= 4 else None

        over_line = _parse_number(total_txt[0]) if len(total_txt) >= 2 else None
        over_odds = _parse_number(total_txt[1]) if len(total_txt) >= 2 else None
        under_line = _parse_number(total_txt[2]) if len(total_txt) >= 4 else None
        under_odds = _parse_number(total_txt[3]) if len(total_txt) >= 4 else None

        away_ml = _parse_number(ml_txt[0]) if len(ml_txt) >= 1 else None
        home_ml = _parse_number(ml_txt[1]) if len(ml_txt) >= 2 else None

        away_wp = _parse_number(wp_txt[0]) if len(wp_txt) >= 1 else _parse_number(
            chart_meta.get("away_win_prob")
        )
        home_wp = _parse_number(wp_txt[1]) if len(wp_txt) >= 2 else _parse_number(
            chart_meta.get("home_win_prob")
        )

        rows.append(
            {
                "slate_date": section_day,
                "game_time_local": time_txt[0] if time_txt else None,
                "away_abbr": away_abbr,
                "home_abbr": home_abbr,
                "away": away_name,
                "home": home_name,
                "chart": chart_meta,
                "moneyline_live": {"away": away_ml, "home": home_ml},
                "spread_live": {
                    "away": (away_spread, away_spread_odds),
                    "home": (home_spread, home_spread_odds),
                },
                "total_live": {
                    "over": (over_line, over_odds),
                    "under": (under_line, under_odds),
                },
                "win_prob": {"away": away_wp, "home": home_wp},
            }
        )
    return rows


def _canonical_abbr(abbr: str, league: str, abbr_map: dict[str, str]) -> str:
    raw = (abbr or "").strip().upper()
    if league == "WNBA":
        from wnba_team_map import canonical_abbr

        return canonical_abbr(raw) or raw
    if league == "NCAAF":
        from cfb_team_map import canonical_abbr

        return canonical_abbr(raw) or raw
    return raw


def _canonical_name(name: str | None, abbr: str, league: str, abbr_map: dict[str, str]) -> str | None:
    if league == "WNBA":
        from wnba_team_map import canonical_name

        return canonical_name(name or abbr) or name or team_name_from_abbr(abbr, abbr_map)
    if league == "NCAAF":
        from cfb_team_map import canonical_name

        return canonical_name(name or abbr) or name or team_name_from_abbr(abbr, abbr_map)
    return name or team_name_from_abbr(abbr, abbr_map)


def build_game(
    row: dict[str, Any],
    chart: dict[str, Any],
    abbr_map: dict[str, str],
    league: str = "MLB",
) -> dict[str, Any]:
    slate_date: date | None = row.get("slate_date")
    year = datetime.now(CHART_TZ).date().year
    away_abbr = _canonical_abbr(row["away_abbr"], league, abbr_map)
    home_abbr = _canonical_abbr(row["home_abbr"], league, abbr_map)
    away_name = _canonical_name(row.get("away"), away_abbr, league, abbr_map)
    home_name = _canonical_name(row.get("home"), home_abbr, league, abbr_map)

    ml_away_hist = _history_points(chart.get("moneyline2"), "price", year)
    ml_home_hist = _history_points(chart.get("moneyline1"), "price", year)
    sp_home_hist = _history_points(chart.get("spread"), "spread", year)
    sp_away_hist = _history_points(chart.get("spread"), "spread", year, negate=True)
    tot_hist = _history_points(chart.get("total"), "total", year)

    away_sp, away_sp_odds = row["spread_live"]["away"]
    home_sp, home_sp_odds = row["spread_live"]["home"]
    over_line, over_odds = row["total_live"]["over"]
    under_line, under_odds = row["total_live"]["under"]

    moneyline = {
        "away": _side(
            away_abbr,
            row["moneyline_live"]["away"],
            history=ml_away_hist,
            win_prob_pct=row["win_prob"]["away"],
        ),
        "home": _side(
            home_abbr,
            row["moneyline_live"]["home"],
            history=ml_home_hist,
            win_prob_pct=row["win_prob"]["home"],
        ),
    }
    spread = {
        "away": _side(away_abbr, away_sp, away_sp_odds, sp_away_hist),
        "home": _side(home_abbr, home_sp, home_sp_odds, sp_home_hist),
    }
    total = {
        "over": _side("Over", over_line, over_odds, tot_hist),
        "under": _side("Under", under_line, under_odds, tot_hist),
    }

    game: dict[str, Any] = {
        "matchup": f"{away_abbr} @ {home_abbr}",
        "away_abbr": away_abbr,
        "home_abbr": home_abbr,
        "away": away_name,
        "home": home_name,
        "date": slate_date.isoformat() if slate_date else None,
        "game_time_local": row.get("game_time_local"),
        "eva_game_id": row["chart"]["gid"],
        "moneyline": moneyline,
        "spread": spread,
        "total": total,
    }
    return game


def scrape(
    day: date | None = None,
    league: str = "MLB",
    abbrevs_path: Path = DEFAULT_ABBREVS,
) -> dict[str, Any]:
    league = (league or "MLB").strip().upper()
    if league == "CFB":
        league = "NCAAF"
    if league not in PAGE_URLS:
        raise ValueError(f"Unsupported EVA league: {league}")
    page_url = PAGE_URLS[league]
    sport = SPORT_CODES[league]
    abbr_map = load_abbr_to_team(abbrevs_path) if league == "MLB" else {}
    html = fetch_html(page_url)
    rows = parse_game_line_rows(html)
    print(f"EV Analytics: {len(rows)} games (fetching line-history charts)…", flush=True)
    games: list[dict[str, Any]] = []
    consecutive_timeouts = 0
    skip_charts = False
    with requests.Session() as session:
        session.trust_env = False
        for i, row in enumerate(rows, 1):
            meta = row["chart"]
            chart: dict[str, Any] = {}
            if not skip_charts:
                try:
                    chart = fetch_chart(
                        session,
                        meta.get("sport") or sport,
                        meta["gid"],
                        meta["tid"],
                        meta["cid"] or "",
                        meta["parent_cid"] or meta["cid"] or "",
                        page_url,
                    )
                    consecutive_timeouts = 0
                except (
                    requests.RequestException,
                    ValueError,
                    json.JSONDecodeError,
                    TimeoutError,
                    OSError,
                ) as exc:
                    consecutive_timeouts += 1
                    if consecutive_timeouts >= CHART_FAIL_FAST_AFTER:
                        skip_charts = True
                        print(
                            f"Warning: EV Analytics charts stalled ({exc}); "
                            f"skipping remaining {len(rows) - i + 1} charts",
                            file=sys.stderr,
                            flush=True,
                        )
            games.append(build_game(row, chart, abbr_map, league=league))
            if i == 1 or i == len(rows) or i % 10 == 0:
                print(f"  chart {i}/{len(rows)}", flush=True)

    slate_dates = sorted({g.get("date") for g in games if g.get("date")})
    return {
        "source": "evanalytics.com",
        "source_page": page_url,
        "api": CHART_URL,
        "date": day.isoformat() if day else (slate_dates[0] if len(slate_dates) == 1 else None),
        "slate_dates": slate_dates,
        "league": league,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "game_count": len(games),
        "games": games,
    }


def merge_eva_into_game(game: dict[str, Any], eva_game: dict[str, Any]) -> None:
    """Copy eva_* fields onto matching side objects."""
    for market in ("moneyline", "spread", "total"):
        src_market = eva_game.get(market)
        if not isinstance(src_market, dict):
            continue
        dst_market = game.get(market)
        if not isinstance(dst_market, dict):
            dst_market = {}
            game[market] = dst_market
        for side, src_side in src_market.items():
            if not isinstance(src_side, dict):
                continue
            dst_side = dst_market.get(side)
            if not isinstance(dst_side, dict):
                dst_side = {"selection": src_side.get("selection")}
                dst_market[side] = dst_side
            for key, value in src_side.items():
                if key == "selection":
                    continue
                if key.startswith("eva_"):
                    dst_side[key] = value
    if eva_game.get("eva_game_id"):
        game["eva_game_id"] = eva_game["eva_game_id"]
    if eva_game.get("date"):
        game["eva_date"] = eva_game["date"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape EV Analytics line movement")
    parser.add_argument("--league", default="MLB", choices=["MLB", "WNBA", "NCAAF", "CFB"])
    parser.add_argument(
        "--date",
        default=None,
        help="Keep only this slate date YYYY-MM-DD (default: whatever the odds page shows)",
    )
    parser.add_argument("--abbrevs", type=Path, default=DEFAULT_ABBREVS)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    day = date.fromisoformat(args.date) if args.date else None
    result = scrape(day=day, league=args.league, abbrevs_path=args.abbrevs)
    out = args.out or DEFAULT_OUT[result["league"]]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {result['game_count']} {args.league} games → {out}")


if __name__ == "__main__":
    main()
