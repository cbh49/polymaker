"""Cluster slate-wide article bets into consensus plays for games without Breton picks."""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field

from polymaker.research.load_matchups import matchup_key, parse_matchup_teams
from polymaker.research.schemas import (
    BestPlay,
    DailyArticleFindings,
    ExtractedBet,
    Matchup,
    SizedPlay,
    SourceSupport,
    TaggedBet,
)
from polymaker.research.sizer import MIN_SIDE_AGREEMENT, _is_player_prop, _norm_text, _team_aliases

RUN_LINE_TOL = 0.26
TOTAL_TOL = 0.26


@dataclass
class _Cluster:
    bet_type: str
    away: str
    home: str
    team: str | None = None
    side: str | None = None
    lines: list[float] = field(default_factory=list)
    sources: list[SourceSupport] = field(default_factory=list)
    article_keys: set[str] = field(default_factory=set)

    def accepts(self, bet_type: str, team: str | None, side: str | None, line: float | None) -> bool:
        if self.bet_type != bet_type:
            return False
        if bet_type == "MONEYLINE":
            return bool(self.team and team and _norm_text(self.team) == _norm_text(team))
        if bet_type == "RUN_LINE":
            if not (self.team and team and _norm_text(self.team) == _norm_text(team)):
                return False
            if line is None:
                return not self.lines
            if not self.lines:
                return True
            return any(abs(line - existing) <= RUN_LINE_TOL for existing in self.lines)
        if bet_type == "TOTAL":
            if (self.side or "").upper() != (side or "").upper():
                return False
            if line is None:
                return not self.lines
            if not self.lines:
                return True
            return any(abs(line - existing) <= TOTAL_TOL for existing in self.lines)
        return False

    def add(self, article_key: str, source: SourceSupport, line: float | None) -> None:
        if article_key in self.article_keys:
            return
        self.article_keys.add(article_key)
        self.sources.append(source)
        if line is not None:
            self.lines.append(line)

    def median_line(self) -> float | None:
        if not self.lines:
            return None
        return float(statistics.median(self.lines))

    def selection_label(self) -> str:
        line = self.median_line()
        if self.bet_type == "MONEYLINE":
            return f"{self.team} ML" if self.team else "ML"
        if self.bet_type == "RUN_LINE":
            if self.team and line is not None:
                sign = "+" if line > 0 else ""
                return f"{self.team} {sign}{line:g}"
            return f"{self.team} RL" if self.team else "RUN_LINE"
        if self.bet_type == "TOTAL":
            side = (self.side or "TOTAL").upper()
            if line is not None:
                return f"{side} {line:g}"
            return side
        return self.bet_type

    def to_sized_play(self, *, query: str, game_time: str = "") -> SizedPlay:
        count = len(self.article_keys)
        return SizedPlay(
            matchup=matchup_key(self.away, self.home),
            time=game_time,
            pick=self.selection_label(),
            bet_type=self.bet_type,
            units=1.0,
            support_count=count,
            opposite_count=0,
            article_count=count,
            support_pct=1.0,
            sources=self.sources,
            query=query,
            origin="consensus",
        )


def _normalize_bet_type(bet: TaggedBet | ExtractedBet) -> str:
    bt = (bet.bet_type or "UNKNOWN").upper()
    if bt in ("OVER", "UNDER"):
        return "TOTAL"
    if bt in ("SPREAD", "POINT_SPREAD", "ATS"):
        return "RUN_LINE"
    if bt in ("ML", "MONEY_LINE"):
        return "MONEYLINE"
    return bt


def _resolve_matchup(bet: TaggedBet, matchups: list[Matchup]) -> Matchup | None:
    away_n = _norm_text(bet.away)
    home_n = _norm_text(bet.home)
    if away_n and home_n:
        for m in matchups:
            if _norm_text(m.away) == away_n and _norm_text(m.home) == home_n:
                return m
            if _norm_text(m.away) == home_n and _norm_text(m.home) == away_n:
                return m

    blob = _norm_text(f"{bet.away} {bet.home} {bet.selection} {bet.raw}")
    if not blob:
        return None

    best: Matchup | None = None
    best_hits = 0
    for m in matchups:
        away_hit = any(a and a in blob for a in _team_aliases(m.away))
        home_hit = any(a and a in blob for a in _team_aliases(m.home))
        hits = int(away_hit) + int(home_hit)
        if hits > best_hits:
            best_hits = hits
            best = m
    return best if best_hits >= 1 else None


def _resolve_team(bet: TaggedBet, matchup: Matchup) -> str | None:
    side = (bet.side or "").upper()
    if side == "AWAY":
        return matchup.away
    if side == "HOME":
        return matchup.home

    blob = _norm_text(f"{bet.selection} {bet.raw}")
    away_aliases = _team_aliases(matchup.away)
    home_aliases = _team_aliases(matchup.home)
    hit_away = any(a and a in blob for a in away_aliases)
    hit_home = any(a and a in blob for a in home_aliases)
    if hit_away and not hit_home:
        return matchup.away
    if hit_home and not hit_away:
        return matchup.home
    return None


def _to_extracted(bet: TaggedBet) -> ExtractedBet:
    return ExtractedBet(
        bet_type=bet.bet_type,
        selection=bet.selection,
        side=bet.side,
        line=bet.line,
        raw=bet.raw,
    )


def articles_for_play(
    play: BestPlay,
    articles: list[DailyArticleFindings],
    matchups: list[Matchup],
) -> list[tuple[DailyArticleFindings, list[ExtractedBet]]]:
    """Filter slate articles down to bets tagged to this play's matchup."""
    teams = parse_matchup_teams(play.matchup)
    if teams is None:
        return []
    away, home = teams
    away_n, home_n = _norm_text(away), _norm_text(home)

    out: list[tuple[DailyArticleFindings, list[ExtractedBet]]] = []
    for art in articles:
        bets: list[ExtractedBet] = []
        for bet in art.best_bets:
            m = _resolve_matchup(bet, matchups)
            if m is None:
                continue
            if _norm_text(m.away) == away_n and _norm_text(m.home) == home_n:
                bets.append(_to_extracted(bet))
        if bets:
            out.append((art, bets))
    return out


def breton_matchup_keys(plays: list[BestPlay]) -> set[str]:
    keys: set[str] = set()
    for play in plays:
        teams = parse_matchup_teams(play.matchup)
        if teams is None:
            continue
        keys.add(matchup_key(teams[0], teams[1]))
    return keys


def build_additional_plays(
    articles: list[DailyArticleFindings],
    matchups: list[Matchup],
    breton_plays: list[BestPlay],
    *,
    query: str = "",
    min_agreement: int = MIN_SIDE_AGREEMENT,
) -> list[SizedPlay]:
    """Consensus bets on slate games that are not already in llm_best_plays."""
    covered = breton_matchup_keys(breton_plays)
    clusters: list[_Cluster] = []
    time_by_key = {matchup_key(m.away, m.home): m.game_time for m in matchups}

    for art in articles:
        akey = art.url or art.title
        for bet in art.best_bets:
            extracted = _to_extracted(bet)
            if _is_player_prop(extracted):
                continue
            matchup = _resolve_matchup(bet, matchups)
            if matchup is None:
                continue
            key = matchup_key(matchup.away, matchup.home)
            if key in covered:
                continue

            bet_type = _normalize_bet_type(bet)
            if bet_type not in ("MONEYLINE", "RUN_LINE", "TOTAL"):
                continue

            team: str | None = None
            side: str | None = None
            line = bet.line

            if bet_type == "TOTAL":
                side = (bet.side or "").upper() or None
                if side not in ("OVER", "UNDER"):
                    blob = _norm_text(f"{bet.selection} {bet.raw}")
                    if "under" in blob or re.search(r"\bu\s*[0-9]", blob):
                        side = "UNDER"
                    elif "over" in blob or re.search(r"\bo\s*[0-9]", blob):
                        side = "OVER"
                    else:
                        continue
            else:
                team = _resolve_team(bet, matchup)
                if team is None:
                    continue

            source = SourceSupport(
                title=art.title,
                url=art.url,
                supports=True,
                opposes=False,
                best_bets=[extracted],
            )
            matched: _Cluster | None = None
            for cluster in clusters:
                if (
                    cluster.away == matchup.away
                    and cluster.home == matchup.home
                    and cluster.accepts(bet_type, team, side, line)
                ):
                    matched = cluster
                    break
            if matched is None:
                matched = _Cluster(
                    bet_type=bet_type,
                    away=matchup.away,
                    home=matchup.home,
                    team=team,
                    side=side,
                )
                clusters.append(matched)
            matched.add(akey, source, line)

    additional: list[SizedPlay] = []
    for cluster in clusters:
        if len(cluster.article_keys) < min_agreement:
            continue
        key = matchup_key(cluster.away, cluster.home)
        additional.append(
            cluster.to_sized_play(query=query, game_time=time_by_key.get(key, ""))
        )

    additional.sort(key=lambda p: (-p.support_count, p.matchup, p.pick))
    return additional
