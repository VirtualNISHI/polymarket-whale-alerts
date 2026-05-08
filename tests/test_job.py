"""Integration tests for job._process_market with a fake Polymarket client."""
from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from src.config import Thresholds
from src.db import Database
from src.job import Summary, _process_market


class FakePolymarket:
    """Minimal stand-in for PolymarketClient. Only the methods _process_market
    actually calls are implemented."""

    def __init__(
        self,
        *,
        trades: list[dict[str, Any]] | None = None,
        orderbook: dict[str, Any] | None = None,
        pnl: dict[str, Any] | None = None,
    ) -> None:
        self._trades = trades or []
        self._orderbook = orderbook if orderbook is not None else {}
        self._pnl = pnl or {}
        self.pnl_calls = 0

    async def get_recent_trades(
        self, *, condition_id: str, since: datetime | None = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        return self._trades

    async def get_orderbook(
        self, *, yes_token_id: str | None, no_token_id: str | None
    ) -> dict[str, Any]:
        return self._orderbook

    async def get_wallet_pnl_summary(self, *, address: str) -> dict[str, Any]:
        self.pnl_calls += 1
        return self._pnl


@pytest.fixture
def thresholds() -> Thresholds:
    return Thresholds.model_validate({
        "screening": {"min_volume_usd": 100_000, "top_n_markets": 50, "excluded_categories": []},
        "large_trade": {"min_trade_usd": 50_000, "min_trade_usd_hourly": 100_000},
        "orderbook_skew": {"skew_ratio": 3.0, "prob_change_1h": 0.05, "min_volume_1h": 20_000},
        "wallet_classification": {
            "smart_trader": {"min_cumulative_pnl_usd": 100_000, "min_win_rate": 0.60},
            "whale": {"min_cumulative_volume_usd": 500_000},
            "new": {"max_trade_count": 10},
        },
        "cache": {"wallet_ttl_seconds": 86_400},
    })


def _fresh_db() -> Database:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db = Database(tmp.name)
    db.init_schema()
    return db


def _trade(amount: float, wallet: str = "0xa", trade_id: str | None = None) -> dict[str, Any]:
    return {
        "trade_id": trade_id,
        "wallet_address": wallet,
        "amount_usd": amount,
        "side": "BUY YES",
        "price": 0.55,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _market(**overrides: Any) -> dict[str, Any]:
    base = {
        "market_id": "m1",
        "slug": "will-foo",
        "question": "Will Foo?",
        "current_probability": 0.55,
        "probability_change_24h": 0.03,
        "probability_change_1h": 0.10,
        "yes_token_id": "tok-yes",
        "no_token_id": "tok-no",
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_large_trade_detected_notified_recorded_and_deduped(thresholds: Thresholds) -> None:
    db = _fresh_db()
    client = FakePolymarket(
        trades=[_trade(75_000, trade_id="t1")],
        pnl={
            "cumulative_pnl_usd": 200_000,
            "win_rate": 0.65,
            "trade_count": 100,
            "cumulative_volume_usd": 1_000_000,
        },
    )
    discord = _Discord()
    summary = Summary()
    market = _market()
    since = datetime.now(timezone.utc) - timedelta(hours=1)

    await _process_market(
        market=market, client=client, db=db, discord=discord,
        thresholds=thresholds, since=since, dry_run=False, summary=summary,
    )

    assert summary.detected == 1
    assert summary.notified == 1
    assert len(discord.sent) == 1
    embed = discord.sent[0]
    assert "Whale Trade" in embed["title"]
    field_text = "\n".join(f.get("value", "") for f in embed["fields"])
    assert "Smart Trader" in field_text
    assert "$75,000" in field_text

    # Re-run with same trade — should be deduped via trade_id
    await _process_market(
        market=market, client=client, db=db, discord=discord,
        thresholds=thresholds, since=since, dry_run=False, summary=summary,
    )
    assert summary.notified == 1   # no new notification
    assert summary.skipped == 1    # one skip recorded
    assert len(discord.sent) == 1


@pytest.mark.asyncio
async def test_dry_run_skips_discord_and_db(thresholds: Thresholds) -> None:
    db = _fresh_db()
    client = FakePolymarket(
        trades=[_trade(75_000, trade_id="t1")],
        pnl={"cumulative_pnl_usd": 200_000, "win_rate": 0.65, "trade_count": 100, "cumulative_volume_usd": 1_000_000},
    )
    discord = _Discord()
    summary = Summary()
    since = datetime.now(timezone.utc) - timedelta(hours=1)

    await _process_market(
        market=_market(), client=client, db=db, discord=discord,
        thresholds=thresholds, since=since, dry_run=True, summary=summary,
    )

    assert summary.detected == 1
    assert summary.notified == 1
    assert discord.sent == []
    assert not db.is_trade_alerted("t1")


@pytest.mark.asyncio
async def test_wallet_cache_avoids_repeat_pnl_calls(thresholds: Thresholds) -> None:
    db = _fresh_db()
    client = FakePolymarket(
        trades=[
            _trade(60_000, wallet="0xsame", trade_id="t1"),
            _trade(60_000, wallet="0xsame", trade_id="t2"),
        ],
        pnl={"cumulative_pnl_usd": 200_000, "win_rate": 0.65, "trade_count": 100, "cumulative_volume_usd": 1_000_000},
    )
    discord = _Discord()
    summary = Summary()
    since = datetime.now(timezone.utc) - timedelta(hours=1)

    await _process_market(
        market=_market(), client=client, db=db, discord=discord,
        thresholds=thresholds, since=since, dry_run=False, summary=summary,
    )

    assert summary.notified == 2
    assert client.pnl_calls == 1, "second trade for same wallet should hit cache"


@pytest.mark.asyncio
async def test_orderbook_skew_alert_dedups_within_window(thresholds: Thresholds) -> None:
    db = _fresh_db()
    client = FakePolymarket(
        trades=[_trade(30_000, wallet="0xq", trade_id="t1")],  # below large-trade threshold
        orderbook={
            "yes": {"bids": [{"price": 0.6, "size": 100_000}]},  # $60k
            "no": {"bids": [{"price": 0.4, "size": 25_000}]},     # $10k → 6× skew
            "mid_price": 0.6,
        },
    )
    discord = _Discord()
    summary = Summary()
    market = _market(probability_change_1h=0.10)
    since = datetime.now(timezone.utc) - timedelta(hours=1)

    await _process_market(
        market=market, client=client, db=db, discord=discord,
        thresholds=thresholds, since=since, dry_run=False, summary=summary,
    )
    assert summary.notified == 1
    assert "Orderbook Skew" in discord.sent[0]["title"]

    # Re-run — should be deduped within 1h window
    await _process_market(
        market=market, client=client, db=db, discord=discord,
        thresholds=thresholds, since=since, dry_run=False, summary=summary,
    )
    assert summary.notified == 1
    assert summary.skipped == 1


@pytest.mark.asyncio
async def test_skips_market_with_no_id(thresholds: Thresholds) -> None:
    db = _fresh_db()
    client = FakePolymarket()
    discord = _Discord()
    summary = Summary()
    since = datetime.now(timezone.utc) - timedelta(hours=1)

    await _process_market(
        market={"slug": "no-id"}, client=client, db=db, discord=discord,
        thresholds=thresholds, since=since, dry_run=False, summary=summary,
    )
    assert summary.skipped == 1
    assert summary.detected == 0


class _Discord:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_embed(self, embed: dict[str, Any]) -> None:
        self.sent.append(embed)
