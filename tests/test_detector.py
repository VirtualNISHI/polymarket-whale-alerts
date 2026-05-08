"""Detector unit tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.config import Thresholds
from src.detector import (
    classify_wallet,
    detect_large_trades,
    detect_orderbook_skew,
)


@pytest.fixture
def thresholds() -> Thresholds:
    return Thresholds.model_validate({
        "screening": {"min_volume_usd": 100_000, "top_n_markets": 50, "excluded_categories": []},
        "large_trade": {"min_trade_usd": 50_000, "min_trade_usd_hourly": 100_000},
        "orderbook_skew": {"skew_ratio": 3.0, "prob_change_1h": 0.05, "min_volume_1h": 20_000},
        "wallet_classification": {
            "smart_trader": {"min_cumulative_pnl_usd": 500_000, "min_win_rate": 0.60},
            "whale": {"min_cumulative_volume_usd": 1_000_000},
            "new": {"max_trade_count": 10},
        },
        "cache": {"wallet_ttl_seconds": 86_400},
    })


def _trade(amount, wallet="0xabc", side="YES", trade_id=None, ts=None):
    return {
        "trade_id": trade_id,
        "wallet_address": wallet,
        "amount_usd": amount,
        "side": side,
        "price": 0.55,
        "timestamp": (ts or datetime.now(timezone.utc)).isoformat(),
    }


def test_large_trade_single_hit(thresholds):
    out = detect_large_trades(
        trades=[_trade(60_000, trade_id="t1")], market_id="m1", thresholds=thresholds
    )
    assert len(out) == 1
    assert out[0].reason == "single"


def test_large_trade_below_single_above_hourly(thresholds):
    trades = [_trade(40_000, trade_id=f"t{i}") for i in range(3)]
    out = detect_large_trades(trades=trades, market_id="m1", thresholds=thresholds)
    assert len(out) == 3
    assert all(t.reason == "hourly_cumulative" for t in out)


def test_large_trade_below_both(thresholds):
    trades = [
        _trade(20_000, trade_id="t1"),
        _trade(20_000, wallet="0xother", trade_id="t2"),
    ]
    assert detect_large_trades(trades=trades, market_id="m1", thresholds=thresholds) == []


def test_large_trade_filters_old_trades(thresholds):
    """Old trades count toward hourly aggregation but don't themselves alert."""
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    trades = [_trade(80_000, trade_id="t1", ts=old)]
    out = detect_large_trades(
        trades=trades,
        market_id="m1",
        thresholds=thresholds,
        since=datetime.now(timezone.utc) - timedelta(minutes=30),
    )
    assert out == []


def test_classify_wallet_smart_trader(thresholds):
    tag, _ = classify_wallet(
        {
            "cumulative_pnl_usd": 800_000,
            "win_rate": 0.65,
            "trade_count": 100,
            "cumulative_volume_usd": 5_000_000,
        },
        thresholds,
    )
    assert tag == "Smart Trader"


def test_classify_wallet_whale(thresholds):
    tag, _ = classify_wallet(
        {
            "cumulative_pnl_usd": 200_000,
            "win_rate": 0.55,
            "trade_count": 200,
            "cumulative_volume_usd": 2_000_000,
        },
        thresholds,
    )
    assert tag == "Whale"


def test_classify_wallet_new(thresholds):
    tag, _ = classify_wallet(
        {
            "cumulative_pnl_usd": 1_000,
            "win_rate": 0.50,
            "trade_count": 5,
            "cumulative_volume_usd": 50_000,
        },
        thresholds,
    )
    assert tag == "New"


def test_classify_wallet_regular(thresholds):
    tag, _ = classify_wallet(
        {
            "cumulative_pnl_usd": 10_000,
            "win_rate": 0.50,
            "trade_count": 50,
            "cumulative_volume_usd": 100_000,
        },
        thresholds,
    )
    assert tag == "Regular"


def test_classify_wallet_handles_percent_winrate(thresholds):
    """Some APIs return win_rate as percent points (65) not fraction (0.65)."""
    tag, summary = classify_wallet(
        {
            "cumulative_pnl_usd": 800_000,
            "win_rate": 65,
            "trade_count": 100,
            "cumulative_volume_usd": 5_000_000,
        },
        thresholds,
    )
    assert tag == "Smart Trader"
    assert summary["win_rate"] == pytest.approx(0.65)


def test_orderbook_skew_detected(thresholds):
    orderbook = {
        "yes": {"bids": [{"price": 0.6, "size": 100_000}]},  # $60k YES
        "no": {"bids": [{"price": 0.4, "size": 25_000}]},     # $10k NO → 6× skew
        "mid_price": 0.6,
    }
    sig = detect_orderbook_skew(
        market_id="m1",
        orderbook=orderbook,
        prob_change_1h=0.10,  # +10pp
        current_probability=0.6,
        trades=[_trade(30_000)],
        thresholds=thresholds,
    )
    assert sig is not None
    assert sig.dominant_side == "YES"
    assert sig.skew_ratio >= 3.0
    assert sig.prob_change_1h == 0.10
    assert sig.current_probability == 0.6


def test_orderbook_skew_below_skew_ratio(thresholds):
    orderbook = {
        "yes": {"bids": [{"price": 0.6, "size": 100_000}]},
        "no": {"bids": [{"price": 0.4, "size": 100_000}]},
        "mid_price": 0.6,
    }
    sig = detect_orderbook_skew(
        market_id="m1",
        orderbook=orderbook,
        prob_change_1h=0.10,
        current_probability=0.6,
        trades=[_trade(30_000)],
        thresholds=thresholds,
    )
    assert sig is None


def test_orderbook_skew_below_prob_change(thresholds):
    orderbook = {
        "yes": {"bids": [{"price": 0.6, "size": 100_000}]},
        "no": {"bids": [{"price": 0.4, "size": 25_000}]},
        "mid_price": 0.6,
    }
    sig = detect_orderbook_skew(
        market_id="m1",
        orderbook=orderbook,
        prob_change_1h=0.01,  # +1pp only
        current_probability=0.6,
        trades=[_trade(30_000)],
        thresholds=thresholds,
    )
    assert sig is None


def test_orderbook_skew_below_volume(thresholds):
    orderbook = {
        "yes": {"bids": [{"price": 0.6, "size": 100_000}]},
        "no": {"bids": [{"price": 0.4, "size": 25_000}]},
        "mid_price": 0.6,
    }
    sig = detect_orderbook_skew(
        market_id="m1",
        orderbook=orderbook,
        prob_change_1h=0.10,
        current_probability=0.6,
        trades=[_trade(5_000)],  # below min_volume_1h
        thresholds=thresholds,
    )
    assert sig is None


def test_orderbook_skew_negative_prob_change_passes_threshold(thresholds):
    """abs() check should let bearish moves through too."""
    orderbook = {
        "yes": {"bids": [{"price": 0.6, "size": 100_000}]},
        "no": {"bids": [{"price": 0.4, "size": 25_000}]},
        "mid_price": 0.6,
    }
    sig = detect_orderbook_skew(
        market_id="m1",
        orderbook=orderbook,
        prob_change_1h=-0.10,
        current_probability=0.6,
        trades=[_trade(30_000)],
        thresholds=thresholds,
    )
    assert sig is not None
    assert sig.prob_change_1h == -0.10
