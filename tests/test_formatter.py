"""Formatter unit tests."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.detector import LargeTrade, OrderbookSkewSignal
from src.formatter import (
    format_large_trade_embed,
    format_orderbook_skew_embed,
    nansen_wallet_url,
    short_address,
)


def _embed_text(embed: dict[str, Any]) -> str:
    """Concatenate every visible string in an embed for substring assertions."""
    parts = [embed.get("title", "")]
    if embed.get("description"):
        parts.append(embed["description"])
    for f in embed.get("fields", []):
        parts.append(f.get("name", ""))
        parts.append(f.get("value", ""))
    return "\n".join(parts)


def test_short_address():
    assert short_address("0x1234567890abcdef1234567890abcdef12345678") == "0x1234…5678"
    assert short_address("") == ""
    assert short_address("0xabc") == "0xabc"


def test_nansen_wallet_url():
    assert nansen_wallet_url("0xabc").endswith("/0xabc")
    assert nansen_wallet_url("") == ""


def test_large_trade_embed_smart_trader():
    trade = LargeTrade(
        trade_id="t1",
        market_id="m1",
        wallet_address="0xabcdef0123456789abcdef0123456789abcdef01",
        side="BUY YES",
        amount_usd=75_000,
        price=0.62,
        timestamp=datetime.now(timezone.utc),
        reason="single",
        raw={},
    )
    embed = format_large_trade_embed(
        market={"market_id": "m1", "slug": "will-foo", "question": "Will Foo?"},
        trade=trade,
        wallet_tag="Smart Trader",
        wallet_summary={"cumulative_pnl_usd": 800_000, "win_rate": 0.65, "trade_count": 100},
        current_probability=0.62,
        delta_24h=0.05,
    )
    assert embed["color"] == 0x2ECC71
    assert "Whale Trade" in embed["title"]

    text = _embed_text(embed)
    assert "Will Foo?" in text
    assert "$75,000" in text
    assert "BUY YES" in text
    assert "Smart Trader" in text
    assert "🟢" in text  # tag emoji + positive delta
    assert "↑5.0%pt" in text
    assert "65%" in text  # win rate
    assert "polymarket.com/event/will-foo" in text  # View Market link
    assert "polymarket.com/event/will-foo" in embed["url"]


def test_large_trade_embed_negative_delta_uses_red_arrow():
    trade = LargeTrade(
        trade_id="t1", market_id="m1", wallet_address="0xabc",
        side="SELL NO", amount_usd=60_000, price=0.4,
        timestamp=datetime.now(timezone.utc), reason="single", raw={},
    )
    embed = format_large_trade_embed(
        market={"market_id": "m1", "question": "Q?"},
        trade=trade, wallet_tag="Whale",
        wallet_summary={"cumulative_pnl_usd": 0, "win_rate": 0, "trade_count": 0},
        current_probability=0.40, delta_24h=-0.03,
    )
    text = _embed_text(embed)
    assert "🔴" in text
    assert "↓3.0%pt" in text


def test_large_trade_embed_color_per_tag():
    trade = LargeTrade(
        trade_id=None, market_id="m1", wallet_address="0xabc",
        side="BUY NO", amount_usd=60_000, price=None,
        timestamp=datetime.now(timezone.utc), reason="single", raw={},
    )
    market = {"market_id": "m1", "question": "Q?"}
    summary = {"cumulative_pnl_usd": 0, "win_rate": 0, "trade_count": 0}

    colors = {
        "Smart Trader": 0x2ECC71,
        "Whale": 0x3498DB,
        "New": 0xF39C12,
        "Regular": 0x95A5A6,
    }
    for tag, color in colors.items():
        embed = format_large_trade_embed(
            market=market, trade=trade, wallet_tag=tag, wallet_summary=summary,
            current_probability=0.5, delta_24h=0.0,
        )
        assert embed["color"] == color


def test_orderbook_skew_embed():
    sig = OrderbookSkewSignal(
        market_id="m1",
        dominant_side="YES",
        skew_ratio=4.2,
        prob_change_1h=0.07,
        current_probability=0.62,
        volume_1h_usd=45_000,
        raw={},
    )
    embed = format_orderbook_skew_embed(
        market={"market_id": "m1", "slug": "will-foo", "question": "Will Foo?"},
        signal=sig,
    )
    assert embed["color"] == 0xE67E22
    assert "Orderbook Skew" in embed["title"]

    text = _embed_text(embed)
    assert "Will Foo?" in text
    assert "YES side 4.2×" in text
    assert "$45,000" in text
    assert "Anomaly" in text
    assert "↑7.0%pt" in text
