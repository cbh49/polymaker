"""Pydantic models for the WNBA best-bets research pipeline."""

from __future__ import annotations

from pydantic import BaseModel, Field


class MatchupLines(BaseModel):
    ml_away: str = ""
    ml_home: str = ""
    spread_away: str = ""
    spread_home: str = ""
    total: str = ""


class Matchup(BaseModel):
    away: str
    home: str
    game_time: str = ""
    lines: MatchupLines = Field(default_factory=MatchupLines)
    favorite: str = ""
    espn_game_id: str = ""


class ArticleRef(BaseModel):
    title: str
    url: str
    snippet: str = ""
    content: str = ""
    published: str | None = None


class ExtractedBet(BaseModel):
    bet_type: str = "UNKNOWN"
    selection: str = ""
    side: str | None = None
    line: float | None = None
    raw: str = ""


class ArticleFindings(BaseModel):
    title: str
    url: str = ""
    game_relevant: bool | None = None
    best_bets: list[ExtractedBet] = Field(default_factory=list)


class GameFindings(BaseModel):
    away: str
    home: str
    game_time: str = ""
    lines: MatchupLines = Field(default_factory=MatchupLines)
    query: str = ""
    articles: list[ArticleFindings] = Field(default_factory=list)


class FindingsFile(BaseModel):
    generated_at: str
    source_matchups: str
    games: list[GameFindings] = Field(default_factory=list)


class ConsensusSource(BaseModel):
    title: str
    url: str = ""
    raw: str = ""


class ConsensusBet(BaseModel):
    bet_type: str
    selection: str
    side: str | None = None
    line: float | None = None
    mention_count: int
    sources: list[ConsensusSource] = Field(default_factory=list)


class GameBestBets(BaseModel):
    away: str
    home: str
    best_bets: list[ConsensusBet] = Field(default_factory=list)


class BestBetsFile(BaseModel):
    generated_at: str
    min_agreement: int = 2
    games: list[GameBestBets] = Field(default_factory=list)


class TaggedBet(BaseModel):
    """A best bet extracted from a slate-wide article, tagged to a matchup."""

    away: str = ""
    home: str = ""
    bet_type: str = "UNKNOWN"
    selection: str = ""
    side: str | None = None
    line: float | None = None
    raw: str = ""


class DailyArticleFindings(BaseModel):
    title: str
    url: str = ""
    published: str | None = None
    best_bets: list[TaggedBet] = Field(default_factory=list)


class DailyFindingsFile(BaseModel):
    generated_at: str
    mode: str = "daily"
    query: str = ""
    hours: int = 12
    article_limit: int = 15
    source_matchups: str = ""
    articles: list[DailyArticleFindings] = Field(default_factory=list)
