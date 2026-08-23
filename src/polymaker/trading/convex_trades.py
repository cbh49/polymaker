"""Convex trade ledger: claim before a live buy, record the fill, fail closed.

Canonical tradeKey is `{slug}|{outcome}` (lowercase outcome), matching
`polymaker.trading.execute._fill_key` so the sharp pipeline and
poly-sharp-finder cannot both buy the same market.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date
from typing import Any

import requests
from dotenv import load_dotenv


def trade_key(slug: str, outcome: str) -> str:
    return f"{slug.strip()}|{outcome.strip().lower()}"


def live_trading_enabled() -> bool:
    return os.environ.get("POLYMAKER_LIVE", "").strip().lower() in {"1", "true", "yes"}


@dataclass(frozen=True, slots=True)
class ClaimResult:
    claimed: bool
    detail: str = ""
    raw: dict[str, Any] | None = None


class ConvexTradeClient:
    def __init__(
        self,
        http_url: str | None = None,
        token: str | None = None,
        *,
        timeout: float = 20.0,
    ) -> None:
        load_dotenv()
        if http_url is None:
            http_url = os.environ.get("CONVEX_HTTP_URL") or ""
        if token is None:
            token = os.environ.get("CONVEX_PUBLISH_TOKEN") or ""
        self.http_url = http_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.http_url and self.token)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        if not self.configured:
            raise RuntimeError("CONVEX_HTTP_URL and CONVEX_PUBLISH_TOKEN must be set")
        r = requests.post(
            f"{self.http_url}{path}",
            headers=self._headers(),
            data=json.dumps(body),
            timeout=self.timeout,
        )
        if r.status_code != 200:
            raise RuntimeError(f"Convex {path} HTTP {r.status_code}: {r.text[:300]}")
        try:
            payload = r.json()
        except ValueError as exc:
            raise RuntimeError(f"Convex {path} returned non-JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"Convex {path} returned unexpected body")
        return payload

    def claim(
        self,
        *,
        trade_key_value: str,
        league: str,
        source: str,
        matchup: str,
        side: str,
        usd: float,
        prediction_date: str,
        slug: str | None = None,
        condition_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> ClaimResult:
        try:
            raw = self._post(
                "/trades/claim",
                {
                    "tradeKey": trade_key_value,
                    "league": league,
                    "source": source,
                    "matchup": matchup,
                    "side": side,
                    "usd": usd,
                    "predictionDate": prediction_date,
                    "slug": slug,
                    "conditionId": condition_id,
                    "payload": payload or {},
                },
            )
        except Exception as exc:  # noqa: BLE001 — fail closed
            return ClaimResult(claimed=False, detail=f"convex claim failed: {exc}")
        if raw.get("claimed") is True:
            return ClaimResult(claimed=True, detail="claimed", raw=raw)
        return ClaimResult(
            claimed=False,
            detail="already traded (convex ledger)",
            raw=raw,
        )

    def complete(
        self,
        trade_key_value: str,
        payload: dict[str, Any],
        *,
        token_id: str | None = None,
        start_time: int | None = None,
        buy_price: float | None = None,
        shares: float | None = None,
    ) -> None:
        body: dict[str, Any] = {"tradeKey": trade_key_value, "payload": payload}
        token_id = token_id or _optional_str(payload.get("token_id"))
        start_time = (
            start_time if start_time is not None else _optional_int(payload.get("start_time_ms"))
        )
        buy_price = buy_price if buy_price is not None else _optional_float(payload.get("buy_price"))
        shares = shares if shares is not None else _optional_float(payload.get("shares"))
        if token_id is not None:
            body["tokenId"] = token_id
        if start_time is not None:
            body["startTime"] = start_time
        if buy_price is not None:
            body["buyPrice"] = buy_price
        if shares is not None:
            body["shares"] = shares
        self._post("/trades/complete", body)

    def release(self, trade_key_value: str) -> None:
        try:
            self._post("/trades/release", {"tradeKey": trade_key_value})
        except Exception:  # noqa: BLE001
            return

    def publish_snapshot(
        self,
        *,
        prediction_date: str,
        payload: dict[str, Any],
        report_type: str = "polymarket_trades",
    ) -> None:
        self._post(
            "/publish",
            {
                "reportType": report_type,
                "predictionDate": prediction_date,
                "payload": payload,
            },
        )


def prediction_date_today() -> str:
    return date.today().isoformat()


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if n != n:
        return None
    return n
