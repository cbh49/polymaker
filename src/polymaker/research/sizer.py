"""Deterministic unit sizing from article best-bet support vs opposite support."""

from __future__ import annotations

import re
from dataclasses import dataclass

from polymaker.research.load_matchups import parse_matchup_teams
from polymaker.research.schemas import (
    ArticleBets,
    BestPlay,
    ExtractedBet,
    SizedPlay,
    SourceSupport,
)

_ODDS_RE = re.compile(r"\([+-]\d+\)|[+-]\d{3,}")
_WS_RE = re.compile(r"\s+")
_TOTAL_RE = re.compile(
    r"\b(over|under|o|u)\s*([0-9]+(?:\.[0-9]+)?)\b",
    re.IGNORECASE,
)
_RUN_LINE_RE = re.compile(
    r"([a-z0-9 .'-]+?)\s*([+-]\s*[0-9]+(?:\.[0-9]+)?)\b",
    re.IGNORECASE,
)
_PROP_RE = re.compile(
    r"\b("
    r"pitcher\s*outs?|outs?\s*recorded|strikeouts?|hits?\s*allowed|"
    r"earned\s*runs?|home\s*runs?|stolen\s*bases?|"
    r"player\s*prop|props?\b|fantasy|"
    r"\brbi\b|\bks\b"
    r")\b",
    re.IGNORECASE,
)

MIN_SIDE_AGREEMENT = 2


@dataclass(frozen=True, slots=True)
class NormalizedPick:
    bet_type: str  # MONEYLINE | RUN_LINE | TOTAL
    team: str | None
    side: str | None  # OVER | UNDER for totals
    line: float | None
    raw: str


def _norm_text(s: str) -> str:
    s = s.lower().strip()
    s = _ODDS_RE.sub(" ", s)
    s = s.replace("½", ".5").replace("−", "-")
    s = _WS_RE.sub(" ", s)
    return s.strip()


def _team_aliases(name: str) -> set[str]:
    """Loose aliases: full name + last token (mascot)."""
    n = _norm_text(name)
    parts = n.split()
    aliases = {n}
    if parts:
        aliases.add(parts[-1])
    # common city+team: keep without leading "the"
    if n.startswith("the "):
        aliases.add(n[4:])
    return {a for a in aliases if a}


def parse_our_pick(play: BestPlay) -> NormalizedPick:
    """Normalize a Breton play into comparable fields."""
    pick = play.pick.strip()
    bt = (play.bet_type or "").upper()
    norm = _norm_text(pick)

    # Totals / OU
    if bt == "TOTAL" or play.category == "ou" or _TOTAL_RE.search(norm):
        m = _TOTAL_RE.search(norm)
        if m:
            side = "OVER" if m.group(1).lower().startswith("o") else "UNDER"
            return NormalizedPick("TOTAL", None, side, float(m.group(2)), pick)
        # UNDER 7.5 style already covered; fallback
        if "under" in norm:
            nums = re.findall(r"[0-9]+(?:\.[0-9]+)?", norm)
            return NormalizedPick("TOTAL", None, "UNDER", float(nums[0]) if nums else None, pick)
        if "over" in norm:
            nums = re.findall(r"[0-9]+(?:\.[0-9]+)?", norm)
            return NormalizedPick("TOTAL", None, "OVER", float(nums[0]) if nums else None, pick)

    # Run line
    if bt == "RUN_LINE" or re.search(r"[+-]\s*1\.5\b", norm):
        m = _RUN_LINE_RE.search(norm)
        if m:
            team = m.group(1).strip(" -")
            line = float(m.group(2).replace(" ", ""))
            return NormalizedPick("RUN_LINE", team, None, line, pick)

    # Moneyline
    team = norm
    for suffix in (" ml", " moneyline", " money line"):
        if team.endswith(suffix):
            team = team[: -len(suffix)].strip()
            break
    team = team.rstrip(" -").strip()
    return NormalizedPick("MONEYLINE", team or None, None, None, pick)


def _lines_close(a: float | None, b: float | None, tol: float = 0.26) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= tol


def _opponent_team(play: BestPlay, our_team: str | None) -> str | None:
    """Return the other team in the matchup when our pick names one side."""
    if not our_team:
        return None
    teams = parse_matchup_teams(play.matchup)
    if teams is None:
        return None
    away, home = teams
    our_aliases = _team_aliases(our_team)
    away_n, home_n = _norm_text(away), _norm_text(home)
    if any(a in away_n for a in our_aliases):
        return home
    if any(a in home_n for a in our_aliases):
        return away
    return None


def _bet_matches(our: NormalizedPick, bet: ExtractedBet) -> bool:
    """True if an extracted article bet agrees with our play."""
    if _is_player_prop(bet):
        return False
    sel = _norm_text(f"{bet.selection} {bet.raw}")
    bt = (bet.bet_type or "UNKNOWN").upper()
    if bt in ("OVER", "UNDER"):
        bt = "TOTAL"

    if our.bet_type == "TOTAL":
        if bt not in ("TOTAL", "UNKNOWN") and "over" not in sel and "under" not in sel:
            # still allow UNKNOWN if selection clearly is O/U
            pass
        side = (bet.side or "").upper()
        if side not in ("OVER", "UNDER"):
            if "under" in sel or re.search(r"\bu\s*[0-9]", sel):
                side = "UNDER"
            elif "over" in sel or re.search(r"\bo\s*[0-9]", sel):
                side = "OVER"
        if our.side and side and side != our.side:
            return False
        if our.side and not side:
            return False
        line = bet.line
        if line is None:
            m = _TOTAL_RE.search(sel)
            if m:
                line = float(m.group(2))
                if not side:
                    side = "OVER" if m.group(1).lower().startswith("o") else "UNDER"
        if our.side and side != our.side:
            return False
        if our.line is not None and line is not None and not _lines_close(our.line, line):
            return False
        # line optional if article omitted number but same side on same game context
        return bool(our.side and side == our.side)

    if our.bet_type == "RUN_LINE":
        if (
            bt not in ("RUN_LINE", "UNKNOWN", "SPREAD")
            and " -1.5" not in f" {sel}"
            and " +1.5" not in f" {sel}"
            and our.team
            and our.team not in sel
        ):
            return False
        if our.team:
            aliases = _team_aliases(our.team)
            if not any(a in sel for a in aliases):
                return False
        line = bet.line
        if line is None:
            m = re.search(r"([+-]\s*[0-9]+(?:\.[0-9]+)?)", sel)
            if m:
                line = float(m.group(1).replace(" ", ""))
        if our.line is not None and line is not None:
            # Favorites: -1.5 should match -1.5; also accept same absolute if team matches
            return _lines_close(our.line, line) or _lines_close(abs(our.line), abs(line))
        # team matched and article tagged RUN_LINE
        return bt in ("RUN_LINE", "SPREAD") or bool(re.search(r"[+-]\s*1\.5", sel))

    # MONEYLINE — same-team moneyline OR run line counts as support
    # (backing a team on the run line implies backing them to win).
    if our.team:
        aliases = _team_aliases(our.team)
        if not any(a in sel for a in aliases):
            return False
    if bt == "TOTAL" or "over" in sel or "under" in sel:
        # totals never support a moneyline
        if bt == "TOTAL":
            return False
        if re.search(r"\b(over|under)\b", sel) and not re.search(
            r"\b(ml|moneyline|money\s*line)\b", sel
        ):
            return False
    if bt in ("RUN_LINE", "SPREAD") or re.search(r"[+-]\s*1\.5\b", sel):
        return True
    # American odds moneyline e.g. "tigers -116" / explicit ML
    if re.search(r"[+-]\s*[1-9]\d{2,}\b", sel):
        return True
    if re.search(r"\b(ml|moneyline|money\s*line|free\s*pick)\b", sel):
        return True
    # team name alone in a pick selection — treat as ML lean
    return bt in ("MONEYLINE", "UNKNOWN", "")


def _bet_opposes(our: NormalizedPick, bet: ExtractedBet, *, opponent: str | None) -> bool:
    """True if an extracted bet backs the opposite side of our play."""
    if _is_player_prop(bet) or _bet_matches(our, bet):
        return False

    sel = _norm_text(f"{bet.selection} {bet.raw}")
    bt = (bet.bet_type or "UNKNOWN").upper()
    if bt in ("OVER", "UNDER"):
        bt = "TOTAL"

    if our.bet_type == "TOTAL":
        if our.side not in ("OVER", "UNDER"):
            return False
        opp_side = "UNDER" if our.side == "OVER" else "OVER"
        side = (bet.side or "").upper()
        if side not in ("OVER", "UNDER"):
            if "under" in sel or re.search(r"\bu\s*[0-9]", sel):
                side = "UNDER"
            elif "over" in sel or re.search(r"\bo\s*[0-9]", sel):
                side = "OVER"
        if side != opp_side:
            return False
        line = bet.line
        if line is None:
            m = _TOTAL_RE.search(sel)
            if m:
                line = float(m.group(2))
        if our.line is not None and line is not None and not _lines_close(our.line, line):
            return False
        return bt in ("TOTAL", "UNKNOWN") or bool(_TOTAL_RE.search(sel))

    # Team markets — opposite team ML / RL
    if not opponent:
        return False
    aliases = _team_aliases(opponent)
    if not any(a in sel for a in aliases):
        return False
    if bt == "TOTAL" or (
        re.search(r"\b(over|under)\b", sel)
        and not re.search(r"\b(ml|moneyline|money\s*line)\b", sel)
    ):
        return False
    if our.bet_type == "RUN_LINE":
        return (
            bt in ("RUN_LINE", "SPREAD", "MONEYLINE", "UNKNOWN", "")
            or bool(re.search(r"[+-]\s*1\.5\b", sel))
            or bool(re.search(r"[+-]\s*[1-9]\d{2,}\b", sel))
            or bool(re.search(r"\b(ml|moneyline)\b", sel))
        )
    # MONEYLINE opposed by opposite-team ML or RL
    return (
        bt in ("MONEYLINE", "RUN_LINE", "SPREAD", "UNKNOWN", "")
        or bool(re.search(r"[+-]\s*1\.5\b", sel))
        or bool(re.search(r"[+-]\s*[1-9]\d{2,}\b", sel))
        or bool(re.search(r"\b(ml|moneyline)\b", sel))
    )


def _text_supports_moneyline(our: NormalizedPick, text: str) -> bool:
    """Heuristic when the LLM returned no structured bets (e.g. 'Tigers -116')."""
    if our.bet_type != "MONEYLINE" or not our.team or not text:
        return False
    t = _norm_text(text)
    for alias in _team_aliases(our.team):
        # "detroit tigers -116" / "tigers -150" American odds moneyline
        if re.search(rf"\b{re.escape(alias)}\b\s*[+-]\s*[1-9]\d{{2,}}\b", t):
            return True
        # "free pick: detroit tigers" / "best bet: tigers ml"
        if re.search(
            rf"(?:free\s*pick|best\s*bet|official\s*pick)\s*:?\s*[^\n]{{0,60}}"
            rf"\b{re.escape(alias)}\b",
            t,
        ):
            return True
        if re.search(rf"\b{re.escape(alias)}\b\s*(?:ml|moneyline)\b", t):
            return True
        # same-team run line as a stated best bet also supports ML
        if re.search(
            rf"(?:best\s*bet|free\s*pick|run\s*line)[^\n]{{0,40}}"
            rf"\b{re.escape(alias)}\b\s*[+-]?\s*1\.5\b",
            t,
        ):
            return True
    return False


def _is_player_prop(bet: ExtractedBet) -> bool:
    """True for player/pitcher props, not game ML / run line / game total."""
    text = _norm_text(f"{bet.selection} {bet.raw}")
    if _PROP_RE.search(text):
        return True
    bt = (bet.bet_type or "").upper()
    # Game totals are usually small (6–12). Prop lines like 14.5 outs are higher
    # and typically include a player name rather than bare OVER/UNDER.
    return (
        bt == "TOTAL"
        and bet.line is not None
        and bet.line >= 12
        and not bool(re.match(r"^(over|under)\s*[0-9]", text))
    )


def _is_game_market_bet(bet: ExtractedBet) -> bool:
    """Moneyline, run line, or game total (not a player prop)."""
    if _is_player_prop(bet):
        return False
    bt = (bet.bet_type or "UNKNOWN").upper()
    if bt in ("MONEYLINE", "RUN_LINE", "SPREAD", "TOTAL"):
        return True
    sel = _norm_text(f"{bet.selection} {bet.raw}")
    if re.search(r"[+-]\s*1\.5\b", sel):
        return True
    if re.search(r"\b(ml|moneyline)\b", sel):
        return True
    if _TOTAL_RE.search(sel) and not _PROP_RE.search(sel):
        return True
    return bool(re.search(r"[+-]\s*[1-9]\d{2,}\b", sel))


def article_skip_reason(play: BestPlay, article: ArticleBets) -> str | None:
    """Return a skip reason if this article should be excluded from support math."""
    del play  # reserved for future market-specific skip rules
    if article.game_relevant is False:
        return "not_game_relevant"
    if (
        article.best_bets
        and all(_is_player_prop(b) for b in article.best_bets)
        and not any(_is_game_market_bet(b) for b in article.best_bets)
    ):
        return "props_only"
    return None


def article_supports(play: BestPlay, article: ArticleBets) -> bool:
    our = parse_our_pick(play)
    game_bets = [b for b in article.best_bets if not _is_player_prop(b)]
    if any(_bet_matches(our, b) for b in game_bets):
        return True
    return _text_supports_moneyline(our, article.content)


def article_opposes(play: BestPlay, article: ArticleBets) -> bool:
    our = parse_our_pick(play)
    opponent = _opponent_team(play, our.team)
    game_bets = [b for b in article.best_bets if not _is_player_prop(b)]
    return any(_bet_opposes(our, b, opponent=opponent) for b in game_bets)


def units_from_support(support_count: int, article_count: int) -> tuple[float, float]:
    """Legacy helper: (units, support_pct) from support / article_count.

    Prefer units_from_sides for the daily pipeline.
    """
    if article_count <= 0:
        return 1.0, 0.0
    pct = support_count / article_count
    if pct > 0.5:
        return 2.0, pct
    if pct == 0.5:
        return 1.0, pct
    return 0.5, pct


def units_from_sides(
    support_count: int,
    opposite_count: int,
    *,
    min_agreement: int = MIN_SIDE_AGREEMENT,
) -> tuple[float, float]:
    """Return (units, support_pct) from support vs opposite article counts.

    - Multiple articles support us more than the other side → 2.0u
    - Multiple articles support the other side more → 0.5u
    - Otherwise keep base 1.0u
    """
    total = support_count + opposite_count
    pct = (support_count / total) if total else 0.0
    if support_count >= min_agreement and support_count > opposite_count:
        return 2.0, pct
    if opposite_count >= min_agreement and opposite_count > support_count:
        return 0.5, pct
    return 1.0, pct


def size_play(
    play: BestPlay,
    articles: list[ArticleBets],
    *,
    query: str = "",
    min_agreement: int = MIN_SIDE_AGREEMENT,
    origin: str = "breton",
) -> SizedPlay:
    """Score article support vs opposite for one play and assign units.

    Articles with no stance on this market are ignored (neither support nor opposite).
    Off-topic / props-only articles are skipped entirely.
    """
    sources: list[SourceSupport] = []
    support_count = 0
    opposite_count = 0
    for art in articles:
        reason = article_skip_reason(play, art)
        if reason:
            sources.append(
                SourceSupport(
                    title=art.title,
                    url=art.url,
                    supports=None,
                    opposes=None,
                    skipped=True,
                    skip_reason=reason,
                    best_bets=list(art.best_bets),
                )
            )
            continue
        supports = article_supports(play, art)
        opposes = False if supports else article_opposes(play, art)
        if supports:
            support_count += 1
        elif opposes:
            opposite_count += 1
        sources.append(
            SourceSupport(
                title=art.title,
                url=art.url,
                supports=supports if (supports or opposes) else None,
                opposes=opposes if (supports or opposes) else None,
                skipped=False,
                best_bets=list(art.best_bets),
            )
        )
    units, pct = units_from_sides(
        support_count, opposite_count, min_agreement=min_agreement
    )
    return SizedPlay(
        matchup=play.matchup,
        time=play.time,
        pick=play.pick,
        bet_type=play.bet_type,
        units=units,
        support_count=support_count,
        opposite_count=opposite_count,
        article_count=support_count + opposite_count,
        support_pct=pct,
        sources=sources,
        query=query,
        origin="consensus" if origin == "consensus" else "breton",
    )
