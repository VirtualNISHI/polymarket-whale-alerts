"""Discord embed builders for whale-trade and orderbook-skew alerts."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.detector import LargeTrade, OrderbookSkewSignal


WALLET_TAG_COLOR = {
    "Smart Trader": 0x2ECC71,
    "Whale": 0x3498DB,
    "New": 0xF39C12,
    "Regular": 0x95A5A6,
}

# Coloured circle emojis used as inline pills next to the wallet tag and as
# directional indicators next to probability deltas.
WALLET_TAG_EMOJI = {
    "Smart Trader": "🟢",
    "Whale": "🔵",
    "New": "🟠",
    "Regular": "⚪",
}

ORDERBOOK_SKEW_COLOR = 0xE67E22
ZERO_WIDTH = "​"


def short_address(addr: str) -> str:
    if not addr:
        return ""
    if len(addr) <= 10:
        return addr
    return f"{addr[:6]}…{addr[-4:]}"


def market_url(market: dict[str, Any]) -> str:
    slug = market.get("slug") or market.get("market_slug")
    if slug:
        return f"https://polymarket.com/event/{slug}"
    market_id = market.get("market_id") or market.get("id") or ""
    return f"https://polymarket.com/markets/{market_id}" if market_id else ""


def nansen_wallet_url(address: str) -> str:
    return f"https://app.nansen.ai/profiler/{address}" if address else ""


def _market_question(market: dict[str, Any]) -> str:
    return (
        market.get("question")
        or market.get("title")
        or market.get("name")
        or "Unknown market"
    )


def _delta_str(delta: float, *, suffix: str = "%pt") -> str:
    """Render a probability delta with arrow + colour emoji."""
    pct = delta * 100
    if pct > 0:
        return f"🟢 ↑{pct:.1f}{suffix}"
    if pct < 0:
        return f"🔴 ↓{abs(pct):.1f}{suffix}"
    return f"⚪ 0.0{suffix}"


def format_large_trade_embed(
    *,
    market: dict[str, Any],
    trade: LargeTrade,
    wallet_tag: str,
    wallet_summary: dict[str, Any],
    current_probability: float,
    delta_24h: float,
) -> dict[str, Any]:
    color = WALLET_TAG_COLOR.get(wallet_tag, WALLET_TAG_COLOR["Regular"])
    tag_emoji = WALLET_TAG_EMOJI.get(wallet_tag, WALLET_TAG_EMOJI["Regular"])

    cum_pnl = float(wallet_summary.get("cumulative_pnl_usd") or 0.0)
    win_rate = float(wallet_summary.get("win_rate") or 0.0)
    trade_count = int(wallet_summary.get("trade_count") or 0)

    prob_pct = current_probability * 100
    prob_value = f"**{prob_pct:.0f}%** {_delta_str(delta_24h)}"

    trade_value = f"**{trade.side} ${trade.amount_usd:,.0f}**"
    if trade.price is not None:
        trade_value += f" @ ${trade.price:.3f}"

    wallet_display = short_address(trade.wallet_address)
    nansen_url = nansen_wallet_url(trade.wallet_address)
    wallet_value = f"[`{wallet_display}`]({nansen_url})" if nansen_url else f"`{wallet_display}`"

    tag_value = f"{tag_emoji} **{wallet_tag}**"

    market_link = market_url(market)
    stats_line = (
        f"Cumulative PnL: **${cum_pnl:,.0f}** · "
        f"Win Rate: **{win_rate:.0%}** over **{trade_count}** trades"
    )
    link_parts: list[str] = []
    if market_link:
        link_parts.append(f"[View Market]({market_link})")
    if nansen_url:
        link_parts.append(f"[View Wallet]({nansen_url})")
    footer_value = stats_line + ("\n\n" + " · ".join(link_parts) if link_parts else "")

    embed: dict[str, Any] = {
        "title": "🐋 Whale Trade | Polymarket",
        "color": color,
        "fields": [
            {"name": "Market", "value": _market_question(market), "inline": False},
            {"name": "Current probability", "value": prob_value, "inline": True},
            {"name": "Trade", "value": trade_value, "inline": True},
            {"name": "Wallet", "value": wallet_value, "inline": True},
            {"name": "Tag", "value": tag_value, "inline": True},
            {"name": ZERO_WIDTH, "value": footer_value, "inline": False},
        ],
        "timestamp": _isoformat(trade.timestamp),
    }
    if market_link:
        embed["url"] = market_link
    return embed


def format_orderbook_skew_embed(
    *,
    market: dict[str, Any],
    signal: OrderbookSkewSignal,
) -> dict[str, Any]:
    prob_pct = signal.current_probability * 100
    prob_value = f"**{prob_pct:.0f}%** {_delta_str(signal.prob_change_1h)} 1h"
    skew_value = f"**{signal.dominant_side} side {signal.skew_ratio:.1f}× deeper**"
    volume_value = f"**${signal.volume_1h_usd:,.0f}**"
    status_value = "🟠 **Anomaly**"

    market_link = market_url(market)
    footer_value = f"[View Market]({market_link})" if market_link else ZERO_WIDTH

    embed: dict[str, Any] = {
        "title": "⚖️ Orderbook Skew | Polymarket",
        "color": ORDERBOOK_SKEW_COLOR,
        "fields": [
            {"name": "Market", "value": _market_question(market), "inline": False},
            {"name": "Probability", "value": prob_value, "inline": True},
            {"name": "Liquidity skew", "value": skew_value, "inline": True},
            {"name": "Volume (1h)", "value": volume_value, "inline": True},
            {"name": "Status", "value": status_value, "inline": True},
            {"name": ZERO_WIDTH, "value": footer_value, "inline": False},
        ],
        "timestamp": _isoformat(datetime.now(timezone.utc)),
    }
    if market_link:
        embed["url"] = market_link
    return embed


def _isoformat(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.isoformat()
