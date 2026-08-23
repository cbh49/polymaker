"""
Signal → Polymarket buy executor.

Dry-run by default (logs would-buy). Pass dry_run=False / --live to send
FAK market buys via polymaker ExecutionGateway.
"""

from __future__ import annotations

import json
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from config import TRADE, TradeConfig
from detector import Signal
from registry import WatchedMarket

from polymaker.catalog.gamma import GammaClient
from polymaker.catalog.sports import is_pre_game
from polymaker.config import Config
from polymaker.domain import MarketMeta, Side
from polymaker.execution.gateway import ExecutionGateway
from polymaker.trading.convex_trades import ConvexTradeClient, prediction_date_today, trade_key


class SignalExecutor:
    def __init__(
        self,
        cfg: Config,
        markets: list[WatchedMarket],
        trade_cfg: TradeConfig | None = None,
        *,
        store_get_meta=None,
    ) -> None:
        self._cfg = cfg
        self._trade = trade_cfg or replace(TRADE)
        self._markets = {m.condition_id: m for m in markets}
        self._store_get_meta = store_get_meta  # callable(condition_id) -> MarketMeta | None
        self._gw: ExecutionGateway | None = None
        self._gamma: GammaClient | None = None
        self._event_cache: dict[str, dict[str, Any]] = {}
        self._convex = ConvexTradeClient()
        self._already = _load_filled_keys(self._trade.filled_log)
        self._today = datetime.now(UTC).date().isoformat()

    @property
    def trade_cfg(self) -> TradeConfig:
        return self._trade

    async def start(self) -> None:
        if not self._trade.enabled:
            print("[executor] trading disabled (--no-trade)")
            return
        # Always connect when we have a wallet so dry-run can fetch live books.
        # paper=True when dry_run so we never post orders.
        self._gw = ExecutionGateway(self._cfg, paper=self._trade.dry_run)
        await self._gw.connect()
        self._gamma = GammaClient(self._cfg.wallet.gamma_host)
        mode = "DRY-RUN" if self._trade.dry_run else "LIVE"
        print(f"[executor] connected ({mode}), usd={self._trade.usd_per_signal}, "
              f"max_ask={self._trade.max_ask}")
        print(f"[executor] intents → {self._trade.intents_log}")
        if not self._trade.dry_run:
            print(f"[executor] live fills → {self._trade.filled_log}")

    def close(self) -> None:
        if self._gw is not None:
            self._gw.close()
            self._gw = None

    async def aclose(self) -> None:
        self.close()
        if self._gamma is not None:
            await self._gamma.aclose()
            self._gamma = None

    def should_trade(self, sig: Signal) -> bool:
        if not self._trade.enabled:
            return False
        return sig.signal_type in self._trade.signal_types

    async def on_signal(self, sig: Signal) -> dict[str, Any] | None:
        if not self.should_trade(sig):
            return None

        market = self._markets.get(sig.condition_id)
        if market is None:
            return {"action": "skipped", "detail": "unknown market"}

        key = _fill_key(
            sig.condition_id,
            sig.side,
            self._today,
            per_market=self._trade.dedupe_per_market,
        )
        if key in self._already:
            print(f"[executor] skip dedupe {market.label} ({sig.signal_type})")
            return {"action": "skipped", "detail": "already traded (dedupe)"}

        usd = float(self._trade.usd_per_signal)
        if usd <= 0:
            return {"action": "skipped", "detail": "usd size is 0"}

        try:
            token_id = market.token_id_for_side(sig.side)
            buy_team = market.outcome_for_side(sig.side)
        except ValueError as exc:
            return {"action": "skipped", "detail": str(exc)}

        event = await self._event_for_pregame_gate(market)
        if not is_pre_game(event, self._trade.pregame_buffer_minutes):
            detail = "not pre-game (startTime)"
            print(f"[executor] skip {market.label}: {detail}")
            self._already.add(key)
            return {"action": "skipped", "detail": detail, "usd": usd}

        token_side = sig.side.strip().lower()
        self._already.add(key)

        book: dict[str, Any] | None = None
        ask: float | None = None
        if self._gw is not None:
            book = await self._gw.get_book(token_id)
            ask = _best_ask(book)

        px = _price_for_gate(ask, sig)
        if px is None or px > self._trade.max_ask:
            detail = (
                f"ask {px:.3f} above max_ask {self._trade.max_ask}"
                if px is not None
                else f"no ask (max_ask {self._trade.max_ask})"
            )
            print(f"[executor] skip {market.label} {sig.side}: {detail}")
            self._already.discard(key)  # allow retry if price improves
            return {"action": "skipped", "detail": detail, "usd": usd}

        if self._trade.dry_run:
            detail = _intent_detail(usd, buy_team, ask, sig)
            print(f"[executor] DRY-RUN {detail}")
            intent = _build_intent(
                sig=sig,
                market=market,
                key=key,
                day=self._today,
                buy_team=buy_team,
                token_side=token_side,
                usd=usd,
                ask=ask,
                action="dry_run",
                detail=detail,
            )
            _append_filled(self._trade.intents_log, intent)
            return {
                "action": "dry_run",
                "usd": usd,
                "detail": detail,
                "book": book,
                "token_id": token_id,
            }

        meta = self._resolve_meta(market)
        if meta is None:
            detail = "no MarketMeta in catalog — run export with --refresh"
            print(f"[executor] skip {market.label}: {detail}")
            self._already.discard(key)
            return {"action": "skipped", "detail": detail}

        if not self._convex.configured:
            detail = "convex unavailable (fail closed)"
            print(f"[executor] skip {market.label}: {detail}")
            self._already.discard(key)
            return {"action": "skipped", "detail": detail}

        ledger_key = trade_key(market.slug or market.condition_id, buy_team)
        claim = self._convex.claim(
            trade_key_value=ledger_key,
            league=market.league,
            source=sig.signal_type,
            matchup=market.label,
            side=buy_team,
            usd=usd,
            prediction_date=prediction_date_today(),
            slug=market.slug or None,
            condition_id=market.condition_id,
            payload={"signal_type": sig.signal_type, "ask": ask},
        )
        if not claim.claimed:
            print(f"[executor] skip {market.label}: {claim.detail}")
            self._already.discard(key)
            return {"action": "skipped", "detail": claim.detail, "usd": usd}

        assert self._gw is not None
        try:
            resp = await self._gw.market_order(token_id, Side.BUY, usd, meta, fak=True)
        except Exception as exc:  # noqa: BLE001
            self._convex.release(ledger_key)
            self._already.discard(key)
            return {"action": "failed", "detail": f"order error: {exc}", "usd": usd}

        payload = _build_intent(
            sig=sig,
            market=market,
            key=key,
            day=self._today,
            buy_team=buy_team,
            token_side=token_side,
            usd=usd,
            ask=ask,
            resp=resp,
        )
        payload["ledger_key"] = ledger_key
        status = str(resp.get("status", resp.get("error", ""))).lower()
        action = "bought" if "error" not in status and not resp.get("error") else "failed"
        if action == "failed":
            self._convex.release(ledger_key)
            self._already.discard(key)
        else:
            try:
                self._convex.complete(ledger_key, payload)
            except Exception as exc:  # noqa: BLE001
                payload["convex_complete_error"] = str(exc)
            _append_filled(self._trade.filled_log, payload)
            _append_filled(
                self._trade.intents_log,
                {**payload, "action": action, "detail": status or "sent"},
            )
        print(f"[executor] LIVE {action} ${usd:.2f} {buy_team} ML {market.label}: {status or resp}")
        return {"action": action, "usd": usd, "detail": status or "sent", "response": resp}

    def _resolve_meta(self, market: WatchedMarket) -> MarketMeta | None:
        if self._store_get_meta is not None:
            meta = self._store_get_meta(market.condition_id)
            if meta is not None:
                return meta
        # Minimal meta from watch list when catalog miss (tick/neg_risk defaults)
        if not market.yes_token_id or not market.no_token_id:
            return None
        from polymaker.domain import TokenMeta

        return MarketMeta(
            condition_id=market.condition_id,
            question=market.label,
            slug=market.slug or market.condition_id,
            tokens=(
                TokenMeta(market.yes_token_id, market.yes_outcome or "Yes"),
                TokenMeta(market.no_token_id, market.no_outcome or "No"),
            ),
            tick_size=0.01,
            neg_risk=False,
            min_order_size=1.0,
            rewards_min_size=0.0,
            rewards_max_spread=0.0,
            rewards_daily_rate=0.0,
            maker_fee_bps=0,
            taker_fee_bps=0,
            fees_enabled=False,
            end_date_iso=None,
            event_id=None,
            start_time_iso=market.start_time or None,
        )

    async def _event_for_pregame_gate(self, market: WatchedMarket) -> dict[str, Any]:
        """Resolve an event dict for `is_pre_game`. Prefer stored startTime."""
        if market.start_time:
            return {"startTime": market.start_time}
        slug = market.slug or ""
        if slug in self._event_cache:
            return self._event_cache[slug]
        event: dict[str, Any] = {}
        if self._gamma is not None and slug:
            fetched = await self._gamma.event_by_slug(slug)
            if fetched:
                event = fetched
        if slug:
            self._event_cache[slug] = event
        return event


def _signal_metrics(sig: Signal) -> dict[str, Any]:
    """Extract log-friendly metrics from the triggering signal."""
    d = sig.detail or {}
    if sig.signal_type == "whale_trade":
        return {
            "size_usd": round(float(d.get("size_usd") or 0), 2),
            "price": d.get("price"),
            "tier": d.get("tier"),
            "wallet": d.get("wallet"),
        }
    if sig.signal_type == "convergence":
        return {
            "wallet_count": d.get("wallet_count"),
            "wallets": d.get("wallets"),
        }
    return dict(d)


def _intent_detail(usd: float, buy_team: str, ask: float | None, sig: Signal) -> str:
    metrics = _signal_metrics(sig)
    if sig.signal_type == "whale_trade":
        trigger = f"whale ${metrics['size_usd']:,.2f}"
    else:
        trigger = sig.signal_type
    return (
        f"would BUY ${usd:.2f} {buy_team} ML @ ask={ask} "
        f"[{trigger}] {sig.label}"
    )


def _build_intent(
    *,
    sig: Signal,
    market: WatchedMarket,
    key: str,
    day: str,
    buy_team: str,
    token_side: str,
    usd: float,
    ask: float | None,
    action: str | None = None,
    detail: str | None = None,
    resp: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ts": time.time(),
        "day": day,
        "key": key,
        "condition_id": sig.condition_id,
        "slug": market.slug,
        "label": market.label,
        "side": buy_team,
        "token_side": token_side,
        "signal_type": sig.signal_type,
        "signal": _signal_metrics(sig),
        "usd": usd,
        "ask": ask,
    }
    if action is not None:
        payload["action"] = action
    if detail is not None:
        payload["detail"] = detail
    if resp is not None:
        payload["resp"] = resp
    return payload


def _fill_key(
    condition_id: str,
    side: str,
    day: str,
    *,
    per_market: bool = True,
) -> str:
    if per_market:
        return f"{day}|{condition_id}"
    return f"{day}|{condition_id}|{side.strip().lower()}"


def _best_ask(book: dict[str, Any] | None) -> float | None:
    if not book or book.get("best_ask") is None:
        return None
    try:
        return float(book["best_ask"])
    except (TypeError, ValueError):
        return None


def _signal_last_price(sig: Signal) -> float | None:
    raw = (sig.detail or {}).get("price")
    try:
        if raw is not None:
            return float(raw)
    except (TypeError, ValueError):
        return None
    return None


def _price_for_gate(ask: float | None, sig: Signal) -> float | None:
    """Book ask first; whale last-trade price if the book is missing."""
    if ask is not None:
        return ask
    return _signal_last_price(sig)


def _load_filled_keys(path: str | Path) -> set[str]:
    """Load dedupe keys. Also expands day|cid|side → day|cid for per-market dedupe."""
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
        if not isinstance(key, str):
            continue
        keys.add(key)
        parts = key.split("|")
        if len(parts) >= 2:
            keys.add(f"{parts[0]}|{parts[1]}")
    return keys


def _append_filled(path: str | Path, payload: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, default=str) + "\n")
