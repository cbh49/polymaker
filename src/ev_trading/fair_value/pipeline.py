"""Walk aggregated NFL odds JSON and emit a tradable-first fair-value report."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from ev_trading.fair_value.confidence import confidence_score, is_low_liquidity
from ev_trading.fair_value.config import FairValueConfig
from ev_trading.fair_value.consensus import (
    extrapolation_for,
    fair_over_from_fit,
    fit_distribution,
    lines_are_same_strike,
    same_strike_consensus,
    strike_range,
)
from ev_trading.fair_value.devig import devig_one_sided, devig_two_way, prob_to_american
from ev_trading.fair_value.models import (
    BookPoint,
    FittedDistribution,
    InformationalRow,
    PricedOpportunity,
    SameStrikeConsensus,
)
from ev_trading.fair_value.report import FairValueReport, build_report
from ev_trading.fair_value.tradable_pricer import (
    kalshi_no_ask,
    kalshi_yes_ask,
    opportunity,
    polymarket_side_ask,
)

_TWO_WAY_STATS = frozenset(
    {
        "spread",
        "total",
        "passing_yards",
        "rushing_yards",
        "receiving_yards",
        "rushing_receiving_yards",
        "receptions",
        "interceptions",
        "passing_tds",
        "rushing_tds",
        "receiving_tds",
    }
)


def _f(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _american(value: Any) -> float | None:
    """Keep posted American odds; ignore implied probabilities in (0, 1]."""
    num = _f(value)
    if num is None or abs(num) <= 1.0:
        return None
    return num


def _depth(raw: dict[str, Any] | None) -> tuple[float | None, float | None, float | None]:
    if not isinstance(raw, dict):
        return None, None, None
    vol = _f(raw.get("volume"))
    liq = _f(raw.get("liquidity"))
    v24 = _f(raw.get("volume_24hr"))
    for key in ("home", "away", "over", "under"):
        side = raw.get(key)
        if not isinstance(side, dict):
            continue
        vol = vol if vol is not None else _f(side.get("volume"))
        liq = liq if liq is not None else _f(side.get("liquidity"))
        v24 = v24 if v24 is not None else _f(side.get("volume_24hr"))
    return vol, liq, v24


def _books(blob: dict[str, Any]) -> dict[str, Any]:
    raw = blob.get("books")
    return raw if isinstance(raw, dict) else {}


def _obj(blob: dict[str, Any], key: str) -> dict[str, Any] | None:
    raw = blob.get(key)
    return raw if isinstance(raw, dict) else None


def _make_opp(kwargs: Any) -> PricedOpportunity:
    return opportunity(**kwargs)


def _matchup_label(game: dict[str, Any]) -> str:
    matchup = game.get("matchup")
    if isinstance(matchup, str) and matchup.strip():
        return matchup.strip()
    away = (game.get("away") or {}).get("abbr") or (game.get("away") or {}).get("team")
    home = (game.get("home") or {}).get("abbr") or (game.get("home") or {}).get("team")
    if away and home:
        return f"{away} @ {home}"
    return "unknown"


def _ou_points(books: dict[str, Any], cfg: FairValueConfig) -> list[BookPoint]:
    points: list[BookPoint] = []
    for book, entry in books.items():
        if not isinstance(entry, dict):
            continue
        line = _f(entry.get("line"))
        if line is None:
            continue
        over_raw = entry.get("over_odds", entry.get("over_implied_prob"))
        under_raw = entry.get("under_odds", entry.get("under_implied_prob"))
        over_p = _f(over_raw)
        under_p = _f(under_raw)
        if over_p is None or under_p is None:
            continue
        fair_over, _fair_under = devig_two_way(over_p, under_p, method=cfg.devig_method)
        points.append(
            BookPoint(
                book=str(book),
                line=line,
                fair_over=fair_over,
                weight=cfg.book_weight(str(book)),
                raw_over=_f(entry.get("over_implied_prob")) or over_p,
                raw_under=_f(entry.get("under_implied_prob")) or under_p,
                over_odds=_american(entry.get("over_odds")),
                under_odds=_american(entry.get("under_odds")),
            )
        )
    return points


def _spread_points(books: dict[str, Any], cfg: FairValueConfig) -> list[BookPoint]:
    """line = home spread; fair_over = P(home covers)."""
    points: list[BookPoint] = []
    for book, entry in books.items():
        if not isinstance(entry, dict):
            continue
        home = entry.get("home") if isinstance(entry.get("home"), dict) else None
        away = entry.get("away") if isinstance(entry.get("away"), dict) else None
        if not home or not away:
            continue
        home_line = _f(home.get("line"))
        if home_line is None:
            continue
        home_raw = home.get("odds", home.get("implied_prob"))
        away_raw = away.get("odds", away.get("implied_prob"))
        hp = _f(home_raw)
        ap = _f(away_raw)
        if hp is None or ap is None:
            continue
        fair_home, _fair_away = devig_two_way(hp, ap, method=cfg.devig_method)
        points.append(
            BookPoint(
                book=str(book),
                line=home_line,
                fair_over=fair_home,
                weight=cfg.book_weight(str(book)),
                raw_over=_f(home.get("implied_prob")) or hp,
                raw_under=_f(away.get("implied_prob")) or ap,
                over_odds=_american(home.get("odds")),
                under_odds=_american(away.get("odds")),
            )
        )
    return points


def _ml_points(books: dict[str, Any], cfg: FairValueConfig) -> list[BookPoint]:
    """line unused (0); fair_over = P(home wins)."""
    points: list[BookPoint] = []
    for book, entry in books.items():
        if not isinstance(entry, dict):
            continue
        home = entry.get("home") if isinstance(entry.get("home"), dict) else None
        away = entry.get("away") if isinstance(entry.get("away"), dict) else None
        if not home or not away:
            continue
        hp = _f(home.get("odds", home.get("implied_prob")))
        ap = _f(away.get("odds", away.get("implied_prob")))
        if hp is None or ap is None:
            continue
        fair_home, _ = devig_two_way(hp, ap, method=cfg.devig_method)
        points.append(
            BookPoint(
                book=str(book),
                line=0.0,
                fair_over=fair_home,
                weight=cfg.book_weight(str(book)),
                raw_over=_f(home.get("implied_prob")) or hp,
                raw_under=_f(away.get("implied_prob")) or ap,
                over_odds=_american(home.get("odds")),
                under_odds=_american(away.get("odds")),
            )
        )
    return points


def _yes_points(books: dict[str, Any], cfg: FairValueConfig) -> list[BookPoint]:
    points: list[BookPoint] = []
    for book, entry in books.items():
        if not isinstance(entry, dict):
            continue
        raw = entry.get("odds", entry.get("implied_prob"))
        p = _f(raw)
        if p is None:
            continue
        fair = devig_one_sided(p, overround=cfg.one_sided_overround)
        points.append(
            BookPoint(
                book=str(book),
                line=0.0,
                fair_over=fair,
                weight=cfg.book_weight(str(book)),
                raw_over=_f(entry.get("implied_prob")) or p,
                over_odds=_american(entry.get("odds")),
            )
        )
    return points


def _threshold_points(points: list[BookPoint]) -> list[BookPoint]:
    """Home spread L → margin threshold −L so P(M > −L) = P(home covers L)."""
    return [
        BookPoint(
            book=p.book,
            line=-p.line,
            fair_over=p.fair_over,
            weight=p.weight,
            raw_over=p.raw_over,
            raw_under=p.raw_under,
            over_odds=p.over_odds,
            under_odds=p.under_odds,
        )
        for p in points
    ]


def _allow_ou_side(
    side: str,
    *,
    market_line: float | None,
    fair_line: float | None,
    fit: FittedDistribution | None,
    cfg: FairValueConfig,
) -> bool:
    """Whether this O/U side is a line we should even try to price.

    Over 9.5 vs a 0.5 sportsbook main is a *worse* number, not a better one.
    When σ is unidentified we cannot turn that long-shot into a +EV over;
    only the favorable side (under 9.5) is comparable, and only if cheap.
    Nearby alts (213 vs 224.5 passing) still price both sides.
    """
    if side not in {"over", "under"}:
        return True
    if market_line is None or fair_line is None:
        return True
    delta = market_line - fair_line
    if abs(delta) <= cfg.same_strike_line_tolerance:
        return True
    unfavorable = (side == "over" and delta > 0) or (side == "under" and delta < 0)
    if not unfavorable:
        return True
    if fit is not None and fit.sigma_source == "fitted":
        return True
    rel = abs(delta) / max(abs(fair_line), 1.0)
    return rel <= cfg.unfitted_max_rel_delta


def different_strike_veto(
    *,
    n_books: int,
    fit: FittedDistribution | None,
    market_line: float | None,
    points: list[BookPoint],
    cfg: FairValueConfig,
) -> str | None:
    """Hard-exclude untrustworthy different-strike rows.

    Same-strike (``fit is None``) is never vetoed. ``sigma_source == fitted``
    is not the same claim as "trustworthy at this exact strike" — walking a
    curve past the observed book range is a separate failure mode.
    """
    if fit is None:
        return None
    if n_books < cfg.min_books_hard_floor:
        return "insufficient_books"
    is_extra, extra_dist = extrapolation_for(market_line, points)
    rng = strike_range(points)
    book_min = rng[0] if rng else None
    rel = 0.0
    if is_extra:
        rel = extra_dist / max(abs(book_min) if book_min is not None else 1.0, 1.0)
    r2 = fit.r2
    if r2 is not None and r2 < cfg.min_r2_for_fitted_sigma:
        if fit.sigma_source == "fitted":
            return "fit_quality_too_low"
        # Degenerate linreg (r2≈0) labeled fallback is still a silent fit
        # failure when we walk well past the quoted book range (Chase 50+).
        if is_extra and rel > cfg.max_fallback_extrapolation_ratio:
            return "fit_quality_too_low"
    if is_extra and rel > cfg.max_fallback_extrapolation_ratio:
        return "fallback_sigma_extrapolation"
    return None


def _strike_meta(
    *,
    fit: FittedDistribution | None,
    points: list[BookPoint] | None,
    market_line: float | None,
) -> dict[str, Any]:
    if fit is None or not points:
        return {
            "is_extrapolation": None,
            "extrapolation_distance": None,
            "sigma_source": None,
            "role_bucket": None,
        }
    is_extra, extra_dist = extrapolation_for(market_line, points)
    return {
        "is_extrapolation": is_extra,
        "extrapolation_distance": extra_dist,
        "sigma_source": fit.sigma_source,
        "role_bucket": fit.role_bucket,
    }


def _route_opp(
    opp: PricedOpportunity,
    *,
    tradable: list[PricedOpportunity],
    rejected: list[PricedOpportunity],
    fit: FittedDistribution | None,
    points: list[BookPoint] | None,
    cfg: FairValueConfig,
) -> None:
    reason = different_strike_veto(
        n_books=opp.n_books,
        fit=fit,
        market_line=opp.market_line,
        points=points or [],
        cfg=cfg,
    )
    if reason:
        rejected.append(replace(opp, veto_reason=reason, rank_score=0.0))
        return
    tradable.append(opp)


def _fair_at_line(
    points: list[BookPoint],
    *,
    stat: str,
    target_line: float | None,
    cfg: FairValueConfig,
) -> tuple[float, FittedDistribution | None, SameStrikeConsensus | None]:
    dist = cfg.distribution_for(stat)
    weights = [p.weight for p in points]
    lines = [p.line for p in points]
    common = lines_are_same_strike(lines, cfg.same_strike_line_tolerance)
    mean_line = (
        sum(line * weight for line, weight in zip(lines, weights, strict=True)) / sum(weights)
        if points
        else None
    )
    target_matches = (
        target_line is None
        or mean_line is None
        or abs(mean_line - target_line) <= cfg.same_strike_line_tolerance
    )
    force_same = dist == "same_strike" or (common and target_matches)
    if force_same:
        cons = same_strike_consensus(points, cfg=cfg)
        return cons.fair_prob, None, cons
    kind = dist if dist in {"normal", "poisson", "nbinom"} else "normal"
    fit = fit_distribution(points, kind, stat=stat, cfg=cfg)
    line = fit.mu if target_line is None else target_line
    return fair_over_from_fit(fit, line), fit, None


def _conf(
    *,
    n_books: int,
    fit: FittedDistribution | None,
    cons: SameStrikeConsensus | None,
    volume: float | None,
    liquidity: float | None,
    volume_24hr: float | None,
    line_delta: float | None,
    cfg: FairValueConfig,
    extrapolation_distance: float | None = None,
    book_line_min: float | None = None,
) -> tuple[float, bool]:
    r2 = fit.r2 if fit is not None else None
    std = cons.std if cons is not None else None
    low = bool(
        (fit is not None and fit.low_confidence) or (cons is not None and cons.low_confidence)
    )
    score = confidence_score(
        n_books=n_books,
        r2=r2,
        agreement_std=std,
        volume=volume,
        liquidity=liquidity,
        volume_24hr=volume_24hr,
        line_delta=line_delta,
        extrapolation_distance=extrapolation_distance,
        book_line_min=book_line_min,
        cfg=cfg,
    )
    return score, low


def _append_venue_info(
    informational: list[InformationalRow] | None,
    *,
    market: str,
    matchup: str,
    player: str | None,
    stat: str,
    book: str,
    line: float | None,
    ask: float | None,
    fair: float,
    n_books: int,
    side: str | None,
) -> None:
    """Sportsbook-style +EV row for Kalshi/Polymarket (fair minus ask)."""
    if informational is None or not side or ask is None:
        return
    if ask <= 0.0 or ask >= 1.0:
        return
    informational.append(
        InformationalRow(
            market=market,
            matchup=matchup,
            player=player,
            stat=stat,
            book=book,
            book_line=line,
            book_prob=ask,
            fair_prob=fair,
            raw_edge=fair - ask,
            n_books=n_books,
            side=side,
            book_odds=float(prob_to_american(ask)),
        )
    )


def _emit_binary_venue(
    *,
    tradable: list[PricedOpportunity],
    rejected: list[PricedOpportunity],
    market: str,
    matchup: str,
    stat: str,
    player: str | None,
    fair_yes: float,
    fair_line: float | None,
    venue_line: float | None,
    line_delta: float | None,
    n_books: int,
    fit: FittedDistribution | None,
    cons: SameStrikeConsensus | None,
    cfg: FairValueConfig,
    kalshi: dict[str, Any] | None,
    polymarket: dict[str, Any] | None,
    yes_side: str = "yes",
    no_side: str = "no",
    points: list[BookPoint] | None = None,
    informational: list[InformationalRow] | None = None,
    info_yes_side: str | None = None,
    info_no_side: str | None = None,
) -> None:
    if info_yes_side is None and info_no_side is None:
        alert_yes, alert_no = yes_side, no_side
    else:
        alert_yes, alert_no = info_yes_side, info_no_side
    if isinstance(kalshi, dict) and "yes_ask" not in kalshi and "yes_bid" not in kalshi:
        # Nested home/away moneyline handled elsewhere.
        kalshi = None
    rng = strike_range(points or [])
    book_min = rng[0] if rng else None

    def _emit_side(common: dict[str, Any], side: str, fair: float, price: float) -> None:
        opp = _make_opp({**common, "side": side, "fair_prob": fair, "market_price": price})
        _route_opp(
            opp,
            tradable=tradable,
            rejected=rejected,
            fit=fit,
            points=points,
            cfg=cfg,
        )

    if isinstance(kalshi, dict):
        k_line = venue_line if venue_line is not None else _f(kalshi.get("line"))
        meta = _strike_meta(fit=fit, points=points, market_line=k_line)
        vol, liq, v24 = _depth(kalshi)
        conf, low_c = _conf(
            n_books=n_books,
            fit=fit,
            cons=cons,
            volume=vol,
            liquidity=liq,
            volume_24hr=v24,
            line_delta=line_delta,
            cfg=cfg,
            extrapolation_distance=meta["extrapolation_distance"],
            book_line_min=book_min,
        )
        thin = is_low_liquidity(volume=vol, liquidity=liq, volume_24hr=v24, cfg=cfg)
        yes_ask = kalshi_yes_ask(kalshi)
        no_ask = kalshi_no_ask(kalshi)
        common: dict[str, Any] = dict(
            market=market,
            matchup=matchup,
            stat=stat,
            player=player,
            fair_line=fair_line,
            market_line=k_line,
            volume=vol,
            liquidity=liq,
            volume_24hr=v24,
            line_delta=line_delta,
            low_liquidity=thin,
            low_confidence=low_c,
            n_books=n_books,
            fit_r2=fit.r2 if fit else None,
            market_id=str(kalshi["ticker"]) if kalshi.get("ticker") else None,
            ticker=str(kalshi["ticker"]) if kalshi.get("ticker") else None,
            confidence=conf,
            cfg=cfg,
            venue="kalshi",
            **meta,
        )
        if yes_ask is not None and _allow_ou_side(
            yes_side,
            market_line=common["market_line"],
            fair_line=fair_line,
            fit=fit,
            cfg=cfg,
        ):
            _emit_side(common, yes_side, fair_yes, yes_ask)
            _append_venue_info(
                informational,
                market=market,
                matchup=matchup,
                player=player,
                stat=stat,
                book="kalshi",
                line=k_line,
                ask=yes_ask,
                fair=fair_yes,
                n_books=n_books,
                side=alert_yes,
            )
        if no_ask is not None and _allow_ou_side(
            no_side,
            market_line=common["market_line"],
            fair_line=fair_line,
            fit=fit,
            cfg=cfg,
        ):
            _emit_side(common, no_side, 1.0 - fair_yes, no_ask)
            _append_venue_info(
                informational,
                market=market,
                matchup=matchup,
                player=player,
                stat=stat,
                book="kalshi",
                line=k_line,
                ask=no_ask,
                fair=1.0 - fair_yes,
                n_books=n_books,
                side=alert_no,
            )

    if isinstance(polymarket, dict):
        over = polymarket_side_ask(polymarket, "over")
        under = polymarket_side_ask(polymarket, "under")
        if over is None and under is None:
            over = polymarket_side_ask(polymarket, "home")
            under = polymarket_side_ask(polymarket, "away")
        poly_line = _f(polymarket.get("line"))
        # Game markets store American odds in `line`; ignore those as strikes.
        if poly_line is not None and abs(poly_line) >= 100:
            poly_line = venue_line
        p_line = poly_line if poly_line is not None else venue_line
        meta = _strike_meta(fit=fit, points=points, market_line=p_line)
        vol, liq, v24 = _depth(polymarket)
        conf, low_c = _conf(
            n_books=n_books,
            fit=fit,
            cons=cons,
            volume=vol,
            liquidity=liq,
            volume_24hr=v24,
            line_delta=line_delta if line_delta is not None else _f(polymarket.get("line_delta")),
            cfg=cfg,
            extrapolation_distance=meta["extrapolation_distance"],
            book_line_min=book_min,
        )
        thin = is_low_liquidity(volume=vol, liquidity=liq, volume_24hr=v24, cfg=cfg)
        common = dict(
            market=market,
            matchup=matchup,
            stat=stat,
            player=player,
            fair_line=fair_line,
            market_line=p_line,
            volume=vol,
            liquidity=liq,
            volume_24hr=v24,
            line_delta=line_delta if line_delta is not None else _f(polymarket.get("line_delta")),
            low_liquidity=thin,
            low_confidence=low_c,
            n_books=n_books,
            fit_r2=fit.r2 if fit else None,
            market_id=str(polymarket["market_id"]) if polymarket.get("market_id") else None,
            ticker=None,
            confidence=conf,
            cfg=cfg,
            venue="polymarket",
            **meta,
        )
        if over is not None and _allow_ou_side(
            yes_side,
            market_line=common["market_line"],
            fair_line=fair_line,
            fit=fit,
            cfg=cfg,
        ):
            _emit_side(common, yes_side, fair_yes, over)
            _append_venue_info(
                informational,
                market=market,
                matchup=matchup,
                player=player,
                stat=stat,
                book="polymarket",
                line=p_line,
                ask=over,
                fair=fair_yes,
                n_books=n_books,
                side=alert_yes,
            )
        if under is not None and _allow_ou_side(
            no_side,
            market_line=common["market_line"],
            fair_line=fair_line,
            fit=fit,
            cfg=cfg,
        ):
            _emit_side(common, no_side, 1.0 - fair_yes, under)
            _append_venue_info(
                informational,
                market=market,
                matchup=matchup,
                player=player,
                stat=stat,
                book="polymarket",
                line=p_line,
                ask=under,
                fair=1.0 - fair_yes,
                n_books=n_books,
                side=alert_no,
            )


def _info_rows(
    points: list[BookPoint],
    *,
    market: str,
    matchup: str,
    stat: str,
    player: str | None,
    fair_at: float | None = None,
    fit: FittedDistribution | None = None,
    yes_side: str = "over",
    no_side: str | None = "under",
) -> list[InformationalRow]:
    rows: list[InformationalRow] = []
    for p in points:
        if fit is not None:
            fair = fair_over_from_fit(fit, p.line)
        elif fair_at is not None:
            fair = fair_at
        else:
            continue
        if p.raw_over is not None:
            rows.append(
                InformationalRow(
                    market=market,
                    matchup=matchup,
                    player=player,
                    stat=stat,
                    book=p.book,
                    book_line=p.line,
                    book_prob=p.raw_over,
                    fair_prob=fair,
                    raw_edge=fair - p.raw_over,
                    n_books=len(points),
                    side=yes_side,
                    book_odds=p.over_odds,
                )
            )
        if no_side and p.raw_under is not None:
            fair_no = 1.0 - fair
            rows.append(
                InformationalRow(
                    market=market,
                    matchup=matchup,
                    player=player,
                    stat=stat,
                    book=p.book,
                    book_line=p.line,
                    book_prob=p.raw_under,
                    fair_prob=fair_no,
                    raw_edge=fair_no - p.raw_under,
                    n_books=len(points),
                    side=no_side,
                    book_odds=p.under_odds,
                )
            )
    return rows


def _process_ou(
    *,
    market_name: str,
    matchup: str,
    stat: str,
    player: str | None,
    blob: dict[str, Any],
    cfg: FairValueConfig,
    tradable: list[PricedOpportunity],
    rejected: list[PricedOpportunity],
    informational: list[InformationalRow],
) -> None:
    books = _books(blob)
    points = _ou_points(books, cfg)
    if not points:
        return
    kalshi = _obj(blob, "kalshi")
    poly = _obj(blob, "polymarket")
    consensus_line = _f(blob.get("consensus_line"))
    kalshi_line = _f(kalshi.get("line")) if kalshi else None
    poly_line = _f(poly.get("line")) if poly else None
    if poly_line is not None and abs(poly_line) >= 100:
        poly_line = consensus_line

    targets: list[tuple[str, float | None, dict[str, Any] | None, dict[str, Any] | None]] = []
    if kalshi is not None:
        targets.append(("kalshi", kalshi_line or consensus_line, kalshi, None))
    if poly is not None:
        targets.append(("poly", poly_line or consensus_line, None, poly))
    if not targets:
        fair, fit, cons = _fair_at_line(
            points, stat=stat, target_line=consensus_line, cfg=cfg
        )
        informational.extend(
            _info_rows(
                points,
                market=market_name,
                matchup=matchup,
                stat=stat,
                player=player,
                fair_at=fair if fit is None else None,
                fit=fit,
            )
        )
        return

    seen_info = False
    for _label, target, k_blob, p_blob in targets:
        fair, fit, cons = _fair_at_line(points, stat=stat, target_line=target, cfg=cfg)
        if not seen_info:
            informational.extend(
                _info_rows(
                    points,
                    market=market_name,
                    matchup=matchup,
                    stat=stat,
                    player=player,
                    fair_at=fair if fit is None else None,
                    fit=fit,
                )
            )
            seen_info = True
        line_delta = None
        src = k_blob or p_blob or {}
        line_delta = _f(src.get("line_delta"))
        if line_delta is None and target is not None and consensus_line is not None:
            line_delta = target - consensus_line
        _emit_binary_venue(
            tradable=tradable,
            rejected=rejected,
            market=market_name,
            matchup=matchup,
            stat=stat,
            player=player,
            fair_yes=fair,
            fair_line=consensus_line or (cons.line if cons else None) or (fit.mu if fit else None),
            venue_line=target,
            line_delta=line_delta,
            n_books=len(points),
            fit=fit,
            cons=cons,
            cfg=cfg,
            kalshi=k_blob,
            polymarket=p_blob,
            yes_side="over",
            no_side="under",
            points=points,
            informational=informational,
        )


def _process_spread(
    *,
    matchup: str,
    blob: dict[str, Any],
    cfg: FairValueConfig,
    tradable: list[PricedOpportunity],
    rejected: list[PricedOpportunity],
    informational: list[InformationalRow],
) -> None:
    books = _books(blob)
    points = _spread_points(books, cfg)
    if not points:
        return
    kalshi = _obj(blob, "kalshi")
    poly = _obj(blob, "polymarket")
    consensus_line = _f(blob.get("consensus_line"))
    kalshi_home = None
    if kalshi:
        kalshi_home = _f(kalshi.get("line"))
        if kalshi_home is None:
            kalshi_home = _f(kalshi.get("home_line"))
    # Fit on margin threshold −home_spread.
    fit_points = _threshold_points(points)
    dist = cfg.distribution_for("spread")
    common_home = lines_are_same_strike([p.line for p in points], cfg.same_strike_line_tolerance)

    def fair_home_at(home_line: float | None) -> tuple[float, FittedDistribution | None, SameStrikeConsensus | None]:
        if home_line is None:
            home_line = consensus_line
        if home_line is None:
            cons = same_strike_consensus(points, cfg=cfg)
            return cons.fair_prob, None, cons
        target_matches = abs(
            (sum(p.line * p.weight for p in points) / sum(p.weight for p in points)) - home_line
        ) <= cfg.same_strike_line_tolerance
        if dist == "same_strike" or (common_home and target_matches):
            cons = same_strike_consensus(points, cfg=cfg)
            return cons.fair_prob, None, cons
        kind = dist if dist in {"normal", "poisson", "nbinom"} else "normal"
        fit = fit_distribution(fit_points, kind, stat="spread", cfg=cfg)
        return fair_over_from_fit(fit, -home_line), fit, None

    for point in points:
        fair_book, _, _ = fair_home_at(point.line)
        if point.raw_over is not None:
            informational.append(
                InformationalRow(
                    market=f"{matchup} spread",
                    matchup=matchup,
                    player=None,
                    stat="spread",
                    book=point.book,
                    book_line=point.line,
                    book_prob=point.raw_over,
                    fair_prob=fair_book,
                    raw_edge=fair_book - point.raw_over,
                    n_books=len(points),
                    side="home",
                    book_odds=point.over_odds,
                )
            )
        if point.raw_under is not None:
            fair_away = 1.0 - fair_book
            informational.append(
                InformationalRow(
                    market=f"{matchup} spread",
                    matchup=matchup,
                    player=None,
                    stat="spread",
                    book=point.book,
                    book_line=point.line,
                    book_prob=point.raw_under,
                    fair_prob=fair_away,
                    raw_edge=fair_away - point.raw_under,
                    n_books=len(points),
                    side="away",
                    book_odds=point.under_odds,
                )
            )

    if kalshi:
        fair, fit, cons = fair_home_at(kalshi_home)
        # Yes pays the named team ("CAR wins by over 3.5"), not always home.
        contract_yes = str(kalshi.get("yes_side") or "home").strip().lower()
        if contract_yes not in {"home", "away"}:
            contract_yes = "home"
        if contract_yes == "away":
            fair_yes = 1.0 - fair
            info_yes, info_no = "away", "home"
        else:
            fair_yes = fair
            info_yes, info_no = "home", "away"
        _emit_binary_venue(
            tradable=tradable,
            rejected=rejected,
            market=f"{matchup} spread",
            matchup=matchup,
            stat="spread",
            player=None,
            fair_yes=fair_yes,
            fair_line=consensus_line,
            venue_line=kalshi_home,
            line_delta=_f(kalshi.get("line_delta")),
            n_books=len(points),
            fit=fit,
            cons=cons,
            cfg=cfg,
            kalshi=kalshi,
            polymarket=None,
            yes_side="yes",
            no_side="no",
            points=points,
            informational=informational,
            info_yes_side=info_yes,
            info_no_side=info_no,
        )
    if poly:
        fair, fit, cons = fair_home_at(consensus_line)
        home_px = polymarket_side_ask(poly, "home")
        away_px = polymarket_side_ask(poly, "away")
        meta = _strike_meta(fit=fit, points=points, market_line=consensus_line)
        rng = strike_range(points)
        vol, liq, v24 = _depth(poly)
        conf, low_c = _conf(
            n_books=len(points),
            fit=fit,
            cons=cons,
            volume=vol,
            liquidity=liq,
            volume_24hr=v24,
            line_delta=0.0,
            cfg=cfg,
            extrapolation_distance=meta["extrapolation_distance"],
            book_line_min=rng[0] if rng else None,
        )
        thin = is_low_liquidity(volume=vol, liquidity=liq, volume_24hr=v24, cfg=cfg)
        common = dict(
            market=f"{matchup} spread",
            matchup=matchup,
            stat="spread",
            player=None,
            fair_line=consensus_line,
            market_line=consensus_line,
            volume=vol,
            liquidity=liq,
            volume_24hr=v24,
            line_delta=0.0,
            low_liquidity=thin,
            low_confidence=low_c,
            n_books=len(points),
            fit_r2=fit.r2 if fit else None,
            market_id=str(poly["home"]["market_id"])
            if isinstance(poly.get("home"), dict) and poly["home"].get("market_id")
            else (str(poly["market_id"]) if poly.get("market_id") else None),
            ticker=None,
            confidence=conf,
            cfg=cfg,
            venue="polymarket",
            **meta,
        )
        if home_px is not None:
            _route_opp(
                _make_opp({**common, "side": "home", "fair_prob": fair, "market_price": home_px}),
                tradable=tradable,
                rejected=rejected,
                fit=fit,
                points=points,
                cfg=cfg,
            )
            _append_venue_info(
                informational,
                market=f"{matchup} spread",
                matchup=matchup,
                player=None,
                stat="spread",
                book="polymarket",
                line=consensus_line,
                ask=home_px,
                fair=fair,
                n_books=len(points),
                side="home",
            )
        if away_px is not None:
            _route_opp(
                _make_opp(
                    {**common, "side": "away", "fair_prob": 1.0 - fair, "market_price": away_px}
                ),
                tradable=tradable,
                rejected=rejected,
                fit=fit,
                points=points,
                cfg=cfg,
            )
            _append_venue_info(
                informational,
                market=f"{matchup} spread",
                matchup=matchup,
                player=None,
                stat="spread",
                book="polymarket",
                line=consensus_line,
                ask=away_px,
                fair=1.0 - fair,
                n_books=len(points),
                side="away",
            )


def _process_moneyline(
    *,
    matchup: str,
    blob: dict[str, Any],
    cfg: FairValueConfig,
    tradable: list[PricedOpportunity],
    rejected: list[PricedOpportunity],
    informational: list[InformationalRow],
) -> None:
    books = _books(blob)
    points = _ml_points(books, cfg)
    if not points:
        return
    cons = same_strike_consensus(points, cfg=cfg)
    fair_home = cons.fair_prob
    informational.extend(
        _info_rows(
            points,
            market=f"{matchup} ML",
            matchup=matchup,
            stat="moneyline",
            player=None,
            fair_at=fair_home,
            yes_side="home",
            no_side="away",
        )
    )
    kalshi = _obj(blob, "kalshi")
    poly = _obj(blob, "polymarket")
    if isinstance(kalshi, dict):
        for side, fair in (("home", fair_home), ("away", 1.0 - fair_home)):
            contract = kalshi.get(side)
            if not isinstance(contract, dict):
                continue
            _emit_binary_venue(
                tradable=tradable,
                rejected=rejected,
                market=f"{matchup} ML {side}",
                matchup=matchup,
                stat="moneyline",
                player=None,
                fair_yes=fair,
                fair_line=None,
                venue_line=None,
                line_delta=None,
                n_books=len(points),
                fit=None,
                cons=cons,
                cfg=cfg,
                kalshi=contract,
                polymarket=None,
                yes_side="yes",
                no_side="no",
                informational=informational,
                info_yes_side=side,
                info_no_side=None,
            )
    if isinstance(poly, dict):
        vol, liq, v24 = _depth(poly)
        conf, low_c = _conf(
            n_books=len(points),
            fit=None,
            cons=cons,
            volume=vol,
            liquidity=liq,
            volume_24hr=v24,
            line_delta=None,
            cfg=cfg,
        )
        thin = is_low_liquidity(volume=vol, liquidity=liq, volume_24hr=v24, cfg=cfg)
        for side, fair in (("home", fair_home), ("away", 1.0 - fair_home)):
            px = polymarket_side_ask(poly, side)
            if px is None:
                continue
            side_blob = poly.get(side)
            mid: dict[str, Any] = side_blob if isinstance(side_blob, dict) else {}
            tradable.append(
                opportunity(
                    market=f"{matchup} ML {side}",
                    matchup=matchup,
                    stat="moneyline",
                    player=None,
                    venue="polymarket",
                    side="yes",
                    fair_prob=fair,
                    market_price=px,
                    cfg=cfg,
                    fair_line=None,
                    market_line=None,
                    volume=vol,
                    liquidity=liq or _f(mid.get("liquidity")),
                    volume_24hr=v24 or _f(mid.get("volume_24hr")),
                    line_delta=None,
                    low_liquidity=thin,
                    low_confidence=low_c,
                    n_books=len(points),
                    fit_r2=None,
                    market_id=str(mid["market_id"]) if mid.get("market_id") else None,
                    confidence=conf,
                )
            )
            _append_venue_info(
                informational,
                market=f"{matchup} ML {side}",
                matchup=matchup,
                player=None,
                stat="moneyline",
                book="polymarket",
                line=None,
                ask=px,
                fair=fair,
                n_books=len(points),
                side=side,
            )


def _process_yes_prop(
    *,
    market_name: str,
    matchup: str,
    stat: str,
    player: str | None,
    blob: dict[str, Any],
    cfg: FairValueConfig,
    tradable: list[PricedOpportunity],
    rejected: list[PricedOpportunity],
    informational: list[InformationalRow],
) -> None:
    books = _books(blob)
    points = _yes_points(books, cfg)
    if not points:
        return
    cons = same_strike_consensus(points, cfg=cfg)
    informational.extend(
        _info_rows(
            points,
            market=market_name,
            matchup=matchup,
            stat=stat,
            player=player,
            fair_at=cons.fair_prob,
            yes_side="yes",
            no_side=None,
        )
    )
    kalshi = _obj(blob, "kalshi")
    poly = _obj(blob, "polymarket")
    _emit_binary_venue(
        tradable=tradable,
        rejected=rejected,
        market=market_name,
        matchup=matchup,
        stat=stat,
        player=player,
        fair_yes=cons.fair_prob,
        fair_line=None,
        venue_line=_f(kalshi.get("line")) if kalshi else None,
        line_delta=_f((kalshi or poly or {}).get("line_delta")),
        n_books=len(points),
        fit=None,
        cons=cons,
        cfg=cfg,
        kalshi=kalshi,
        polymarket=poly,
        yes_side="yes",
        no_side="no",
        informational=informational,
        info_yes_side="yes",
        info_no_side=None,
    )


def process_game(
    game: dict[str, Any],
    cfg: FairValueConfig,
    *,
    tradable: list[PricedOpportunity],
    rejected: list[PricedOpportunity],
    informational: list[InformationalRow],
) -> int:
    """Process one game. Returns the number of markets touched."""
    matchup = _matchup_label(game)
    n = 0
    raw_markets = game.get("markets")
    markets: dict[str, Any] = raw_markets if isinstance(raw_markets, dict) else {}
    moneyline = markets.get("moneyline")
    if isinstance(moneyline, dict):
        _process_moneyline(
            matchup=matchup,
            blob=moneyline,
            cfg=cfg,
            tradable=tradable,
            rejected=rejected,
            informational=informational,
        )
        n += 1
    spread = markets.get("spread")
    if isinstance(spread, dict):
        _process_spread(
            matchup=matchup,
            blob=spread,
            cfg=cfg,
            tradable=tradable,
            rejected=rejected,
            informational=informational,
        )
        n += 1
    total = markets.get("total")
    if isinstance(total, dict):
        _process_ou(
            market_name=f"{matchup} total",
            matchup=matchup,
            stat="total",
            player=None,
            blob=total,
            cfg=cfg,
            tradable=tradable,
            rejected=rejected,
            informational=informational,
        )
        n += 1
    for prop in game.get("player_props") or []:
        if not isinstance(prop, dict):
            continue
        player = str(prop.get("player") or "") or None
        stat = str(prop.get("type") or "prop")
        label = f"{player} {stat}" if player else stat
        n += 1
        books = prop.get("books")
        if stat in _TWO_WAY_STATS and isinstance(books, dict) and books:
            sample = next(iter(books.values()), None)
            if isinstance(sample, dict) and (
                "over_odds" in sample or "over_implied_prob" in sample
            ):
                _process_ou(
                    market_name=label,
                    matchup=matchup,
                    stat=stat,
                    player=player,
                    blob=prop,
                    cfg=cfg,
                    tradable=tradable,
                    rejected=rejected,
                    informational=informational,
                )
                continue
        _process_yes_prop(
            market_name=label,
            matchup=matchup,
            stat=stat,
            player=player,
            blob=prop,
            cfg=cfg,
            tradable=tradable,
            rejected=rejected,
            informational=informational,
        )
    return n


def process_slate(payload: dict[str, Any], cfg: FairValueConfig | None = None) -> FairValueReport:
    cfg = cfg or FairValueConfig()
    tradable: list[PricedOpportunity] = []
    rejected: list[PricedOpportunity] = []
    informational: list[InformationalRow] = []
    n_markets = 0
    games = payload.get("games")
    if not isinstance(games, list):
        games = [payload] if payload.get("markets") or payload.get("books") else []
    for game in games:
        if isinstance(game, dict):
            n_markets += process_game(
                game,
                cfg,
                tradable=tradable,
                rejected=rejected,
                informational=informational,
            )
    return build_report(
        tradable,
        informational,
        rejected=rejected,
        min_edge_pct=cfg.min_edge_pct,
        n_markets=n_markets,
    )


def run_fair_value(
    odds_path: str | Path,
    *,
    cfg: FairValueConfig | None = None,
    config_path: str | Path | None = None,
    out_path: str | Path | None = None,
) -> FairValueReport:
    cfg = cfg or FairValueConfig.load(config_path)
    path = Path(odds_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected a JSON object in {path}")
    report = process_slate(payload, cfg)
    report.source = str(path)
    if out_path is not None:
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8")
    return report
