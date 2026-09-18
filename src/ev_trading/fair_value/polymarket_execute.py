"""Place Polymarket FAK buys for high-confidence tradable NFL fair-value rows.

Only the `tradable` bucket is eligible (not low-liquidity or TD yes/no lists).
A row must be `venue=polymarket` with `confidence >= min_confidence`. Stake is
a fixed USD amount per side (default $10) via a CLOB market buy.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ev_trading.fair_value.kalshi_execute import (
    DEFAULT_MIN_CONFIDENCE,
    DEFAULT_USD,
    load_tradable_rows,
)
from ev_trading.fair_value.models import PricedOpportunity
from ev_trading.fair_value.report import FairValueReport
from polymaker.catalog.gamma import parse_market
from polymaker.catalog.sports import DEFAULT_PREGAME_BUFFER_MINUTES, is_pre_game
from polymaker.domain import MarketMeta, Side, TokenMeta
from polymaker.trading.fill import parse_buy_fill
from polymaker.trading.match import resolve_outcome_token, resolve_total_outcome_token
from polymaker.trading.teams import parse_matchup, resolve_team

DEFAULT_MAX_ASK_SLIPPAGE = 0.01
_BOT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_FILLED_LOG = _BOT_ROOT / "journal" / "nfl_polymarket_trades.jsonl"


class GammaMarketSource(Protocol):
    async def market_by_id(self, market_id: str) -> dict[str, Any] | None: ...

    async def aclose(self) -> None: ...


class PolyExecutionGateway(Protocol):
    async def connect(self) -> None: ...

    async def get_book(self, token_id: str) -> dict[str, float]: ...

    async def market_order(
        self,
        token_id: str,
        side: Side,
        amount: float,
        meta: MarketMeta,
        *,
        fak: bool = True,
    ) -> dict[str, Any]: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class PolyTradeConfig:
    usd: float = DEFAULT_USD
    min_confidence: float = DEFAULT_MIN_CONFIDENCE
    dry_run: bool = True
    filled_log: Path = DEFAULT_FILLED_LOG
    max_ask_slippage: float = DEFAULT_MAX_ASK_SLIPPAGE
    refresh_quote: bool = True
    pregame_buffer_minutes: float = DEFAULT_PREGAME_BUFFER_MINUTES
    config_dir: str = "config"


@dataclass(slots=True)
class PolyTradeResult:
    market: str
    market_id: str
    side: str
    action: str  # bought | dry_run | skipped | failed
    detail: str = ""
    usd: float = 0.0
    token_id: str | None = None
    outcome: str | None = None
    limit_price: float | None = None
    response: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "market": self.market,
            "market_id": self.market_id,
            "side": self.side,
            "action": self.action,
            "detail": self.detail,
            "usd": self.usd,
            "token_id": self.token_id,
            "outcome": self.outcome,
            "limit_price": self.limit_price,
            "response": self.response,
        }


def poly_trade_side(row: dict[str, Any]) -> str:
    """Map a fair-value row onto a Polymarket outcome.

    Moneyline rows are stored as `side=yes` on a two-team market; the real
    side is in the market name (`… ML home` / `… ML away`).
    """
    market = str(row.get("market") or "").strip().lower()
    if market.endswith(" ml home"):
        return "home"
    if market.endswith(" ml away"):
        return "away"
    side = str(row.get("side") or "").strip().lower()
    if side in {"over", "under", "home", "away", "yes", "no"}:
        return side
    raise ValueError(f"unmapped Polymarket side: {row.get('side')!r}")


def token_for_poly_side(meta: MarketMeta, side: str, matchup: str) -> TokenMeta:
    """Resolve Over/Under/Yes/No or home/away onto a CLOB token."""
    key = (side or "").strip().lower()
    if key in {"over", "under"}:
        return resolve_total_outcome_token(meta, key)
    if key in {"yes", "no"}:
        return meta.token_for_outcome(key)
    if key in {"home", "away"}:
        parsed = parse_matchup(matchup)
        if parsed is None:
            raise ValueError(f"cannot parse matchup {matchup!r}")
        away_raw, home_raw = parsed
        abbr = home_raw if key == "home" else away_raw
        team = resolve_team("nfl", abbr)
        if team is None:
            raise ValueError(f"unresolved NFL team {abbr!r}")
        return resolve_outcome_token(meta, team.full_name, team.poly_code)
    raise ValueError(f"unmapped Polymarket side: {side!r}")


def select_tradable_polymarket(
    rows: list[PricedOpportunity] | list[dict[str, Any]],
    *,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> list[dict[str, Any]]:
    """Keep `tradable` Polymarket rows at or above the confidence floor."""
    picked: list[dict[str, Any]] = []
    for raw in rows:
        row = raw.to_dict() if isinstance(raw, PricedOpportunity) else dict(raw)
        if str(row.get("venue") or "").lower() != "polymarket":
            continue
        try:
            conf = float(row.get("confidence") or 0.0)
        except (TypeError, ValueError):
            continue
        if conf < min_confidence:
            continue
        market_id = str(row.get("market_id") or row.get("ticker") or "").strip()
        if not market_id:
            continue
        try:
            poly_trade_side(row)
        except ValueError:
            continue
        picked.append(row)
    return picked


def run_polymarket_trades(
    source: FairValueReport | dict[str, Any] | Path | str,
    cfg: PolyTradeConfig | None = None,
    *,
    gamma: GammaMarketSource | None = None,
    gateway: PolyExecutionGateway | None = None,
) -> list[PolyTradeResult]:
    """Filter + size + (optionally) send FAK buys. Dry-run unless cfg.dry_run is False."""
    return asyncio.run(
        run_polymarket_trades_async(source, cfg, gamma=gamma, gateway=gateway)
    )


async def run_polymarket_trades_async(
    source: FairValueReport | dict[str, Any] | Path | str,
    cfg: PolyTradeConfig | None = None,
    *,
    gamma: GammaMarketSource | None = None,
    gateway: PolyExecutionGateway | None = None,
) -> list[PolyTradeResult]:
    cfg = cfg or PolyTradeConfig()
    rows = select_tradable_polymarket(
        load_tradable_rows(source),
        min_confidence=cfg.min_confidence,
    )
    already = _load_filled_keys(cfg.filled_log)
    owns_gamma = False
    owns_gw = False
    source_client = gamma
    gw = gateway
    if not cfg.dry_run:
        if source_client is None:
            from polymaker.catalog.gamma import GammaClient

            source_client = GammaClient()
            owns_gamma = True
        if gw is None:
            from polymaker.config import Config
            from polymaker.execution.gateway import ExecutionGateway

            gw = ExecutionGateway(Config.load(cfg.config_dir), paper=False)
            owns_gw = True
            await gw.connect()
    results: list[PolyTradeResult] = []
    try:
        for row in rows:
            results.append(await _trade_one(row, cfg, already, source_client, gw))
    finally:
        if owns_gamma and source_client is not None:
            await source_client.aclose()
        if owns_gw and gw is not None:
            gw.close()
    return results


def render_trade_results(results: list[PolyTradeResult]) -> None:
    if not results:
        print("polymarket trades: none matched (tradable + venue=polymarket + confidence floor)")
        return
    print(f"polymarket trades: {len(results)}")
    for row in results:
        px = f"{row.limit_price:.2f}" if row.limit_price is not None else "—"
        outcome = row.outcome or row.side
        print(
            f"  {row.action:<8} ${row.usd:<6.2f} {outcome:<18} @{px}  "
            f"{row.market_id}  {row.market}  {row.detail}"
        )


async def _trade_one(
    row: dict[str, Any],
    cfg: PolyTradeConfig,
    already: set[str],
    gamma: GammaMarketSource | None,
    gateway: PolyExecutionGateway | None,
) -> PolyTradeResult:
    market_id = str(row.get("market_id") or row.get("ticker") or "").strip()
    side = poly_trade_side(row)
    market = str(row.get("market") or market_id)
    key = _trade_key(market_id, side)
    base = PolyTradeResult(market=market, market_id=market_id, side=side, action="skipped")
    if key in already:
        base.detail = "already traded (dedupe log)"
        return base
    try:
        ask = float(row.get("market_price"))
    except (TypeError, ValueError):
        base.detail = "missing market_price"
        return base
    usd = float(cfg.usd)
    if usd <= 0:
        base.detail = "usd size is 0"
        return base

    if cfg.dry_run:
        return PolyTradeResult(
            market=market,
            market_id=market_id,
            side=side,
            action="dry_run",
            detail="FAK market buy",
            usd=usd,
            limit_price=ask,
        )

    if gamma is None or gateway is None:
        base.action = "failed"
        base.detail = "no Polymarket client"
        return base

    try:
        raw = await gamma.market_by_id(market_id)
    except Exception as exc:  # noqa: BLE001
        base.action = "failed"
        base.detail = f"gamma fetch failed: {exc}"
        return base
    if not raw:
        base.detail = "gamma market missing"
        return base
    meta = parse_market(raw)
    if meta is None:
        base.detail = "market not accepting orders"
        return base
    if meta.start_time_iso and not is_pre_game(
        {"startTime": meta.start_time_iso},
        cfg.pregame_buffer_minutes,
    ):
        base.detail = "not pre-game (startTime)"
        return base
    try:
        token = token_for_poly_side(meta, side, str(row.get("matchup") or ""))
    except ValueError as exc:
        base.detail = str(exc)
        return base

    live_ask = ask
    if cfg.refresh_quote:
        book = await gateway.get_book(token.token_id)
        live = book.get("best_ask") if book else None
        if live is None or live <= 0 or live >= 1:
            base.detail = "live ask missing"
            return base
        if live > ask + cfg.max_ask_slippage:
            base.detail = f"ask moved {ask:.2f} → {live:.2f}"
            return base
        live_ask = float(live)

    min_notional = float(meta.min_order_size) * live_ask
    if usd + 1e-9 < min_notional:
        base.detail = f"${usd:.2f} below min order ${min_notional:.2f}"
        return base

    try:
        resp = await gateway.market_order(token.token_id, Side.BUY, usd, meta, fak=True)
    except Exception as exc:  # noqa: BLE001
        base.action = "failed"
        base.detail = f"order error: {exc}"
        return base

    status = str((resp or {}).get("status", (resp or {}).get("error", ""))).lower()
    if (resp or {}).get("error") or "error" in status or "fail" in status:
        base.action = "failed"
        base.detail = status or "sent"
        base.token_id = token.token_id
        base.outcome = token.outcome
        base.usd = usd
        base.limit_price = live_ask
        base.response = resp if isinstance(resp, dict) else {"raw": resp}
        return base

    _buy_price, shares = parse_buy_fill(resp if isinstance(resp, dict) else None, live_ask)
    if shares is not None and shares <= 0:
        base.detail = "FAK no fill"
        base.token_id = token.token_id
        base.outcome = token.outcome
        base.usd = usd
        base.limit_price = live_ask
        base.response = resp if isinstance(resp, dict) else {"raw": resp}
        return base

    _append_filled(
        cfg.filled_log,
        {
            "key": key,
            "market_id": market_id,
            "side": side,
            "outcome": token.outcome,
            "token_id": token.token_id,
            "usd": usd,
            "ask": live_ask,
            "slug": meta.slug,
        },
    )
    already.add(key)
    return PolyTradeResult(
        market=market,
        market_id=market_id,
        side=side,
        action="bought",
        detail=status or "sent",
        usd=usd,
        token_id=token.token_id,
        outcome=token.outcome,
        limit_price=live_ask,
        response=resp if isinstance(resp, dict) else {"raw": resp},
    )


def _trade_key(market_id: str, side: str) -> str:
    return f"{market_id}:{(side or '').strip().lower()}"


def _load_filled_keys(path: Path | str) -> set[str]:
    log_path = Path(path)
    if not log_path.is_file():
        return set()
    keys: set[str] = set()
    for line in log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("key"):
            keys.add(str(row["key"]))
    return keys


def _append_filled(path: Path | str, row: dict[str, Any]) -> None:
    log_path = Path(path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")
