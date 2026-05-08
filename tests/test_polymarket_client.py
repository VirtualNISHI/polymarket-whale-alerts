"""Tests for polymarket_client normalization helpers (no network)."""
from __future__ import annotations

import json

import pytest

from src.polymarket_client import (
    PolymarketClient,
    _mid_from_book,
    _normalize_market,
    _parse_string_array,
    _to_float,
)


def test_parse_string_array_handles_json_string():
    assert _parse_string_array('["0.6234","0.3766"]') == ["0.6234", "0.3766"]


def test_parse_string_array_handles_native_list():
    assert _parse_string_array(["a", "b"]) == ["a", "b"]


def test_parse_string_array_returns_empty_on_garbage():
    assert _parse_string_array(None) == []
    assert _parse_string_array("not json") == []
    assert _parse_string_array(42) == []


def test_to_float_handles_strings_and_none():
    assert _to_float("1.5") == 1.5
    assert _to_float(None) == 0.0
    assert _to_float("") == 0.0
    assert _to_float("nope") == 0.0


def test_normalize_market_extracts_tokens_and_prices():
    market = {
        "conditionId": "0xcond",
        "slug": "will-x",
        "question": "Will X?",
        "outcomePrices": json.dumps(["0.62", "0.38"]),
        "clobTokenIds": json.dumps(["yes-token", "no-token"]),
        "volume24hr": "150000",
        "oneDayPriceChange": "0.04",
        "oneHourPriceChange": "-0.02",
    }
    event = {"category": "Politics"}
    out = _normalize_market(market, event)
    assert out is not None
    assert out["market_id"] == "0xcond"
    assert out["yes_token_id"] == "yes-token"
    assert out["no_token_id"] == "no-token"
    assert out["current_probability"] == 0.62
    assert out["volume_24h"] == 150000.0
    assert out["probability_change_24h"] == 0.04
    assert out["probability_change_1h"] == -0.02
    assert out["category"] == "Politics"


def test_normalize_market_returns_none_without_condition_id():
    assert _normalize_market({"slug": "foo"}, {}) is None


def test_mid_from_book_average_when_both_sides_present():
    bids = [{"price": "0.5", "size": "100"}]
    asks = [{"price": "0.6", "size": "100"}]
    assert _mid_from_book(bids, asks) == pytest.approx(0.55)


def test_mid_from_book_falls_back_to_one_side():
    assert _mid_from_book([{"price": "0.5", "size": "1"}], []) == 0.5
    assert _mid_from_book([], [{"price": "0.6", "size": "1"}]) == 0.6
    assert _mid_from_book([], []) == 0.0


@pytest.mark.asyncio
async def test_get_recent_trades_normalizes_fields(monkeypatch):
    """get_recent_trades should compute amount_usd, lowercase wallet, build side label."""
    client = PolymarketClient()
    raw = [
        {
            "proxyWallet": "0xABCDEF",
            "side": "BUY",
            "outcome": "Yes",
            "size": 1000,
            "price": 0.62,
            "timestamp": 1_700_000_000,
            "transactionHash": "0xtx1",
            "conditionId": "0xcond",
        },
    ]

    async def fake_get(self, url, params=None):
        return raw

    monkeypatch.setattr(PolymarketClient, "_get", fake_get)
    out = await client.get_recent_trades(condition_id="0xcond")
    await client.aclose()

    assert len(out) == 1
    t = out[0]
    assert t["amount_usd"] == pytest.approx(620.0)
    assert t["wallet_address"] == "0xabcdef"
    assert t["side"] == "BUY YES"
    assert t["trade_id"] == "0xtx1"


@pytest.mark.asyncio
async def test_get_recent_trades_filters_by_since(monkeypatch):
    from datetime import datetime, timezone
    client = PolymarketClient()
    raw = [
        {"proxyWallet": "0x1", "side": "BUY", "outcome": "YES",
         "size": 100, "price": 0.5, "timestamp": 1_700_000_000,
         "transactionHash": "old"},
        {"proxyWallet": "0x2", "side": "BUY", "outcome": "YES",
         "size": 100, "price": 0.5, "timestamp": 1_800_000_000,
         "transactionHash": "new"},
    ]

    async def fake_get(self, url, params=None):
        return raw

    monkeypatch.setattr(PolymarketClient, "_get", fake_get)
    cutoff = datetime.fromtimestamp(1_750_000_000, tz=timezone.utc)
    out = await client.get_recent_trades(condition_id="x", since=cutoff)
    await client.aclose()

    assert [t["trade_id"] for t in out] == ["new"]


@pytest.mark.asyncio
async def test_get_wallet_pnl_summary_aggregates(monkeypatch):
    client = PolymarketClient()
    raw = [
        {"cashPnl": 50_000, "totalBought": 200_000, "initialValue": 200_000},
        {"cashPnl": -10_000, "totalBought": 50_000, "initialValue": 50_000},
        {"cashPnl": 30_000, "totalBought": 100_000, "initialValue": 100_000},
    ]

    async def fake_get(self, url, params=None):
        return raw

    monkeypatch.setattr(PolymarketClient, "_get", fake_get)
    out = await client.get_wallet_pnl_summary(address="0xabc")
    await client.aclose()

    assert out["cumulative_pnl_usd"] == 70_000
    assert out["cumulative_volume_usd"] == 350_000
    assert out["trade_count"] == 3
    assert out["win_rate"] == pytest.approx(2 / 3)


@pytest.mark.asyncio
async def test_list_active_markets_filters_and_flattens(monkeypatch):
    client = PolymarketClient()
    events = [
        {
            "category": "Politics",
            "markets": [
                {
                    "conditionId": "0xa",
                    "slug": "high-vol",
                    "question": "A?",
                    "outcomePrices": '["0.7","0.3"]',
                    "clobTokenIds": '["ya","na"]',
                    "volume24hr": "300000",
                    "active": True,
                    "closed": False,
                    "enableOrderBook": True,
                },
                {
                    "conditionId": "0xb",
                    "slug": "low-vol",
                    "question": "B?",
                    "outcomePrices": '["0.5","0.5"]',
                    "clobTokenIds": '["yb","nb"]',
                    "volume24hr": "5000",  # filtered out
                    "active": True,
                    "closed": False,
                    "enableOrderBook": True,
                },
                {
                    "conditionId": "0xc",
                    "slug": "amm-only",
                    "question": "C?",
                    "outcomePrices": '["0.5","0.5"]',
                    "clobTokenIds": '[]',
                    "volume24hr": "500000",
                    "active": True,
                    "closed": False,
                    "enableOrderBook": False,  # filtered out
                },
            ],
        },
        {
            "category": "Sports",  # category-excluded
            "markets": [
                {
                    "conditionId": "0xd",
                    "slug": "sports",
                    "question": "D?",
                    "outcomePrices": '["0.5","0.5"]',
                    "clobTokenIds": '["yd","nd"]',
                    "volume24hr": "1000000",
                    "active": True,
                    "closed": False,
                    "enableOrderBook": True,
                },
            ],
        },
    ]

    async def fake_get(self, url, params=None):
        return events

    monkeypatch.setattr(PolymarketClient, "_get", fake_get)
    out = await client.list_active_markets(
        min_volume_usd=100_000, limit=10, excluded_categories=["Sports"]
    )
    await client.aclose()

    assert [m["market_id"] for m in out] == ["0xa"]
