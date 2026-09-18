"""EV-trading knobs: optional `[ev]` in config.toml plus API keys from .env."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pydantic import AliasChoices, BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from ev_trading.models import Venue

DEFAULT_VENUES: tuple[Venue, ...] = (
    Venue.POLYMARKET,
    Venue.KALSHI,
    Venue.PROPHETX,
    Venue.SXBET,
)


class EvSecrets(BaseSettings):
    """Filled in when venue docs land. Public reads may not need any of these."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    kalshi_api_key_id: str = Field(default="", validation_alias=AliasChoices("KALSHI_API_KEY_ID"))
    kalshi_private_key: str = Field(default="", validation_alias=AliasChoices("KALSHI_PRIVATE_KEY"))
    prophetx_api_key: str = Field(default="", validation_alias=AliasChoices("PROPHETX_API_KEY"))
    sxbet_api_key: str = Field(default="", validation_alias=AliasChoices("SXBET_API_KEY"))


class EvConfig(BaseModel):
    out_dir: str = "output/ev"
    min_spread: float = 0.03
    fetch_limit: int = 400
    venues: list[str] = Field(default_factory=lambda: [v.value for v in DEFAULT_VENUES])
    polymarket_series: list[str] = Field(default_factory=list)
    secrets: EvSecrets = Field(default_factory=EvSecrets)
    config_dir: Path = Path("config")

    @property
    def venue_list(self) -> tuple[Venue, ...]:
        out: list[Venue] = []
        for name in self.venues:
            key = name.strip().lower()
            if key:
                out.append(Venue(key))
        return tuple(out) or DEFAULT_VENUES

    @classmethod
    def load(cls, config_dir: str | Path = "config", *, load_env: bool = True) -> EvConfig:
        if load_env:
            load_dotenv()
        cdir = Path(config_dir)
        main = _read_toml(cdir / "config.toml")
        block = main.get("ev", {})
        catalog = main.get("catalog", {})
        series = list(block.get("polymarket_series") or catalog.get("series_slugs") or [])
        return cls(
            out_dir=block.get("out_dir", "output/ev"),
            min_spread=float(block.get("min_spread", 0.03)),
            fetch_limit=int(block.get("fetch_limit", 400)),
            venues=list(block.get("venues") or [v.value for v in DEFAULT_VENUES]),
            polymarket_series=series,
            secrets=EvSecrets(),
            config_dir=cdir,
        )


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh)
