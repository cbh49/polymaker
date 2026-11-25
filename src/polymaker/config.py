"""Configuration: pydantic models over local TOML + .env secrets.

  config.toml   wallet / catalog / execution / paths / sharp
  .env          POLY_PRIVATE_KEY, POLY_FUNDER, optional POLYGON_RPC_URL
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pydantic import AliasChoices, BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class WalletConfig(BaseModel):
    chain_id: int = 137
    signature_type: int = 0
    clob_host: str = "https://clob.polymarket.com"
    gamma_host: str = "https://gamma-api.polymarket.com"
    data_api_host: str = "https://data-api.polymarket.com"
    polygon_rpc: str = "https://polygon-bor-rpc.publicnode.com"


class CatalogConfig(BaseModel):
    """Market discovery defaults for `polymaker scan`."""

    include_politics: bool = True
    series_slugs: list[str] = Field(
        default_factory=lambda: [
            "mlb",
            "wnba",
            "nba",
            "nba-2026",
            "nhl",
            "nhl-2026",
            "ufc",
            "cfb-2026",
            "ncaa-cbb",
            "cbb",
            "nfl-2026",
        ]
    )
    look_ahead_days: int = 3
    skip_live_events: bool = True
    pregame_buffer_minutes: float = 5.0
    min_liquidity: float = 1000.0
    sports_rewards_only: bool = False


class ExecutionConfig(BaseModel):
    rate_budget_fraction: float = 0.25
    # True = every order posted through ExecutionGateway.place() is post-only
    # (rejected rather than filled as a taker).
    post_only: bool = True
    max_orders_per_batch: int = 15


class MakerConfig(BaseModel):
    """Resting-limit (maker) buys for the sharp pipeline. Never crosses the spread."""

    # Dollars below the best ask for the resting buy (0.01 = one cent).
    offset: float = 0.01
    # Cancel unfilled orders once the game is this close to starting.
    cancel_window_minutes: float = 30.0
    post_only: bool = True


class KalshiMakerFeeConfig(BaseModel):
    """Kalshi maker fee: ceil_to_cent(rate * C * p * (1 - p)), per order.

    `series` maps a Kalshi series ticker (the ticker prefix before the first
    dash, e.g. KXNBAGAME) to whether that series charges a maker fee. Missing
    series fall back to `default_enabled`. A series set to false pays fee = 0.
    """

    rate: float = 0.0175
    default_enabled: bool = True
    series: dict[str, bool] = Field(default_factory=dict)

    def enabled_for(self, ticker: str | None) -> bool:
        key = (ticker or "").split("-", 1)[0].strip().upper()
        for name, flag in self.series.items():
            if name.strip().upper() == key:
                return bool(flag)
        return self.default_enabled


class PathsConfig(BaseModel):
    db: str = "state.db"
    journal_dir: str = "journal"
    log_dir: str = "logs"


class SharpConfig(BaseModel):
    """Auto-trade knobs for sharp-money → Polymarket moneyline/spread/total."""

    mlb_path: str = "data-aggregation/output/mlb_sharp_money.json"
    wnba_path: str = "data-aggregation/output/wnba_sharp_money.json"
    ufc_path: str = "data-aggregation/output/ufc_sharp_money.json"
    ncaaf_path: str = "data-aggregation/output/ncaaf_sharp_money.json"
    ncaab_path: str = "data-aggregation/output/ncaab_sharp_money.json"
    nba_path: str = "data-aggregation/output/nba_sharp_money.json"
    nhl_path: str = "data-aggregation/output/nhl_sharp_money.json"
    nfl_path: str = "data-aggregation/output/nfl_sharp_money.json"
    usd_tier_a: float = 25.0  # A/A+ stake; NFL totals and NHL are $10; NCAAB totals are $5; to-win for +200 ML dogs
    usd_tier_b: float = 10.0
    min_tier: str = "B"  # "A" = Tier A only; "B" = A+B
    markets: list[str] = Field(default_factory=lambda: ["moneyline", "spread", "total"])
    venues: list[str] = Field(default_factory=lambda: ["polymarket", "kalshi"])
    tie_venue: str = "polymarket"
    require_rlm: bool = False
    max_ask: float = 0.60  # skip if best ask / last price is above this
    min_moneyline_ask: float = 0.35  # skip moneyline if best ask is cheaper than this
    # If set, require best ask <= implied_fair_prob - min_edge
    min_edge: float | None = None
    filled_log: str = "journal/sharp_trades.jsonl"
    resting_log: str = "journal/resting_orders.json"
    maker_fills_log: str = "journal/maker_fills.jsonl"


class Secrets(BaseSettings):
    """Loaded from environment / .env. Never written to disk by us."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    pk: str = Field(
        default="",
        validation_alias=AliasChoices("PK", "POLY_PRIVATE_KEY"),
    )
    browser_address: str = Field(
        default="",
        validation_alias=AliasChoices("BROWSER_ADDRESS", "POLY_FUNDER"),
    )
    polygon_rpc: str | None = Field(
        default=None,
        validation_alias=AliasChoices("POLYGON_RPC", "POLYGON_RPC_URL"),
    )
    alert_webhook_url: str | None = Field(default=None, alias="ALERT_WEBHOOK_URL")

    @property
    def has_wallet(self) -> bool:
        return bool(self.pk and self.browser_address)


class Config(BaseModel):
    """Fully-resolved configuration tree."""

    wallet: WalletConfig = WalletConfig()
    catalog: CatalogConfig = CatalogConfig()
    execution: ExecutionConfig = ExecutionConfig()
    paths: PathsConfig = PathsConfig()
    sharp: SharpConfig = SharpConfig()
    maker: MakerConfig = MakerConfig()
    kalshi_maker_fees: KalshiMakerFeeConfig = KalshiMakerFeeConfig()
    secrets: Secrets = Field(default_factory=Secrets)
    config_dir: Path = Path("config")

    @property
    def proxy(self) -> str | None:
        return os.environ.get("ALL_PROXY") or os.environ.get("HTTPS_PROXY")

    @classmethod
    def load(cls, config_dir: str | Path = "config", *, load_env: bool = True) -> Config:
        cdir = Path(config_dir)
        if load_env:
            load_dotenv()
        main = _read_toml(cdir / "config.toml")
        return cls(
            wallet=WalletConfig(**main.get("wallet", {})),
            catalog=CatalogConfig(**main.get("catalog", {})),
            execution=ExecutionConfig(**main.get("execution", {})),
            paths=PathsConfig(**main.get("paths", {})),
            sharp=SharpConfig(**main.get("sharp", {})),
            maker=MakerConfig(**main.get("maker", {})),
            kalshi_maker_fees=KalshiMakerFeeConfig(**main.get("kalshi_maker_fees", {})),
            secrets=Secrets(),
            config_dir=cdir,
        )


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh)
