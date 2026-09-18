"""Execute sharp-money buys on the cheaper of Polymarket and Kalshi."""

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
from polymaker.trading.event_key import (
    canonical_trade_key,
    event_date_for_play,
    poly_slug_key,
)
from polymaker.trading.fill import enrich_fill
from polymaker.trading.kalshi_match import KalshiMatchedPlay, match_kalshi_plays
from polymaker.trading.match import MatchedPlay
from polymaker.trading.sharp import SharpPlay, load_sharp_plays
from polymaker.trading.venue_quote import PricedVenue, pick_venue, price_venue


@dataclass(frozen=True, slots=True)
class SharpTradeConfig:
    """Runtime knobs for sharp auto-buys (overridable via CLI)."""

    usd_tier_a: float = 25.0
    usd_tier_b: float = 10.0
    min_tier: str = "B"  # A = Tier A only; B = A+B
    markets: frozenset[str] = frozenset({"moneyline", "spread", "total"})
    require_rlm: bool = False
    max_ask: float | None = 0.60
    min_moneyline_ask: float | None = 0.35  # skip ML if best ask is cheaper than this
    min_edge: float | None = None  # require ask <= fair - min_edge
    filled_log: str = "journal/sharp_trades.jsonl"
    dry_run: bool = True
    pregame_buffer_minutes: float = DEFAULT_PREGAME_BUFFER_MINUTES
    venues: frozenset[str] = frozenset({"polymarket", "kalshi"})
    tie_venue: str = "polymarket"
    max_ask_slippage: float = 0.01


@dataclass(slots=True)
class SharpTradeResult:
    matched: MatchedPlay
    action: str  # bought | dry_run | skipped | failed
    usd: float = 0.0
    detail: str = ""
    response: dict[str, Any] = field(default_factory=dict)
    venue: str | None = None
    ask: float | None = None
    fee: float | None = None
    all_in: float | None = None
    ticker: str | None = None


async def run_sharp_trades(
    matched: list[MatchedPlay],
    cfg: Config,
    trade_cfg: SharpTradeConfig,
    *,
    gateway: ExecutionGateway | None = None,
    kalshi_client: Any | None = None,
    kalshi_matches: list[KalshiMatchedPlay] | None = None,
) -> list[SharpTradeResult]:
    """Buy (or dry-run) each play on the cheaper fee-adjusted venue."""
    already = _load_filled_keys(trade_cfg.filled_log)
    owns_gw = gateway is None
    gw = gateway or ExecutionGateway(cfg, paper=trade_cfg.dry_run)
    owns_kalshi = False
    kalshi = kalshi_client
    kalshi_rows = kalshi_matches
    results: list[SharpTradeResult] = []

    try:
        poly_enabled = "polymarket" in trade_cfg.venues
        kalshi_enabled = "kalshi" in trade_cfg.venues
        needs_poly = poly_enabled and any(m.status == "matched" for m in matched)
        if needs_poly:
            await gw.connect()

        if kalshi_enabled and kalshi_rows is None:
            if kalshi is None:
                try:
                    from ev_trading.venues.kalshi.api import KalshiClient

                    kalshi = KalshiClient.from_env(require_auth=False)
                    owns_kalshi = True
                except Exception:  # noqa: BLE001
                    kalshi = None
            kalshi_rows = match_kalshi_plays(
                [m.play for m in matched],
                client=kalshi,
                markets=trade_cfg.markets,
            )
        if kalshi_rows is None:
            kalshi_rows = [None] * len(matched)  # type: ignore[list-item]

        by_play = {_play_id(k.play): k for k in kalshi_rows if k is not None}
        for m in matched:
            km = by_play.get(_play_id(m.play))
            results.append(await _trade_one(m, gw, trade_cfg, already, kalshi=km, kalshi_client=kalshi))
    finally:
        if owns_gw:
            gw.close()
        if owns_kalshi and kalshi is not None:
            close = getattr(kalshi, "close", None)
            if callable(close):
                close()

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
    if league_l == "nfl":
        return [Path(sharp.nfl_path)]
    return [
        Path(sharp.mlb_path),
        Path(sharp.wnba_path),
        Path(sharp.ufc_path),
        Path(sharp.ncaaf_path),
        Path(sharp.nfl_path),
    ]


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
    *,
    kalshi: KalshiMatchedPlay | None = None,
    kalshi_client: Any | None = None,
) -> SharpTradeResult:
    poly_enabled = "polymarket" in trade_cfg.venues
    kalshi_enabled = "kalshi" in trade_cfg.venues
    poly_found = (
        m.status == "matched"
        and m.meta is not None
        and m.token is not None
        and m.slug is not None
    )
    poly_matched = poly_enabled and poly_found
    kalshi_matched = (
        kalshi_enabled
        and kalshi is not None
        and kalshi.status == "matched"
        and bool(kalshi.ticker)
        and bool(kalshi.kalshi_side)
    )
    if not poly_matched and not kalshi_matched:
        detail = m.detail or m.status
        if kalshi is not None and kalshi.detail and not poly_enabled:
            detail = kalshi.detail
        return SharpTradeResult(matched=m, action="skipped", detail=detail)

    canonical = canonical_trade_key(
        m.play,
        away=m.away,
        home=m.home,
        side_team=m.side_team,
        event_date=m.event_date or event_date_for_play(m.play),
    )
    slug_key = (
        poly_slug_key(m.slug, m.token.outcome)
        if m.slug and m.token is not None
        else None
    )
    if canonical in already or (slug_key is not None and slug_key in already):
        return SharpTradeResult(matched=m, action="skipped", detail="already traded (dedupe log)")

    if not _is_pre_game_play(m, trade_cfg.pregame_buffer_minutes):
        return SharpTradeResult(
            matched=m, action="skipped", detail="not pre-game (startTime)"
        )

    quotes: list[PricedVenue] = []
    quote_notes: dict[str, str] = {}
    poly_book: dict[str, Any] | None = None
    poly_ask: float | None = None
    kalshi_ask: float | None = kalshi.ask if kalshi is not None else None

    if poly_matched:
        assert m.token is not None and m.meta is not None
        poly_book = await gw.get_book(m.token.token_id)
        poly_ask = _best_ask(poly_book, m.meta.best_ask)
        usd = _usd_for_play(m.play, trade_cfg, poly_ask)
        skip_reason = _price_gate(
            poly_ask, m.play.implied_fair_prob, trade_cfg, market=m.play.market
        )
        if skip_reason:
            quote_notes["polymarket"] = skip_reason
        elif usd <= 0 or poly_ask is None:
            quote_notes["polymarket"] = "usd size is 0" if usd <= 0 else "no ask"
        else:
            quotes.append(price_venue("polymarket", poly_ask, usd))

    if kalshi_matched:
        assert kalshi is not None and kalshi.ticker and kalshi.kalshi_side
        live_ask = kalshi_ask
        if kalshi_client is not None and not trade_cfg.dry_run:
            live_ask, refresh_err = _refresh_kalshi_ask(
                kalshi_client,
                kalshi.ticker,
                kalshi.kalshi_side,
                kalshi_ask,
                trade_cfg.max_ask_slippage,
            )
            if refresh_err:
                quote_notes["kalshi"] = refresh_err
                live_ask = None
        kalshi_ask = live_ask
        usd_k = _usd_for_play(m.play, trade_cfg, kalshi_ask)
        skip_reason = _price_gate(
            kalshi_ask, m.play.implied_fair_prob, trade_cfg, market=m.play.market
        )
        if skip_reason:
            quote_notes["kalshi"] = skip_reason
        elif usd_k <= 0 or kalshi_ask is None:
            quote_notes["kalshi"] = "usd size is 0" if usd_k <= 0 else "no ask"
        else:
            from ev_trading.fair_value.kalshi_execute import contracts_for_usd

            n = contracts_for_usd(usd_k, kalshi_ask)
            if n < 1:
                quote_notes["kalshi"] = f"cannot size ${usd_k:.2f} at ask {kalshi_ask:.4f}"
            else:
                quotes.append(price_venue("kalshi", kalshi_ask, usd_k, contracts=n))

    chosen = pick_venue(quotes, tie_venue=trade_cfg.tie_venue)  # type: ignore[arg-type]
    if chosen is None:
        detail = " ; ".join(f"{k}: {v}" for k, v in quote_notes.items()) or "no tradable venue"
        return SharpTradeResult(matched=m, action="skipped", detail=detail)

    if chosen.venue == "kalshi" and not trade_cfg.dry_run:
        if kalshi_client is None or not getattr(kalshi_client, "has_auth", False):
            return SharpTradeResult(
                matched=m,
                action="skipped",
                usd=chosen.usd,
                venue="kalshi",
                ask=chosen.ask,
                fee=chosen.fee,
                all_in=chosen.all_in,
                ticker=kalshi.ticker if kalshi else None,
                detail="kalshi cheaper but API keys missing (fail closed)",
            )

    if trade_cfg.dry_run:
        sizing = ""
        if m.play.low_volume_dog_flag:
            base = _usd_for_tier(m.play.tier, trade_cfg)
            sizing = f" (to-win ${base:.0f} on low-volume dog)"
        other = [q for q in quotes if q.venue != chosen.venue]
        other_bit = ""
        if other:
            o = other[0]
            other_bit = f" vs {o.venue} ask={o.ask:.3f} all_in={o.all_in:.4f}"
        outcome = m.token.outcome if m.token is not None else m.play.side
        return SharpTradeResult(
            matched=m,
            action="dry_run",
            usd=chosen.usd,
            venue=chosen.venue,
            ask=chosen.ask,
            fee=chosen.fee,
            all_in=chosen.all_in,
            ticker=kalshi.ticker if chosen.venue == "kalshi" and kalshi else None,
            detail=(
                f"would BUY ${chosen.usd:.2f} on {chosen.venue} {outcome} "
                f"@ ask={chosen.ask:.3f} fee={chosen.fee:.4f} all_in={chosen.all_in:.4f}"
                f"{other_bit}{sizing}"
            ),
            response={
                "book": poly_book,
                "slug": m.slug,
                "quotes": [
                    {"venue": q.venue, "ask": q.ask, "fee": q.fee, "all_in": q.all_in}
                    for q in quotes
                ],
            },
        )

    convex = ConvexTradeClient()
    if not convex.configured:
        return SharpTradeResult(
            matched=m,
            action="skipped",
            usd=chosen.usd,
            venue=chosen.venue,
            ask=chosen.ask,
            fee=chosen.fee,
            all_in=chosen.all_in,
            detail="convex unavailable (fail closed)",
        )

    pred_date = (m.play.game_time_utc or "")[:10] or prediction_date_today()
    outcome = m.token.outcome if m.token is not None else m.play.side
    claimed_keys, claim_err = _claim_event(
        convex,
        canonical=canonical,
        slug_key=slug_key if poly_found else None,
        league=m.play.league,
        matchup=m.play.matchup,
        side=outcome,
        usd=chosen.usd,
        prediction_date=pred_date,
        slug=m.slug,
        condition_id=m.meta.condition_id if m.meta is not None else None,
        payload={
            "tier": m.play.tier,
            "ask": chosen.ask,
            "fee": chosen.fee,
            "all_in": chosen.all_in,
            "venue": chosen.venue,
            "ticker": kalshi.ticker if kalshi else None,
            "kalshi_side": kalshi.kalshi_side if kalshi else None,
            "poly_ask": poly_ask,
            "kalshi_ask": kalshi_ask,
            "low_volume_dog_flag": m.play.low_volume_dog_flag,
            "quotes": [
                {"venue": q.venue, "ask": q.ask, "fee": q.fee, "all_in": q.all_in}
                for q in quotes
            ],
        },
    )
    if claim_err:
        return SharpTradeResult(
            matched=m,
            action="skipped",
            usd=chosen.usd,
            venue=chosen.venue,
            ask=chosen.ask,
            fee=chosen.fee,
            all_in=chosen.all_in,
            detail=claim_err,
        )

    if chosen.venue == "polymarket":
        return await _execute_poly(
            m, gw, chosen, canonical, convex, claimed_keys, already, trade_cfg
        )
    return await _execute_kalshi(
        m,
        kalshi,
        kalshi_client,
        chosen,
        canonical,
        convex,
        claimed_keys,
        already,
        trade_cfg,
    )


def _play_id(play: SharpPlay) -> tuple[str, str, str, str]:
    return (play.league, play.matchup, play.side, play.market)


def _claim_event(
    convex: ConvexTradeClient,
    *,
    canonical: str,
    slug_key: str | None,
    league: str,
    matchup: str,
    side: str,
    usd: float,
    prediction_date: str,
    slug: str | None,
    condition_id: str | None,
    payload: dict[str, Any],
) -> tuple[list[str], str | None]:
    """Claim canonical key, then optional Polymarket slug lock. Release on failure."""
    claim = convex.claim(
        trade_key_value=canonical,
        league=league,
        source="sharp_money",
        matchup=matchup,
        side=side,
        usd=usd,
        prediction_date=prediction_date,
        slug=slug,
        condition_id=condition_id,
        venue=str(payload.get("venue") or ""),
        payload=payload,
    )
    if not claim.claimed:
        return [], claim.detail
    claimed = [canonical]
    if slug_key and slug_key != canonical:
        lock = convex.claim(
            trade_key_value=slug_key,
            league=league,
            source="sharp_money",
            matchup=matchup,
            side=side,
            usd=0.0,
            prediction_date=prediction_date,
            slug=slug,
            condition_id=condition_id,
            venue=str(payload.get("venue") or ""),
            payload={**payload, "lockOnly": True},
        )
        if not lock.claimed:
            convex.release(canonical)
            return [], lock.detail
        claimed.append(slug_key)
    return claimed, None


def _release_keys(convex: ConvexTradeClient, keys: list[str]) -> None:
    for key in keys:
        convex.release(key)


def _refresh_kalshi_ask(
    client: Any,
    ticker: str,
    side: str,
    prior: float | None,
    max_slippage: float,
) -> tuple[float | None, str | None]:
    try:
        live = client.get_market(ticker)
    except Exception as exc:  # noqa: BLE001
        return None, f"quote refresh failed: {exc}"
    from ev_trading.fair_value.tradable_pricer import kalshi_no_ask, kalshi_yes_ask

    ask = kalshi_yes_ask(live) if side == "yes" else kalshi_no_ask(live)
    if ask is None:
        return None, "live ask missing"
    if prior is not None and ask > prior + max_slippage:
        return None, f"ask moved {prior:.2f} → {ask:.2f}"
    return ask, None


async def _execute_poly(
    m: MatchedPlay,
    gw: ExecutionGateway,
    chosen: PricedVenue,
    canonical: str,
    convex: ConvexTradeClient,
    claimed_keys: list[str],
    already: set[str],
    trade_cfg: SharpTradeConfig,
) -> SharpTradeResult:
    assert m.token is not None and m.meta is not None
    try:
        resp = await gw.market_order(m.token.token_id, Side.BUY, chosen.usd, m.meta, fak=True)
    except Exception as exc:  # noqa: BLE001
        _release_keys(convex, claimed_keys)
        return SharpTradeResult(
            matched=m,
            action="failed",
            usd=chosen.usd,
            venue="polymarket",
            ask=chosen.ask,
            fee=chosen.fee,
            all_in=chosen.all_in,
            detail=f"order error: {exc}",
        )

    status = str(resp.get("status", resp.get("error", ""))).lower()
    action = "bought" if "error" not in status and not resp.get("error") else "failed"
    start_iso = m.meta.start_time_iso or m.play.game_time_utc
    fill = enrich_fill(
        {
            "ts": time.time(),
            "key": canonical,
            "venue": "polymarket",
            "slug": m.slug,
            "outcome": m.token.outcome,
            "league": m.play.league,
            "matchup": m.play.matchup,
            "side": m.play.side,
            "tier": m.play.tier,
            "usd": chosen.usd,
            "ask": chosen.ask,
            "fee": chosen.fee,
            "all_in": chosen.all_in,
            "low_volume_dog_flag": m.play.low_volume_dog_flag,
            "resp": resp,
        },
        token_id=m.token.token_id,
        start_time_iso=start_iso,
        ask=chosen.ask,
        resp=resp if isinstance(resp, dict) else None,
    )
    if action == "failed":
        _release_keys(convex, claimed_keys)
        return SharpTradeResult(
            matched=m,
            action="failed",
            usd=chosen.usd,
            venue="polymarket",
            ask=chosen.ask,
            fee=chosen.fee,
            all_in=chosen.all_in,
            detail=status or "sent",
            response=resp if isinstance(resp, dict) else {"raw": resp},
        )
    try:
        convex.complete(canonical, fill)
    except Exception as exc:  # noqa: BLE001
        fill["convex_complete_error"] = str(exc)
    _append_filled(trade_cfg.filled_log, fill)
    already.add(canonical)
    if m.slug and m.token is not None:
        already.add(poly_slug_key(m.slug, m.token.outcome))
    return SharpTradeResult(
        matched=m,
        action=action,
        usd=chosen.usd,
        venue="polymarket",
        ask=chosen.ask,
        fee=chosen.fee,
        all_in=chosen.all_in,
        detail=status or "sent",
        response=resp if isinstance(resp, dict) else {"raw": resp},
    )


async def _execute_kalshi(
    m: MatchedPlay,
    kalshi: KalshiMatchedPlay | None,
    kalshi_client: Any,
    chosen: PricedVenue,
    canonical: str,
    convex: ConvexTradeClient,
    claimed_keys: list[str],
    already: set[str],
    trade_cfg: SharpTradeConfig,
) -> SharpTradeResult:
    from ev_trading.fair_value.kalshi_execute import send_kalshi_ioc

    if kalshi is None or not kalshi.ticker or not kalshi.kalshi_side or kalshi_client is None:
        _release_keys(convex, claimed_keys)
        return SharpTradeResult(
            matched=m,
            action="failed",
            usd=chosen.usd,
            venue="kalshi",
            ask=chosen.ask,
            fee=chosen.fee,
            all_in=chosen.all_in,
            detail="kalshi client/ticker missing",
        )
    try:
        placed = send_kalshi_ioc(
            kalshi_client,
            ticker=kalshi.ticker,
            side=kalshi.kalshi_side,
            ask=chosen.ask,
            usd=chosen.usd,
        )
    except Exception as exc:  # noqa: BLE001
        _release_keys(convex, claimed_keys)
        return SharpTradeResult(
            matched=m,
            action="failed",
            usd=chosen.usd,
            venue="kalshi",
            ask=chosen.ask,
            fee=chosen.fee,
            all_in=chosen.all_in,
            ticker=kalshi.ticker,
            detail=f"order error: {exc}",
        )
    fill_count = float(placed.get("fill_count") or 0)
    resp = placed.get("response") or {}
    if fill_count <= 0:
        _release_keys(convex, claimed_keys)
        return SharpTradeResult(
            matched=m,
            action="skipped",
            usd=chosen.usd,
            venue="kalshi",
            ask=chosen.ask,
            fee=chosen.fee,
            all_in=chosen.all_in,
            ticker=kalshi.ticker,
            detail="IOC no fill",
            response=resp if isinstance(resp, dict) else {"raw": resp},
        )
    start_iso = (m.meta.start_time_iso if m.meta is not None else None) or m.play.game_time_utc
    fill = {
        "ts": time.time(),
        "key": canonical,
        "venue": "kalshi",
        "ticker": kalshi.ticker,
        "kalshi_side": kalshi.kalshi_side,
        "slug": m.slug,
        "outcome": m.token.outcome if m.token is not None else m.play.side,
        "league": m.play.league,
        "matchup": m.play.matchup,
        "side": m.play.side,
        "tier": m.play.tier,
        "usd": placed.get("usd") or chosen.usd,
        "ask": chosen.ask,
        "fee": chosen.fee,
        "all_in": chosen.all_in,
        "contracts": placed.get("contracts"),
        "fill_count": fill_count,
        "client_order_id": placed.get("client_order_id"),
        "low_volume_dog_flag": m.play.low_volume_dog_flag,
        "resp": resp,
        "buy_price": chosen.ask,
        "shares": fill_count,
        "clv_status": "unavailable",
    }
    from polymaker.trading.fill import iso_to_unix_ms

    start_ms = iso_to_unix_ms(start_iso)
    if start_iso:
        fill["start_time"] = start_iso
    if start_ms is not None:
        fill["start_time_ms"] = start_ms
    try:
        convex.complete(
            canonical,
            fill,
            buy_price=chosen.ask,
            shares=fill_count,
            start_time=start_ms,
        )
    except Exception as exc:  # noqa: BLE001
        fill["convex_complete_error"] = str(exc)
    _append_filled(trade_cfg.filled_log, fill)
    already.add(canonical)
    if m.slug and m.token is not None:
        already.add(poly_slug_key(m.slug, m.token.outcome))
    return SharpTradeResult(
        matched=m,
        action="bought",
        usd=float(placed.get("usd") or chosen.usd),
        venue="kalshi",
        ask=chosen.ask,
        fee=chosen.fee,
        all_in=chosen.all_in,
        ticker=kalshi.ticker,
        detail=f"filled {fill_count}",
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


def _american_to_prob(odds: float) -> float | None:
    if odds >= 0:
        p = 100.0 / (odds + 100.0)
    else:
        p = abs(odds) / (abs(odds) + 100.0)
    if p <= 0 or p >= 1:
        return None
    return p


def _dog_price(ask: float | None, play: SharpPlay) -> float | None:
    """Polymarket share price in (0, 1) for to-win sizing."""
    if ask is not None and 0 < ask < 1:
        return float(ask)
    if play.implied_fair_prob is not None and 0 < play.implied_fair_prob < 1:
        return float(play.implied_fair_prob)
    live = play.raw.get("live") if play.raw else None
    try:
        odds = float(live)
    except (TypeError, ValueError):
        return None
    return _american_to_prob(odds)


def stake_to_win(target_profit: float, price: float) -> float:
    """USDC stake so a winning buy at `price` profits `target_profit`.

    Polymarket shares pay $1. At price p, profit = stake × (1 − p) / p,
    so stake = target × p / (1 − p). American +250 is p ≈ 0.286 → $10
    to win $25.
    """
    if target_profit <= 0 or price <= 0 or price >= 1:
        return target_profit
    return target_profit * price / (1.0 - price)


def _usd_for_play(play: SharpPlay, cfg: SharpTradeConfig, ask: float | None) -> float:
    """Tier stake, or to-win that amount when `low_volume_dog_flag` is set."""
    base = _usd_for_tier(play.tier, cfg)
    if not play.low_volume_dog_flag:
        return base
    price = _dog_price(ask, play)
    if price is None:
        return base
    # Never size *up* if the market is shorter than a true dog.
    return round(min(base, stake_to_win(base, price)), 2)


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
    *,
    market: str | None = None,
) -> str | None:
    if cfg.max_ask is not None:
        if ask is None:
            return f"no ask (max_ask {cfg.max_ask})"
        if ask > cfg.max_ask:
            return f"ask {ask:.3f} above max_ask {cfg.max_ask}"
    is_moneyline = (market or "").strip().lower() == "moneyline"
    if is_moneyline and cfg.min_moneyline_ask is not None:
        if ask is None:
            return f"no ask (min_moneyline_ask {cfg.min_moneyline_ask})"
        if ask < cfg.min_moneyline_ask:
            return (
                f"ask {ask:.3f} below min_moneyline_ask {cfg.min_moneyline_ask}"
            )
    if ask is None:
        return None
    if cfg.min_edge is not None and fair is not None and ask > fair - cfg.min_edge:
        # Buy only when market ask is cheaper than our fair by min_edge.
        return f"ask {ask:.3f} not below fair {fair:.3f} - edge {cfg.min_edge}"
    return None


def _fill_key(slug: str, outcome: str) -> str:
    return poly_slug_key(slug, outcome)


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
