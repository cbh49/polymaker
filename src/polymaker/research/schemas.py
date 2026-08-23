"""Pydantic models for the MLB research / unit-sizing pipeline."""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class BetType(str, Enum):
    MONEYLINE = "MONEYLINE"
    RUN_LINE = "RUN_LINE"
    TOTAL = "TOTAL"
    UNKNOWN = "UNKNOWN"


class MatchupLines(BaseModel):
    ml_away: str = ""
    ml_home: str = ""
    run_line_away: str = ""
    run_line_home: str = ""
    total: str = ""


class Matchup(BaseModel):
    away: str
    home: str
    game_time: str = ""
    lines: MatchupLines = Field(default_factory=MatchupLines)
    favorite: str = ""
    espn_game_id: str = ""


class BestPlay(BaseModel):
    """One play from llm_best_plays.json (ml or ou)."""

    matchup: str
    time: str = ""
    pick: str
    bet_type: str | None = None
    category: Literal["ml", "ou"] = "ml"


class BestPlaysFile(BaseModel):
    ml_best_plays: list[BestPlay] = Field(default_factory=list)
    ou_best_plays: list[BestPlay] = Field(default_factory=list)


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


class TaggedBet(BaseModel):
    """A best bet extracted from a slate-wide article, tagged to a matchup."""

    away: str = ""
    home: str = ""
    bet_type: str = "UNKNOWN"
    selection: str = ""
    side: str | None = None
    line: float | None = None
    raw: str = ""


class ArticleBets(BaseModel):
    title: str
    url: str = ""
    best_bets: list[ExtractedBet] = Field(default_factory=list)
    content: str = ""  # article text for heuristic fallback when LLM misses picks
    # None = unknown; False = off-topic / props-only (exclude from support math)
    game_relevant: bool | None = None


class DailyArticleFindings(BaseModel):
    title: str
    url: str = ""
    published: str | None = None
    best_bets: list[TaggedBet] = Field(default_factory=list)


class SourceSupport(BaseModel):
    title: str
    url: str
    supports: bool | None = None  # None when skipped / no stance
    opposes: bool | None = None
    skipped: bool = False
    skip_reason: str = ""
    best_bets: list[ExtractedBet] = Field(default_factory=list)


class SizedPlay(BaseModel):
    matchup: str
    time: str = ""
    pick: str
    bet_type: str | None = None
    units: float
    support_count: int
    opposite_count: int = 0
    article_count: int
    support_pct: float
    sources: list[SourceSupport] = Field(default_factory=list)
    query: str = ""
    origin: Literal["breton", "consensus"] = "breton"


class SizedPlaysFile(BaseModel):
    generated_at: str
    source_plays: str
    source_matchups: str = ""
    query: str = ""
    hours: int = 12
    article_limit: int = 15
    min_agreement: int = 2
    ml_best_plays: list[SizedPlay] = Field(default_factory=list)
    ou_best_plays: list[SizedPlay] = Field(default_factory=list)
    additional_plays: list[SizedPlay] = Field(default_factory=list)
