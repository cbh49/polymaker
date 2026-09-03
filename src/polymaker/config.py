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
    series_slugs: list[str] = Field(default_factory=lambda: ["mlb", "wnba", "ufc", "cfb-2026"])
    look_ahead_days: int = 3
    skip_live_events: bool = True
    pregame_buffer_minutes: float = 5.0
    min_liquidity: float = 1000.0
    sports_rewards_only: bool = False


class ExecutionConfig(BaseModel):
    rate_budget_fraction: float = 0.25
    # False = allow taker (market) buys; set True only if you want post-only limits.
    post_only: bool = False
    max_orders_per_batch: int = 15


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
    usd_tier_a: float = 25.0  # stake for A/A+; to-win target for +200 ML dogs
    usd_tier_b: float = 10.0
    min_tier: str = "B"  # "A" = Tier A only; "B" = A+B
    markets: list[str] = Field(default_factory=lambda: ["moneyline", "spread", "total"])
    require_rlm: bool = False
    max_ask: float = 0.55  # skip if best ask / last price is above this
    # If set, require best ask <= implied_fair_prob - min_edge
    min_edge: float | None = None
    filled_log: str = "journal/sharp_trades.jsonl"


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
            secrets=Secrets(),
            config_dir=cdir,
        )


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh)
