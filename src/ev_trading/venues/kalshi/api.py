"""Kalshi Trade API client: public GET, RSA-PSS on 401 / all POSTs.

Despite the elections subdomain, this host serves all Kalshi markets including sports.
"""

from __future__ import annotations

import base64
import os
import time
from pathlib import Path
from typing import Any

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from dotenv import load_dotenv

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
API_PREFIX = "/trade-api/v2"

SCRIPT_DIR = Path(__file__).resolve().parent
EVENTS_PAGE_LIMIT = 200
MARKETS_PAGE_LIMIT = 1000
MAX_RETRIES = 8
REQUEST_TIMEOUT = 30


def _find_env_file() -> Path | None:
    """Prefer trading-bot/.env whether we are invoked from repo root or this folder."""
    seen: set[Path] = set()
    candidates = [Path.cwd() / ".env"]
    for parent in (SCRIPT_DIR, *SCRIPT_DIR.parents):
        candidates.append(parent / ".env")
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.is_file():
            return resolved
    return None


def _load_env() -> Path | None:
    env_path = _find_env_file()
    if env_path is not None:
        load_dotenv(env_path)
    else:
        load_dotenv()
    return env_path


def _resolve_private_key_path(raw: str, env_file: Path | None) -> Path:
    path = Path(raw).expanduser()
    if path.is_file():
        return path
    if path.is_absolute():
        return path
    search = []
    if env_file is not None:
        search.append(env_file.parent / path)
    search.extend([Path.cwd() / path, SCRIPT_DIR / path])
    for candidate in search:
        if candidate.is_file():
            return candidate
    return path


class KalshiClient:
    """Kalshi Trade API client: public GET, RSA-PSS on 401 / all POSTs, backoff on 429."""

    def __init__(self, *, api_key_id: str, private_key_path: Path | None) -> None:
        self.api_key_id = api_key_id
        self.private_key_path = private_key_path
        self._private_key: rsa.RSAPrivateKey | None = None
        self._use_auth = False
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})

    def close(self) -> None:
        self.session.close()

    @classmethod
    def from_env(cls, *, require_auth: bool = False) -> KalshiClient:
        env_file = _load_env()
        api_key_id = os.getenv("KALSHI_API_KEY_ID", "").strip()
        key_raw = os.getenv("KALSHI_PRIVATE_KEY_PATH", "").strip()
        private_key_path = _resolve_private_key_path(key_raw, env_file) if key_raw else None
        if require_auth:
            if not api_key_id:
                raise RuntimeError("KALSHI_API_KEY_ID is missing.")
            if private_key_path is None or not private_key_path.is_file():
                raise FileNotFoundError(
                    "KALSHI_PRIVATE_KEY_PATH is missing or not a file. "
                    f"path={private_key_path!s}"
                )
        return cls(api_key_id=api_key_id, private_key_path=private_key_path)

    @property
    def has_auth(self) -> bool:
        return bool(self.api_key_id and self.private_key_path and self.private_key_path.is_file())

    def _load_private_key(self) -> rsa.RSAPrivateKey:
        if self._private_key is not None:
            return self._private_key
        if self.private_key_path is None or not self.private_key_path.is_file():
            raise FileNotFoundError(
                "Kalshi returned 401 and KALSHI_PRIVATE_KEY_PATH is missing or not a file. "
                f"path={self.private_key_path!s}"
            )
        if not self.api_key_id:
            raise RuntimeError("Kalshi returned 401 but KALSHI_API_KEY_ID is empty.")
        pem = self.private_key_path.read_bytes()
        loaded = serialization.load_pem_private_key(pem, password=None)
        if not isinstance(loaded, rsa.RSAPrivateKey):
            raise TypeError(f"Expected an RSA private key, got {type(loaded).__name__}")
        self._private_key = loaded
        return loaded

    def _signed_headers(self, method: str, sign_path: str) -> dict[str, str]:
        timestamp = str(int(time.time() * 1000))
        message = f"{timestamp}{method.upper()}{sign_path}".encode()
        signature = self._load_private_key().sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
        }

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        auth: bool | None = None,
    ) -> dict[str, Any]:
        """HTTP call. Query params are sent but never included in the signature."""
        if not path.startswith("/"):
            path = "/" + path
        url = f"{BASE_URL}{path}"
        sign_path = f"{API_PREFIX}{path}"
        method_u = method.upper()
        use_auth = self._use_auth if auth is None else auth
        last_error: Exception | None = None

        for attempt in range(MAX_RETRIES):
            headers: dict[str, str] = {}
            if use_auth:
                headers.update(self._signed_headers(method_u, sign_path))
            if json_body is not None:
                headers["Content-Type"] = "application/json"
            try:
                response = self.session.request(
                    method_u,
                    url,
                    params=params,
                    json=json_body,
                    headers=headers,
                    timeout=REQUEST_TIMEOUT,
                )
            except requests.RequestException as exc:
                last_error = exc
                time.sleep(min(60.0, 2**attempt))
                continue

            if response.status_code == 401 and not use_auth:
                use_auth = True
                self._use_auth = True
                continue

            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after else min(60.0, 2**attempt)
                except ValueError:
                    delay = min(60.0, 2**attempt)
                time.sleep(delay)
                continue

            if response.status_code >= 500:
                time.sleep(min(60.0, 2**attempt))
                last_error = requests.HTTPError(
                    f"{response.status_code} {path}: {response.text[:200]}",
                    response=response,
                )
                continue

            if response.status_code >= 400:
                raise requests.HTTPError(
                    f"{response.status_code} {method_u} {path}: {response.text[:400]}",
                    response=response,
                )

            if not response.content:
                return {}
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError(f"Expected JSON object from {path}, got {type(payload).__name__}")
            return payload

        if last_error is not None:
            raise last_error
        raise RuntimeError(f"{method_u} {path} failed after {MAX_RETRIES} retries")

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.request("GET", path, params=params)

    def post(self, path: str, json_body: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", path, json_body=json_body, auth=True)

    def get_market(self, ticker: str) -> dict[str, Any]:
        data = self.get(f"/markets/{ticker}")
        market = data.get("market")
        return market if isinstance(market, dict) else data

    def create_event_order(
        self,
        *,
        ticker: str,
        side: str,
        count: str,
        price: str,
        time_in_force: str = "immediate_or_cancel",
        self_trade_prevention_type: str = "taker_at_cross",
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        """Buy/sell YES on the V2 event-market book (`bid` = buy YES, `ask` = sell YES / buy NO)."""
        body: dict[str, Any] = {
            "ticker": ticker,
            "side": side,
            "count": count,
            "price": price,
            "time_in_force": time_in_force,
            "self_trade_prevention_type": self_trade_prevention_type,
        }
        if client_order_id:
            body["client_order_id"] = client_order_id
        return self.post("/portfolio/events/orders", body)

    def paginate(
        self,
        path: str,
        *,
        list_key: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            page_params = dict(params or {})
            if cursor:
                page_params["cursor"] = cursor
            data = self.get(path, page_params)
            chunk = data.get(list_key) or []
            if isinstance(chunk, list):
                items.extend(item for item in chunk if isinstance(item, dict))
            cursor = data.get("cursor") or None
            if not cursor:
                break
        return items

    def get_series(self, series_ticker: str) -> dict[str, Any] | None:
        try:
            data = self.get(f"/series/{series_ticker}")
        except requests.HTTPError as exc:
            response = exc.response
            if response is not None and response.status_code == 404:
                return None
            raise
        series = data.get("series")
        return series if isinstance(series, dict) else data

    def list_open_events(self, series_ticker: str) -> list[dict[str, Any]]:
        return self.paginate(
            "/events",
            list_key="events",
            params={
                "series_ticker": series_ticker,
                "status": "open",
                "with_nested_markets": "true",
                "limit": EVENTS_PAGE_LIMIT,
            },
        )

    def list_event_markets(self, event_ticker: str) -> list[dict[str, Any]]:
        return self.paginate(
            "/markets",
            list_key="markets",
            params={
                "event_ticker": event_ticker,
                "status": "open",
                "limit": MARKETS_PAGE_LIMIT,
            },
        )

    def get_orderbook(self, ticker: str) -> dict[str, Any]:
        return self.get(f"/markets/{ticker}/orderbook")
