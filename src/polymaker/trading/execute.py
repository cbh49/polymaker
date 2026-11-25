"""Place sharp-money buys as resting maker orders on the cheaper of Polymarket and Kalshi.

Every buy is a post-only GTC limit one tick below the best ask, so it never
fills as a taker. Orders are tracked in `resting.py` and reconciled each run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from polymaker.catalog.sports import DEFAULT_PREGAME_BUFFER_MINUTES, is_pre_game, parse_utc
from polymaker.config import Config, KalshiMakerFeeConfig
from polymaker.execution.gateway import ExecutionGateway
from polymaker.trading.convex_trades import ConvexTradeClient, prediction_date_today
from polymaker.trading.event_key import (
    canonical_trade_key,
    event_date_for_play,
    poly_slug_key,
    sibling_side_key,
)
from polymaker.trading.kalshi_match import KalshiMatchedPlay, match_kalshi_plays
from polymaker.trading.maker import (
    MakerQuote,
    assert_below_ask,
    pick_maker_venue,
    price_maker_venue,
)
from polymaker.trading.match import MatchedPlay
from polymaker.trading.resting import RestingBook, RestingOrder, cancel_resting
from polymaker.trading.sharp import SharpPlay, load_sharp_plays, qualifying_trade_data


@dataclass(frozen=True, slots=True)
class SharpTradeConfig:
    """Runtime knobs for sharp auto-buys (overridable via CLI)."""

    usd_tier_a: float = 25.0  # A/A+ stake; NFL totals and NHL are $10; NCAAB totals are $5
    usd_tier_b: float = 10.0
    min_tier: str = "B"  # A = Tier A only; B = A+B
    markets: frozenset[str] = frozenset({"moneyline", "spread", "total"})
    require_rlm: bool = False
    max_ask: float | None = 0.60
    min_moneyline_ask: float | None = 0.35  # skip ML if best ask is cheaper than this
    min_edge: float | None = None  # require ask <= fair - min_edge
    filled_log: str = "journal/sharp_trades.jsonl"
    resting_log: str = "journal/resting_orders.json"
    maker_fills_log: str = "journal/maker_fills.jsonl"
    dry_run: bool = True
    pregame_buffer_minutes: float = DEFAULT_PREGAME_BUFFER_MINUTES
    venues: frozenset[str] = frozenset({"polymarket", "kalshi"})
    tie_venue: str = "polymarket"
    max_ask_slippage: float = 0.01
    # Maker mode: rest `maker_offset` below the best ask; cancel inside the window.
    maker_offset: float = 0.01
    cancel_window_minutes: float = 30.0
    post_only: bool = True
    kalshi_fees: KalshiMakerFeeConfig = field(default_factory=KalshiMakerFeeConfig)


@dataclass(slots=True)
class SharpTradeResult:
    matched: MatchedPlay
    action: str  # resting | dry_run | skipped | failed
    usd: float = 0.0
    detail: str = ""
    response: dict[str, Any] = field(default_factory=dict)
    venue: str | None = None
    ask: float | None = None
    fee: float | None = None  # total maker fee for the order, dollars
    all_in: float | None = None  # effective price per contract: limit + fee / size
    ticker: str | None = None
    limit_price: float | None = None
    size: float | None = None


async def run_sharp_trades(
    matched: list[MatchedPlay],
    cfg: Config,
    trade_cfg: SharpTradeConfig,
    *,
    gateway: ExecutionGateway | None = None,
    kalshi_client: Any | None = None,
    kalshi_matches: list[KalshiMatchedPlay] | None = None,
) -> list[SharpTradeResult]:
    """Rest (or dry-run) a maker buy for each play on the lower effective-price venue."""
    already = _load_filled_keys(trade_cfg.filled_log) | RestingBook(trade_cfg.resting_log).keys()
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
    if league_l == "CBB":
        league_l = "NCAAB"
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
    if league_l == "cbb":
        league_l = "ncaab"
    if league_l == "mlb":
        return [Path(sharp.mlb_path)]
    if league_l == "wnba":
        return [Path(sharp.wnba_path)]
    if league_l == "ufc":
        return [Path(sharp.ufc_path)]
    if league_l == "ncaaf":
        return [Path(sharp.ncaaf_path)]
    if league_l == "ncaab":
        return [Path(sharp.ncaab_path)]
    if league_l == "nfl":
        return [Path(sharp.nfl_path)]
    if league_l == "nba":
        return [Path(sharp.nba_path)]
    if league_l == "nhl":
        return [Path(sharp.nhl_path)]
    return [
        Path(sharp.mlb_path),
        Path(sharp.wnba_path),
        Path(sharp.nba_path),
        Path(sharp.nhl_path),
        Path(sharp.ufc_path),
        Path(sharp.ncaaf_path),
        Path(sharp.ncaab_path),
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
        detail = (m.detail or m.status or "").strip()
        kalshi_detail = (kalshi.detail if kalshi is not None else "").strip()
        if kalshi_detail and not poly_enabled:
            detail = kalshi_detail
        elif (
            kalshi_enabled
            and kalshi_detail
            and detail.lower().startswith("no open ")
            and kalshi_detail.lower() != detail.lower()
        ):
            # Polymarket often has no NCAAB spread or total. Keep Kalshi's reason
            # when that is why the order did not go out.
            detail = f"{detail}; {kalshi_detail}"
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
    sibling = sibling_side_key(canonical)
    if sibling is not None and sibling in already:
        return SharpTradeResult(
            matched=m,
            action="skipped",
            detail="already traded this team (moneyline/spread)",
        )

    window = max(trade_cfg.pregame_buffer_minutes, trade_cfg.cancel_window_minutes)
    if not _is_pre_game_play(m, window, kalshi=kalshi):
        return SharpTradeResult(
            matched=m,
            action="skipped",
            detail=f"not pre-game or inside {window:g}-min maker window (startTime)",
        )

    quotes: list[MakerQuote] = []
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
            poly_ask,
            m.play.implied_fair_prob,
            trade_cfg,
            market=m.play.market,
            league=m.play.league,
        )
        if skip_reason:
            quote_notes["polymarket"] = skip_reason
        elif usd <= 0 or poly_ask is None:
            quote_notes["polymarket"] = "usd size is 0" if usd <= 0 else "no ask"
        else:
            quote, why = price_maker_venue(
                "polymarket",
                poly_ask,
                usd,
                tick=m.meta.tick_size or 0.01,
                offset=trade_cfg.maker_offset,
                min_size=m.meta.min_order_size,
            )
            if quote is None:
                quote_notes["polymarket"] = why or "no maker quote"
            else:
                quotes.append(quote)

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
            kalshi_ask,
            m.play.implied_fair_prob,
            trade_cfg,
            market=m.play.market,
            league=m.play.league,
        )
        if skip_reason:
            quote_notes["kalshi"] = skip_reason
        elif usd_k <= 0 or kalshi_ask is None:
            quote_notes["kalshi"] = "usd size is 0" if usd_k <= 0 else "no ask"
        else:
            quote, why = price_maker_venue(
                "kalshi",
                kalshi_ask,
                usd_k,
                offset=trade_cfg.maker_offset,
                ticker=kalshi.ticker,
                kalshi_fees=trade_cfg.kalshi_fees,
            )
            if quote is None:
                quote_notes["kalshi"] = why or "no maker quote"
            else:
                quotes.append(quote)

    chosen = pick_maker_venue(quotes, tie_venue=trade_cfg.tie_venue)  # type: ignore[arg-type]
    if chosen is None:
        detail = " ; ".join(f"{k}: {v}" for k, v in quote_notes.items()) or "no tradable venue"
        return SharpTradeResult(matched=m, action="skipped", detail=detail)

    if chosen.venue == "kalshi" and not trade_cfg.dry_run:
        if kalshi_client is None or not getattr(kalshi_client, "has_auth", False):
            return _result(
                m,
                "skipped",
                chosen,
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
            other_bit = (
                f" vs {o.venue} limit={o.limit_price:.3f} fee={o.fee:.2f} "
                f"effective={o.effective_price:.4f}"
            )
        outcome = m.token.outcome if m.token is not None else m.play.side
        return _result(
            m,
            "dry_run",
            chosen,
            ticker=kalshi.ticker if chosen.venue == "kalshi" and kalshi else None,
            detail=(
                f"would REST BUY ${chosen.usd:.2f} on {chosen.venue} {outcome} "
                f"{chosen.size:g} @ limit={chosen.limit_price:.3f} (ask={chosen.ask:.3f}, "
                f"post-only) fee={chosen.fee:.2f} effective={chosen.effective_price:.4f}"
                f"{other_bit}{sizing}"
            ),
            response={
                "book": poly_book,
                "slug": m.slug,
                "quotes": [_quote_dict(q) for q in quotes],
            },
        )

    convex = ConvexTradeClient()
    if not convex.configured:
        return _result(m, "skipped", chosen, detail="convex unavailable (fail closed)")

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
        trade_data=qualifying_trade_data(m.play),
        payload={
            "tier": m.play.tier,
            "ask": chosen.ask,
            "limit_price": chosen.limit_price,
            "size": chosen.size,
            "fee": chosen.fee,
            "all_in": chosen.effective_price,
            "maker": True,
            "venue": chosen.venue,
            "ticker": kalshi.ticker if kalshi else None,
            "kalshi_side": kalshi.kalshi_side if kalshi else None,
            "poly_ask": poly_ask,
            "kalshi_ask": kalshi_ask,
            "low_volume_dog_flag": m.play.low_volume_dog_flag,
            "quotes": [_quote_dict(q) for q in quotes],
        },
    )
    if claim_err:
        return _result(m, "skipped", chosen, detail=claim_err)

    start_iso = _play_start_iso(m, kalshi)
    if chosen.venue == "polymarket":
        return await _execute_poly(
            m,
            gw,
            chosen,
            canonical,
            slug_key if poly_found else None,
            convex,
            claimed_keys,
            already,
            trade_cfg,
            start_iso,
        )
    return await _execute_kalshi(
        m,
        kalshi,
        kalshi_client,
        chosen,
        canonical,
        slug_key if poly_found else None,
        convex,
        claimed_keys,
        already,
        trade_cfg,
        start_iso,
    )


def _quote_dict(q: MakerQuote) -> dict[str, Any]:
    return {
        "venue": q.venue,
        "ask": q.ask,
        "limit_price": q.limit_price,
        "size": q.size,
        "fee": q.fee,
        "effective_price": q.effective_price,
    }


def _result(
    m: MatchedPlay,
    action: str,
    q: MakerQuote,
    *,
    detail: str = "",
    ticker: str | None = None,
    response: dict[str, Any] | None = None,
) -> SharpTradeResult:
    return SharpTradeResult(
        matched=m,
        action=action,
        usd=q.usd,
        detail=detail,
        response=response or {},
        venue=q.venue,
        ask=q.ask,
        fee=q.fee,
        all_in=q.effective_price,
        ticker=ticker,
        limit_price=q.limit_price,
        size=q.size,
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
    trade_data: dict[str, Any] | None = None,
) -> tuple[list[str], str | None]:
    """Claim the market key, the other of moneyline/spread, then the slug lock.

    Release every key already claimed if a later lock fails. Totals have no
    sibling lock.
    """
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
        trade_data=trade_data,
    )
    if not claim.claimed:
        return [], claim.detail
    claimed = [canonical]
    sibling = sibling_side_key(canonical)
    if sibling and sibling not in claimed:
        # Hold the other of moneyline/spread so a later signal on this team
        # cannot buy it. Totals are a different key and are not locked.
        lock = convex.claim(
            trade_key_value=sibling,
            league=league,
            source="sharp_money",
            matchup=matchup,
            side=side,
            usd=0.0,
            prediction_date=prediction_date,
            slug=slug,
            condition_id=condition_id,
            venue=str(payload.get("venue") or ""),
            payload={**payload, "lockOnly": True, "sideLock": True},
        )
        if not lock.claimed:
            _release_keys(convex, claimed)
            return [], lock.detail
        claimed.append(sibling)
    if slug_key and slug_key not in claimed:
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
            _release_keys(convex, claimed)
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
    chosen: MakerQuote,
    canonical: str,
    slug_key: str | None,
    convex: ConvexTradeClient,
    claimed_keys: list[str],
    already: set[str],
    trade_cfg: SharpTradeConfig,
    start_iso: str | None,
) -> SharpTradeResult:
    """Rest a post-only GTC limit buy. Fills are picked up by the next reconcile."""
    assert m.token is not None and m.meta is not None
    try:
        assert_below_ask(chosen.limit_price, chosen.ask)
        placed = await gw.place_limit_buy(
            m.token.token_id,
            chosen.limit_price,
            chosen.size,
            m.meta,
            post_only=trade_cfg.post_only,
        )
    except Exception as exc:  # noqa: BLE001
        _release_keys(convex, claimed_keys)
        return _result(m, "failed", chosen, detail=f"order error: {exc}")

    order_id = placed.get("order_id")
    error = placed.get("error")
    if error or not order_id:
        _release_keys(convex, claimed_keys)
        text = str(error or "no order id")
        crossed = any(w in text.lower() for w in ("cross", "post only", "post-only", "taker"))
        return _result(
            m,
            "skipped" if crossed else "failed",
            chosen,
            detail=(f"post-only rejected (would cross): {text}" if crossed else f"order error: {text}"),
            response=placed.get("raw") or {},
        )

    rec = RestingOrder(
        venue="polymarket",
        order_id=str(order_id),
        canonical=canonical,
        limit_price=chosen.limit_price,
        size=chosen.size,
        claimed_keys=list(claimed_keys),
        slug_key=slug_key,
        league=m.play.league,
        matchup=m.play.matchup,
        side=m.token.outcome,
        tier=m.play.tier,
        usd=chosen.usd,
        ask=chosen.ask,
        start_time=start_iso,
        token_id=m.token.token_id,
        slug=m.slug,
        low_volume_dog_flag=m.play.low_volume_dog_flag,
    )
    return await _record_resting(m, chosen, rec, gw, None, convex, claimed_keys, already, trade_cfg, placed)


async def _execute_kalshi(
    m: MatchedPlay,
    kalshi: KalshiMatchedPlay | None,
    kalshi_client: Any,
    chosen: MakerQuote,
    canonical: str,
    slug_key: str | None,
    convex: ConvexTradeClient,
    claimed_keys: list[str],
    already: set[str],
    trade_cfg: SharpTradeConfig,
    start_iso: str | None,
) -> SharpTradeResult:
    from ev_trading.fair_value.kalshi_execute import send_kalshi_maker

    if kalshi is None or not kalshi.ticker or not kalshi.kalshi_side or kalshi_client is None:
        _release_keys(convex, claimed_keys)
        return _result(m, "failed", chosen, detail="kalshi client/ticker missing")
    try:
        assert_below_ask(chosen.limit_price, chosen.ask)
        placed = send_kalshi_maker(
            kalshi_client,
            ticker=kalshi.ticker,
            side=kalshi.kalshi_side,
            limit_price=chosen.limit_price,
            contracts=int(chosen.size),
            post_only=trade_cfg.post_only,
        )
    except Exception as exc:  # noqa: BLE001
        _release_keys(convex, claimed_keys)
        text = str(exc)
        crossed = any(w in text.lower() for w in ("post_only", "post-only", "cross"))
        return _result(
            m,
            "skipped" if crossed else "failed",
            chosen,
            ticker=kalshi.ticker,
            detail=(f"post-only rejected (would cross): {text}" if crossed else f"order error: {text}"),
        )
    order_id = placed.get("order_id")
    if not order_id:
        _release_keys(convex, claimed_keys)
        return _result(
            m,
            "failed",
            chosen,
            ticker=kalshi.ticker,
            detail="kalshi accepted the order without an order_id",
            response=placed.get("response") or {},
        )

    rec = RestingOrder(
        venue="kalshi",
        order_id=str(order_id),
        canonical=canonical,
        limit_price=chosen.limit_price,
        size=chosen.size,
        claimed_keys=list(claimed_keys),
        slug_key=slug_key,
        league=m.play.league,
        matchup=m.play.matchup,
        side=m.token.outcome if m.token is not None else m.play.side,
        tier=m.play.tier,
        usd=chosen.usd,
        ask=chosen.ask,
        start_time=start_iso,
        slug=m.slug,
        ticker=kalshi.ticker,
        kalshi_side=kalshi.kalshi_side,
        client_order_id=placed.get("client_order_id"),
        low_volume_dog_flag=m.play.low_volume_dog_flag,
    )
    return await _record_resting(
        m, chosen, rec, None, kalshi_client, convex, claimed_keys, already, trade_cfg, placed
    )


async def _record_resting(
    m: MatchedPlay,
    chosen: MakerQuote,
    rec: RestingOrder,
    gw: ExecutionGateway | None,
    kalshi_client: Any | None,
    convex: ConvexTradeClient,
    claimed_keys: list[str],
    already: set[str],
    trade_cfg: SharpTradeConfig,
    placed: dict[str, Any],
) -> SharpTradeResult:
    """Persist a live order so the next run can fill-check or cancel it."""
    try:
        RestingBook(trade_cfg.resting_log).add(rec)
    except Exception as exc:  # noqa: BLE001
        # An order we cannot track must not stay live: cancel it and free the claim.
        ok, _reason, _ = await cancel_resting(rec, gw, kalshi_client)
        if ok:
            _release_keys(convex, claimed_keys)
        return _result(
            m,
            "failed",
            chosen,
            ticker=rec.ticker,
            detail=(
                f"could not save resting order ({exc}); "
                + ("cancelled" if ok else f"CANCEL FAILED, order {rec.order_id} may be live")
            ),
        )
    _remember_trade(already, rec.canonical, rec.slug_key)
    print(
        f"[resting] placed {rec.venue} {rec.matchup} {rec.side} {rec.size:g} @ "
        f"{rec.limit_price:.3f} (ask {rec.ask:.3f}, post-only={trade_cfg.post_only}) "
        f"fee={chosen.fee:.2f} effective={chosen.effective_price:.4f} order={rec.order_id[:12]}",
        flush=True,
    )
    raw = placed.get("raw") if rec.venue == "polymarket" else placed.get("response")
    return _result(
        m,
        "resting",
        chosen,
        ticker=rec.ticker,
        detail=f"resting {rec.size:g} @ {rec.limit_price:.3f} (ask {rec.ask:.3f}) order={rec.order_id}",
        response=raw if isinstance(raw, dict) else {"raw": raw},
    )


def _play_start_iso(m: MatchedPlay, kalshi: KalshiMatchedPlay | None = None) -> str | None:
    """First parseable game start: Polymarket startTime, sportsbook clock, Kalshi occurrence."""
    starts: list[str] = []
    if m.meta is not None and m.meta.start_time_iso:
        starts.append(m.meta.start_time_iso)
    if m.play.game_time_utc:
        starts.append(str(m.play.game_time_utc))
    if kalshi is not None and kalshi.start_time:
        starts.append(kalshi.start_time)
    for start in starts:
        if parse_utc(start) is not None:
            return start
    return None


def _is_pre_game_play(
    m: MatchedPlay,
    buffer_minutes: float,
    kalshi: KalshiMatchedPlay | None = None,
) -> bool:
    """Final startTime gate. Does not trust event.live / gameStatus.

    A matched Polymarket market uses Gamma startTime. NCAAB spreads and totals
    often have no Polymarket line, and the DraftKings slate has no
    game_time_utc, so a Kalshi-only order uses that contract's
    occurrence_datetime. Missing every clock still fails closed.
    """
    start = _play_start_iso(m, kalshi)
    if start is None:
        return False
    return is_pre_game({"startTime": start}, buffer_minutes)


# NFL game totals (Over/Under) stake this much even at Tier A/A+.
# NCAAF totals use the normal tier stake ($25 at A/A+).
NFL_TOTAL_USD = 10.0
# NCAAB game totals stake this much at every tier.
NCAAB_TOTAL_USD = 5.0
# NHL stakes are a flat $10 for every tier and market.
NHL_USD = 10.0
# NHL moneyline skips below this. 0.42 still trades.
NHL_MIN_MONEYLINE_ASK = 0.42
# NHL spread skips below this. 0.45 still trades.
NHL_MIN_SPREAD_ASK = 0.45
# NHL spread also skips above this. 0.58 still trades.
NHL_MAX_SPREAD_ASK = 0.58


def _normalized_league(league: str | None) -> str:
    key = str(league or "").strip().upper()
    if key == "CFB":
        return "NCAAF"
    if key == "CBB":
        return "NCAAB"
    return key


def _usd_for_tier(tier: str, cfg: SharpTradeConfig) -> float:
    if tier.upper() in {"A+", "A"}:
        return float(cfg.usd_tier_a)
    return float(cfg.usd_tier_b)


def _nfl_total_cap(play: SharpPlay, base: float) -> float:
    """A/A+ Over/Under in the NFL stakes $10. NCAAF uses the tier stake."""
    if str(play.market or "").strip().lower() != "total":
        return base
    if _normalized_league(play.league) != "NFL":
        return base
    if str(play.tier or "").strip().upper() not in {"A+", "A"}:
        return base
    return min(base, NFL_TOTAL_USD)


def _ncaab_total_stake(play: SharpPlay, base: float) -> float:
    """NCAAB Over/Under stakes $5 at every tier."""
    if str(play.market or "").strip().lower() != "total":
        return base
    if _normalized_league(play.league) != "NCAAB":
        return base
    return NCAAB_TOTAL_USD


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


def _nhl_stake(play: SharpPlay) -> float | None:
    """Flat $10 NHL stake. None for every other league."""
    if _normalized_league(play.league) != "NHL":
        return None
    return NHL_USD


def _usd_for_play(play: SharpPlay, cfg: SharpTradeConfig, ask: float | None) -> float:
    """Tier stake, or to-win that amount when `low_volume_dog_flag` is set.

    NFL totals cap Tier A/A+ at $10. NCAAF totals use the tier stake.
    NCAAB totals are a flat $5 at every tier. NHL is a flat $10 on every
    market and tier, with no long-dog to-win shrink. Other leagues keep the
    configured tier stake.
    """
    nhl = _nhl_stake(play)
    if nhl is not None:
        return nhl
    base = _ncaab_total_stake(play, _nfl_total_cap(play, _usd_for_tier(play.tier, cfg)))
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
    league: str | None = None,
) -> str | None:
    if cfg.max_ask is not None:
        if ask is None:
            return f"no ask (max_ask {cfg.max_ask})"
        if ask > cfg.max_ask:
            return f"ask {ask:.3f} above max_ask {cfg.max_ask}"
    kind = (market or "").strip().lower()
    is_moneyline = kind == "moneyline"
    if is_moneyline and cfg.min_moneyline_ask is not None:
        if ask is None:
            return f"no ask (min_moneyline_ask {cfg.min_moneyline_ask})"
        if ask < cfg.min_moneyline_ask:
            return (
                f"ask {ask:.3f} below min_moneyline_ask {cfg.min_moneyline_ask}"
            )
    if _normalized_league(league) == "NHL":
        if kind == "moneyline" and ask is not None and ask < NHL_MIN_MONEYLINE_ASK:
            return f"ask {ask:.3f} below nhl min moneyline {NHL_MIN_MONEYLINE_ASK}"
        if kind == "spread" and ask is not None and ask < NHL_MIN_SPREAD_ASK:
            return f"ask {ask:.3f} below nhl min spread {NHL_MIN_SPREAD_ASK}"
        if kind == "spread" and ask is not None and ask > NHL_MAX_SPREAD_ASK:
            return f"ask {ask:.3f} above nhl max spread {NHL_MAX_SPREAD_ASK}"
    if ask is None:
        return None
    if cfg.min_edge is not None and fair is not None and ask > fair - cfg.min_edge:
        # Buy only when market ask is cheaper than our fair by min_edge.
        return f"ask {ask:.3f} not below fair {fair:.3f} - edge {cfg.min_edge}"
    return None


def _fill_key(slug: str, outcome: str) -> str:
    return poly_slug_key(slug, outcome)


def _remember_trade(already: set[str], canonical: str, slug_key: str | None = None) -> None:
    """Record a fill so this run will not buy it, or the other side market, again."""
    already.add(canonical)
    sibling = sibling_side_key(canonical)
    if sibling:
        already.add(sibling)
    if slug_key:
        already.add(slug_key)


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
            sibling = sibling_side_key(key)
            if sibling:
                keys.add(sibling)
    return keys


def _append_filled(path: str | Path, payload: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, default=str) + "\n")
