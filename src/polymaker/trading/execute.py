"""Execute Polymarket buys for matched sharp-money plays."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from polymaker.catalog.sports import DEFAULT_PREGAME_BUFFER_MINUTES, is_pre_game
from polymaker.config import Config
from polymaker.domain import Side
from polymaker.execution.gateway import ExecutionGateway
from polymaker.trading.convex_trades import ConvexTradeClient, prediction_date_today
from polymaker.trading.fill import enrich_fill
from polymaker.trading.match import MatchedPlay
from polymaker.trading.sharp import SharpPlay, load_sharp_plays


@dataclass(frozen=True, slots=True)
class SharpTradeConfig:
    """Runtime knobs for sharp auto-buys (overridable via CLI)."""

    usd_tier_a: float = 25.0
    usd_tier_b: float = 10.0
    min_tier: str = "B"  # A = Tier A only; B = A+B
    markets: frozenset[str] = frozenset({"moneyline"})
    require_rlm: bool = False
    max_ask: float | None = 0.55
    min_edge: float | None = None  # require ask <= fair - min_edge
    filled_log: str = "journal/sharp_trades.jsonl"
    dry_run: bool = True
    pregame_buffer_minutes: float = DEFAULT_PREGAME_BUFFER_MINUTES


@dataclass(slots=True)
class SharpTradeResult:
    matched: MatchedPlay
    action: str  # bought | dry_run | skipped | failed
    usd: float = 0.0
    detail: str = ""
    response: dict[str, Any] = field(default_factory=dict)


async def run_sharp_trades(
    matched: list[MatchedPlay],
    cfg: Config,
    trade_cfg: SharpTradeConfig,
    *,
    gateway: ExecutionGateway | None = None,
) -> list[SharpTradeResult]:
    """Buy (or dry-run) each matched moneyline play that passes filters."""
    already = _load_filled_keys(trade_cfg.filled_log)
    owns_gw = gateway is None
    gw = gateway or ExecutionGateway(cfg, paper=trade_cfg.dry_run)
    results: list[SharpTradeResult] = []

    try:
        needs_connect = any(m.status == "matched" for m in matched)
        if needs_connect:
            await gw.connect()

        for m in matched:
            results.append(await _trade_one(m, gw, trade_cfg, already))
    finally:
        if owns_gw:
            gw.close()

    return results


def filter_plays(
    plays: list[SharpPlay],
    trade_cfg: SharpTradeConfig,
    *,
    league: str | None = None,
) -> list[SharpPlay]:
    """Apply tier / market / RLM / optional league gates before matching."""
    allow_b = trade_cfg.min_tier.upper() != "A"
    league_l = league.strip().upper() if league else None
    if league_l == "CFB":
        league_l = "NCAAF"
    out: list[SharpPlay] = []
    for p in plays:
        if league_l and league_l not in {"BOTH", "ALL"} and p.league.upper() != league_l:
            continue
        if p.market not in trade_cfg.markets:
            continue
        if p.tier in {"A+", "A"} or (allow_b and p.tier == "B"):
            if trade_cfg.require_rlm and not p.rlm_confirmed:
                continue
            out.append(p)
    return out


def default_sharp_paths(cfg: Config, *, league: str | None = None) -> list[Path]:
    sharp = cfg.sharp
    league_l = (league or "both").strip().lower()
    if league_l == "cfb":
        league_l = "ncaaf"
    if league_l == "mlb":
        return [Path(sharp.mlb_path)]
    if league_l == "wnba":
        return [Path(sharp.wnba_path)]
    if league_l == "ufc":
        return [Path(sharp.ufc_path)]
    if league_l == "ncaaf":
        return [Path(sharp.ncaaf_path)]
    return [Path(sharp.mlb_path), Path(sharp.wnba_path), Path(sharp.ufc_path), Path(sharp.ncaaf_path)]


def load_configured_plays(
    cfg: Config,
    paths: list[str | Path] | None = None,
    *,
    league: str | None = None,
) -> list[SharpPlay]:
    files = [Path(p) for p in paths] if paths else default_sharp_paths(cfg, league=league)
    existing = [p for p in files if p.is_file()]
    if not existing:
        raise FileNotFoundError(
            "no sharp money files found; expected " + ", ".join(str(p) for p in files)
        )
    return load_sharp_plays(existing)


async def _trade_one(
    m: MatchedPlay,
    gw: ExecutionGateway,
    trade_cfg: SharpTradeConfig,
    already: set[str],
) -> SharpTradeResult:
    if m.status != "matched" or m.meta is None or m.token is None or m.slug is None:
        return SharpTradeResult(matched=m, action="skipped", detail=m.detail or m.status)

    key = _fill_key(m.slug, m.token.outcome)
    if key in already:
        return SharpTradeResult(matched=m, action="skipped", detail="already traded (dedupe log)")

    usd = _usd_for_tier(m.play.tier, trade_cfg)
    if usd <= 0:
        return SharpTradeResult(matched=m, action="skipped", detail="usd size is 0")

    if not _is_pre_game_play(m, trade_cfg.pregame_buffer_minutes):
        return SharpTradeResult(
            matched=m, action="skipped", detail="not pre-game (startTime)", usd=usd
        )

    book = await gw.get_book(m.token.token_id)
    ask = _best_ask(book, m.meta.best_ask)
    skip_reason = _price_gate(ask, m.play.implied_fair_prob, trade_cfg)
    if skip_reason:
        return SharpTradeResult(matched=m, action="skipped", detail=skip_reason, usd=usd)

    if trade_cfg.dry_run:
        return SharpTradeResult(
            matched=m,
            action="dry_run",
            usd=usd,
            detail=f"would BUY ${usd:.2f} {m.token.outcome} @ ask={ask}",
            response={"book": book, "slug": m.slug},
        )

    convex = ConvexTradeClient()
    if not convex.configured:
        return SharpTradeResult(
            matched=m,
            action="skipped",
            usd=usd,
            detail="convex unavailable (fail closed)",
        )
    pred_date = (m.play.game_time_utc or "")[:10] or prediction_date_today()
    claim = convex.claim(
        trade_key_value=key,
        league=m.play.league,
        source="sharp_money",
        matchup=m.play.matchup,
        side=m.token.outcome,
        usd=usd,
        prediction_date=pred_date,
        slug=m.slug,
        condition_id=m.meta.condition_id,
        payload={"tier": m.play.tier, "ask": ask},
    )
    if not claim.claimed:
        return SharpTradeResult(matched=m, action="skipped", usd=usd, detail=claim.detail)

    try:
        resp = await gw.market_order(m.token.token_id, Side.BUY, usd, m.meta, fak=True)
    except Exception as exc:  # noqa: BLE001
        convex.release(key)
        return SharpTradeResult(
            matched=m, action="failed", usd=usd, detail=f"order error: {exc}"
        )

    status = str(resp.get("status", resp.get("error", ""))).lower()
    action = "bought" if "error" not in status and not resp.get("error") else "failed"
    start_iso = m.meta.start_time_iso or m.play.game_time_utc
    fill = enrich_fill(
        {
            "ts": time.time(),
            "key": key,
            "slug": m.slug,
            "outcome": m.token.outcome,
            "league": m.play.league,
            "matchup": m.play.matchup,
            "side": m.play.side,
            "tier": m.play.tier,
            "usd": usd,
            "ask": ask,
            "resp": resp,
        },
        token_id=m.token.token_id,
        start_time_iso=start_iso,
        ask=ask,
        resp=resp if isinstance(resp, dict) else None,
    )
    if action == "failed":
        convex.release(key)
        return SharpTradeResult(
            matched=m,
            action="failed",
            usd=usd,
            detail=status or "sent",
            response=resp if isinstance(resp, dict) else {"raw": resp},
        )

    try:
        convex.complete(key, fill)
    except Exception as exc:  # noqa: BLE001
        # Fill already happened — keep the claim so we never double-buy.
        fill["convex_complete_error"] = str(exc)
    _append_filled(trade_cfg.filled_log, fill)
    already.add(key)
    return SharpTradeResult(
        matched=m,
        action=action,
        usd=usd,
        detail=status or "sent",
        response=resp if isinstance(resp, dict) else {"raw": resp},
    )


def _is_pre_game_play(m: MatchedPlay, buffer_minutes: float) -> bool:
    """Final startTime gate. Does not trust event.live / gameStatus."""
    start = None
    if m.meta is not None:
        start = m.meta.start_time_iso
    if not start:
        start = m.play.game_time_utc
    return is_pre_game({"startTime": start}, buffer_minutes)


def _usd_for_tier(tier: str, cfg: SharpTradeConfig) -> float:
    if tier.upper() in {"A+", "A"}:
        return float(cfg.usd_tier_a)
    return float(cfg.usd_tier_b)


def _best_ask(book: dict[str, Any] | None, fallback: float) -> float | None:
    if book and book.get("best_ask") is not None:
        try:
            return float(book["best_ask"])
        except (TypeError, ValueError):
            pass
    if fallback and fallback > 0:
        return float(fallback)
    return None


def _price_gate(
    ask: float | None,
    fair: float | None,
    cfg: SharpTradeConfig,
) -> str | None:
    if cfg.max_ask is not None:
        if ask is None:
            return f"no ask (max_ask {cfg.max_ask})"
        if ask > cfg.max_ask:
            return f"ask {ask:.3f} above max_ask {cfg.max_ask}"
    if ask is None:
        return None
    if cfg.min_edge is not None and fair is not None and ask > fair - cfg.min_edge:
        # Buy only when market ask is cheaper than our fair by min_edge.
        return f"ask {ask:.3f} not below fair {fair:.3f} - edge {cfg.min_edge}"
    return None


def _fill_key(slug: str, outcome: str) -> str:
    return f"{slug}|{outcome.strip().lower()}"


def _load_filled_keys(path: str | Path) -> set[str]:
    p = Path(path)
    if not p.is_file():
        return set()
    keys: set[str] = set()
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        key = row.get("key")
        if isinstance(key, str):
            keys.add(key)
    return keys


def _append_filled(path: str | Path, payload: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, default=str) + "\n")
