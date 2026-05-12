"""Settings (.env) and threshold (YAML) loading."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    discord_webhook_url: str = ""
    log_level: str = "INFO"
    db_path: str = "./data/whale_alerts.db"
    dry_run: bool = False
    polymarket_user_agent: str = "polymarket-whale-alerts/0.1"
    # Optional Nansen integration. If empty, Nansen calls are skipped and
    # embeds simply omit the on-chain context fields.
    nansen_api_key: str = ""
    # Lookback for Nansen on-chain PnL aggregation (days).
    nansen_pnl_lookback_days: int = 180


class ScreeningConfig(BaseModel):
    min_volume_usd: float
    top_n_markets: int
    excluded_categories: list[str] = Field(default_factory=list)


class LargeTradeConfig(BaseModel):
    min_trade_usd: float
    min_trade_usd_hourly: float


class OrderbookSkewConfig(BaseModel):
    skew_ratio: float
    prob_change_1h: float
    min_volume_1h: float


class SmartTraderConfig(BaseModel):
    min_cumulative_pnl_usd: float
    min_win_rate: float


class WhaleConfig(BaseModel):
    min_cumulative_volume_usd: float


class NewWalletConfig(BaseModel):
    max_trade_count: int


class WalletClassificationConfig(BaseModel):
    smart_trader: SmartTraderConfig
    whale: WhaleConfig
    new: NewWalletConfig


class CacheConfig(BaseModel):
    wallet_ttl_seconds: int


class Thresholds(BaseModel):
    screening: ScreeningConfig
    large_trade: LargeTradeConfig
    orderbook_skew: OrderbookSkewConfig
    wallet_classification: WalletClassificationConfig
    cache: CacheConfig


def load_thresholds(path: str | Path = "config/thresholds.yaml") -> Thresholds:
    raw: dict[str, Any] = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return Thresholds.model_validate(raw)
