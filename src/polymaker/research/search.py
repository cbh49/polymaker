"""Web search + article fetch for MLB best-play research."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from ddgs import DDGS
from ddgs.exceptions import DDGSException

from polymaker.research.schemas import ArticleRef

NUM_ARTICLES = 4  # legacy per-play search
NUM_DAILY_ARTICLES = 15
DAILY_HOURS = 12
MAX_ARTICLE_CHARS = 5000
MAX_DAILY_ARTICLE_CHARS = 8000
BLOCKED_DOMAINS = {
    "x.com",
    "twitter.com",
    "reddit.com",
    "redd.it",
    "tiktok.com",
    "wikipedia.org",
    "news.google.com",
    "365scores.com",
    "espn.com",
    "oddspedia.com",
    "signalodds.com",
    "youtube.com",
}

_BETTING_RE = re.compile(
    r"\b(best\s+bets?|free\s+picks?|picks?|prediction|odds|run\s*line|"
    r"moneyline|over/?under|props?|betting)\b",
    re.IGNORECASE,
)
_MLB_RE = re.compile(
    r"\bmlb\b|"
    r"\b(baseball|yankees|red\s*sox|blue\s*jays|orioles|rays|white\s*sox|"
    r"guardians|tigers|twins|royals|astros|rangers|athletics|a'?s\b|angels|"
    r"mariners|dodgers|giants|padres|diamondbacks|d[\s-]?backs|rockies|"
    r"phillies|braves|mets|marlins|nationals|cubs|cardinals|brewers|"
    r"pirates|reds|cleveland|detroit|minnesota|kansas\s*city|houston|"
    r"texas|seattle|los\s*angeles|san\s*francisco|san\s*diego|arizona|"
    r"colorado|philadelphia|atlanta|new\s*york|miami|washington|chicago|"
    r"st\.?\s*louis|milwaukee|pittsburgh|cincinnati|baltimore|tampa|"
    r"toronto|boston|oakland)\b",
    re.IGNORECASE,
)
_EXCLUDE_SPORT_RE = re.compile(
    r"\b(wnba|nfl|nhl|nba|soccer|premier\s+league|ufc|mma|golf|tennis|nascar)\b",
    re.IGNORECASE,
)


def build_search_query(matchup: str, when: datetime | None = None) -> str:
    """Build a DDG query from matchup + local date, e.g. 'Orioles Rangers Aug 8 best bets'."""
    dt = when or datetime.now()
    cleaned = matchup.replace("@", " ")
    cleaned = " ".join(cleaned.split())
    date_part = f"{dt.strftime('%b')} {dt.day}"
    return f"{cleaned} {date_part} best bets"


def build_daily_search_query(when: datetime | None = None) -> str:
    """Build a slate-wide DDG query, e.g. 'MLB August 11 Best Bets'."""
    dt = when or datetime.now()
    date_part = f"{dt.strftime('%B')} {dt.day}"
    return f"MLB {date_part} Best Bets"


def _is_blocked(url: str) -> bool:
    try:
        host = urlparse(url).hostname or ""
        return any(host == d or host.endswith("." + d) for d in BLOCKED_DOMAINS)
    except Exception:
        return False


def _parse_publish_date(date_str: str) -> datetime | None:
    try:
        pub = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        if pub.tzinfo is None:
            pub = pub.replace(tzinfo=UTC)
        return pub
    except (ValueError, TypeError):
        return None


def _timelimit_for_days(days: int) -> str:
    if days <= 1:
        return "d"
    if days <= 7:
        return "w"
    return "m"


def _search_web_fallback(query: str, existing: list[ArticleRef]) -> list[ArticleRef]:
    seen_urls = {r.url for r in existing}
    results = list(existing)
    try:
        with DDGS() as ddgs:
            for r in ddgs.text(query + " picks predictions analysis", max_results=20, timelimit="w"):
                url = r.get("href", "")
                if not url or _is_blocked(url) or url in seen_urls:
                    continue
                results.append(
                    ArticleRef(
                        title=r.get("title", "No title"),
                        url=url,
                        snippet=r.get("body", ""),
                        content="",
                        published=None,
                    )
                )
                seen_urls.add(url)
                if len(results) == NUM_ARTICLES:
                    break
    except DDGSException:
        pass
    return results


def search_web(query: str, days: int = 2) -> list[ArticleRef]:
    """Return recent news articles published within the last `days` days.

    Search failures / empty DDG responses return [] instead of raising.
    """
    cutoff = datetime.now(UTC) - timedelta(days=days)
    timelimit = _timelimit_for_days(days)

    results: list[ArticleRef] = []
    try:
        with DDGS() as ddgs:
            for r in ddgs.news(query + " picks predictions", max_results=40, timelimit=timelimit):
                url = r.get("url", "")
                if not url or _is_blocked(url):
                    continue
                pub = _parse_publish_date(r.get("date", ""))
                if pub is None or pub < cutoff:
                    continue
                results.append(
                    ArticleRef(
                        title=r.get("title", "No title"),
                        url=url,
                        snippet=r.get("body", ""),
                        content="",
                        published=pub.isoformat(),
                    )
                )
                if len(results) == NUM_ARTICLES:
                    break
    except DDGSException:
        results = []

    if len(results) < NUM_ARTICLES:
        results = _search_web_fallback(query, results)

    return results[:NUM_ARTICLES]


def _daily_relevance(title: str, snippet: str = "") -> int | None:
    """Score MLB betting relevance; None means drop the article."""
    blob = f"{title} {snippet}"
    if _EXCLUDE_SPORT_RE.search(blob) and not _MLB_RE.search(blob):
        return None
    if _EXCLUDE_SPORT_RE.search(title) and "mlb" not in title.lower() and "baseball" not in title.lower():
        return None

    has_mlb = bool(_MLB_RE.search(blob))
    has_betting = bool(_BETTING_RE.search(blob))
    if not (has_mlb and has_betting):
        return None

    score = 3
    if re.search(r"\bbest\s+bets?\b", blob, re.IGNORECASE):
        score += 3
    if re.search(r"\b(prediction|picks?|odds|moneyline|run\s*line)\b", blob, re.IGNORECASE):
        score += 2
    return score


def search_daily_articles(
    query: str,
    *,
    hours: int = DAILY_HOURS,
    limit: int = NUM_DAILY_ARTICLES,
) -> list[ArticleRef]:
    """Return slate-wide best-bet articles published within the last `hours` hours."""
    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    candidates: list[tuple[int, ArticleRef]] = []
    seen_urls: set[str] = set()

    def _consider(title: str, url: str, snippet: str, published: str | None) -> None:
        if not url or _is_blocked(url) or url in seen_urls:
            return
        score = _daily_relevance(title, snippet)
        if score is None:
            return
        seen_urls.add(url)
        candidates.append(
            (
                score,
                ArticleRef(
                    title=title or "No title",
                    url=url,
                    snippet=snippet,
                    content="",
                    published=published,
                ),
            )
        )

    try:
        with DDGS() as ddgs:
            for r in ddgs.news(query, max_results=80, timelimit="d"):
                pub = _parse_publish_date(r.get("date", ""))
                if pub is None or pub < cutoff:
                    continue
                _consider(
                    r.get("title", "No title"),
                    r.get("url", ""),
                    r.get("body", ""),
                    pub.isoformat(),
                )
    except DDGSException:
        pass

    if len(candidates) < limit:
        try:
            with DDGS() as ddgs:
                for r in ddgs.text(query + " picks predictions", max_results=40, timelimit="d"):
                    _consider(
                        r.get("title", "No title"),
                        r.get("href", ""),
                        r.get("body", ""),
                        None,
                    )
        except DDGSException:
            pass

    candidates.sort(key=lambda item: (-item[0], item[1].published or ""))
    return [art for _, art in candidates[:limit]]


def fetch_article(url: str, *, max_chars: int = MAX_ARTICLE_CHARS) -> str:
    """Fetch a URL and return cleaned article text (truncated)."""
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        }
        resp = requests.get(url, headers=headers, timeout=12)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content, "lxml")
        for tag in soup(
            ["script", "style", "nav", "footer", "header", "aside", "iframe", "noscript", "form", "button"]
        ):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        return "\n".join(lines)[:max_chars]
    except Exception:
        return ""


def fetch_all(results: list[ArticleRef], *, max_chars: int = MAX_ARTICLE_CHARS) -> None:
    """Populate content on each article (snippet fallback if fetch fails)."""
    for r in results:
        content = fetch_article(r.url, max_chars=max_chars)
        r.content = content if content else r.snippet
