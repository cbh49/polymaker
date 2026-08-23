"""Cluster equivalent extracted bets and keep those agreed on by 2+ articles."""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field

from wnba_bot.schemas import (
    ArticleFindings,
    ConsensusBet,
    ConsensusSource,
    ExtractedBet,
    GameBestBets,
    GameFindings,
    Matchup,
)

_ODDS_RE = re.compile(r"\([+-]\d+\)|[+-]\d{3,}")
_WS_RE = re.compile(r"\s+")
_PROP_RE = re.compile(
    r"\b("
    r"points?|rebounds?|assists?|pra|threes?|3pm|steals?|blocks?|"
    r"player\s*prop|props?\b|fantasy"
    r")\b",
    re.IGNORECASE,
)

SPREAD_TOL = 1.0
TOTAL_TOL = 1.5
MIN_AGREEMENT = 2


def _norm_text(s: str) -> str:
    s = s.lower().strip()
    s = _ODDS_RE.sub(" ", s)
    s = s.replace("½", ".5").replace("−", "-")
    s = _WS_RE.sub(" ", s)
    return s.strip()


def _team_aliases(name: str) -> set[str]:
    n = _norm_text(name)
    parts = n.split()
    aliases = {n}
    if parts:
        aliases.add(parts[-1])
    if n.startswith("the "):
        aliases.add(n[4:])
    return {a for a in aliases if a}


def _is_player_prop(bet: ExtractedBet) -> bool:
    blob = f"{bet.selection} {bet.raw}"
    return bool(_PROP_RE.search(blob))


def _resolve_team(bet: ExtractedBet, matchup: Matchup) -> str | None:
    """Return normalized away/home team name if the bet clearly picks one side."""
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


def _normalize_bet_type(bet: ExtractedBet) -> str:
    bt = (bet.bet_type or "UNKNOWN").upper()
    if bt in ("OVER", "UNDER"):
        return "TOTAL"
    if bt in ("RUN_LINE", "POINT_SPREAD", "ATS"):
        return "SPREAD"
    if bt in ("ML", "MONEY_LINE"):
        return "MONEYLINE"
    return bt


@dataclass
class _Cluster:
    bet_type: str
    team: str | None = None
    side: str | None = None  # OVER | UNDER for totals
    lines: list[float] = field(default_factory=list)
    sources: list[ConsensusSource] = field(default_factory=list)
    article_keys: set[str] = field(default_factory=set)

    def accepts(self, bet_type: str, team: str | None, side: str | None, line: float | None) -> bool:
        if self.bet_type != bet_type:
            return False
        if bet_type == "MONEYLINE":
            return bool(self.team and team and _norm_text(self.team) == _norm_text(team))
        if bet_type == "SPREAD":
            if not (self.team and team and _norm_text(self.team) == _norm_text(team)):
                return False
            if line is None:
                return not self.lines
            if not self.lines:
                return True
            return any(abs(line - existing) <= SPREAD_TOL for existing in self.lines)
        if bet_type == "TOTAL":
            if (self.side or "").upper() != (side or "").upper():
                return False
            if line is None:
                return not self.lines
            if not self.lines:
                return True
            return any(abs(line - existing) <= TOTAL_TOL for existing in self.lines)
        return False

    def add(self, article_key: str, source: ConsensusSource, line: float | None) -> None:
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
        if self.bet_type == "SPREAD":
            if self.team and line is not None:
                sign = "+" if line > 0 else ""
                return f"{self.team} {sign}{line:g}"
            return self.team or "SPREAD"
        if self.bet_type == "TOTAL":
            side = (self.side or "TOTAL").upper()
            if line is not None:
                return f"{side} {line:g}"
            return side
        return self.bet_type


def _article_key(article: ArticleFindings) -> str:
    return article.url or article.title


def build_consensus_for_game(
    game: GameFindings,
    matchup: Matchup,
    *,
    min_agreement: int = MIN_AGREEMENT,
) -> GameBestBets:
    """Cluster article bets for one game; keep those with mention_count >= min_agreement."""
    clusters: list[_Cluster] = []

    for article in game.articles:
        akey = _article_key(article)
        for bet in article.best_bets:
            if _is_player_prop(bet):
                continue
            bet_type = _normalize_bet_type(bet)
            if bet_type not in ("MONEYLINE", "SPREAD", "TOTAL"):
                continue

            team: str | None = None
            side: str | None = None
            line = bet.line

            if bet_type == "TOTAL":
                side = (bet.side or "").upper() or None
                if side not in ("OVER", "UNDER"):
                    blob = _norm_text(f"{bet.selection} {bet.raw}")
                    if blob.startswith("u") or " under" in f" {blob}":
                        side = "UNDER"
                    elif blob.startswith("o") or " over" in f" {blob}":
                        side = "OVER"
                    else:
                        continue
            else:
                team = _resolve_team(bet, matchup)
                if team is None:
                    continue

            source = ConsensusSource(title=article.title, url=article.url, raw=bet.raw or bet.selection)
            matched: _Cluster | None = None
            for cluster in clusters:
                if cluster.accepts(bet_type, team, side, line):
                    matched = cluster
                    break
            if matched is None:
                matched = _Cluster(bet_type=bet_type, team=team, side=side)
                clusters.append(matched)
            matched.add(akey, source, line)

    consensus: list[ConsensusBet] = []
    for cluster in clusters:
        count = len(cluster.article_keys)
        if count < min_agreement:
            continue
        consensus.append(
            ConsensusBet(
                bet_type=cluster.bet_type,
                selection=cluster.selection_label(),
                side=cluster.side if cluster.bet_type == "TOTAL" else None,
                line=cluster.median_line(),
                mention_count=count,
                sources=cluster.sources,
            )
        )

    consensus.sort(key=lambda b: (-b.mention_count, b.bet_type, b.selection))
    return GameBestBets(away=game.away, home=game.home, best_bets=consensus)


def build_consensus(
    games: list[GameFindings],
    matchups: list[Matchup],
    *,
    min_agreement: int = MIN_AGREEMENT,
) -> list[GameBestBets]:
    """Build consensus best bets for each game findings row."""
    by_key = {(m.away, m.home): m for m in matchups}
    out: list[GameBestBets] = []
    for game in games:
        matchup = by_key.get((game.away, game.home))
        if matchup is None:
            matchup = Matchup(away=game.away, home=game.home, lines=game.lines)
        out.append(build_consensus_for_game(game, matchup, min_agreement=min_agreement))
    return out
