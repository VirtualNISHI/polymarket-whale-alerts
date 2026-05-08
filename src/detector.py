"""Threshold-based detection: large trades, orderbook skew, wallet classification."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from src.config import Thresholds


@dataclass
class LargeTrade:
    trade_id: str | None
    market_id: str
    wallet_address: str
    side: str
    amount_usd: float
    price: float | None
    timestamp: datetime
    reason: str  # 'single' or 'hourly_cumulative'
    raw: dict[str, Any]


@dataclass
class OrderbookSkewSignal:
    market_id: str
    dominant_side: str  # 'YES' or 'NO'
    skew_ratio: float
    prob_change_1h: float
    current_probability: float
    volume_1h_usd: float
    raw: dict[str, Any]


def detect_large_trades(
    *,
    trades: Iterable[dict[str, Any]],
    market_id: str,
    thresholds: Thresholds,
    since: datetime | None = None,
) -> list[LargeTrade]:
    """Detect trades that are individually large or push wallet hourly cumulative over the line.

    `trades` should already cover at least the last hour so hourly aggregation is meaningful.
    `since` filters which trades are eligible to alert on (typically the last poll time).
    """
    cfg = thresholds.large_trade
    trades_list = list(trades)

    by_wallet_hourly: dict[str, float] = defaultdict(float)
    for t in trades_list:
        wallet = _trade_wallet(t)
        if wallet:
            by_wallet_hourly[wallet] += _trade_amount_usd(t)

    results: list[LargeTrade] = []
    for t in trades_list:
        ts = _trade_timestamp(t)
        if since and ts and ts < since:
            continue

        wallet = _trade_wallet(t)
        if not wallet:
            continue

        amount = _trade_amount_usd(t)
        single_hit = amount >= cfg.min_trade_usd
        hourly_hit = by_wallet_hourly[wallet] >= cfg.min_trade_usd_hourly

        if not (single_hit or hourly_hit):
            continue

        results.append(
            LargeTrade(
                trade_id=_trade_id(t),
                market_id=market_id,
                wallet_address=wallet,
                side=_trade_side(t),
                amount_usd=amount,
                price=_trade_price(t),
                timestamp=ts or datetime.now(timezone.utc),
                reason="single" if single_hit else "hourly_cumulative",
                raw=t,
            )
        )
    return results


def detect_orderbook_skew(
    *,
    market_id: str,
    orderbook: dict[str, Any],
    prob_change_1h: float,
    current_probability: float,
    trades: Iterable[dict[str, Any]],
    thresholds: Thresholds,
) -> OrderbookSkewSignal | None:
    """Flag liquidity-skew + price-move + volume confluence on a market.

    `prob_change_1h` and `current_probability` come from the caller (Polymarket
    exposes both on the market metadata as `oneHourPriceChange` and the YES
    `outcomePrices`, so we don't need a separate OHLCV call and don't have to
    derive probability from the orderbook mid which can be unreliable on
    one-sided books). Both are in price units (0..1).
    """
    cfg = thresholds.orderbook_skew

    yes_liq = _orderbook_side_liquidity(orderbook, "YES")
    no_liq = _orderbook_side_liquidity(orderbook, "NO")
    if yes_liq <= 0 or no_liq <= 0:
        return None

    if yes_liq >= no_liq:
        dominant, skew = "YES", yes_liq / no_liq
    else:
        dominant, skew = "NO", no_liq / yes_liq

    if skew < cfg.skew_ratio:
        return None

    if abs(prob_change_1h) < cfg.prob_change_1h:
        return None

    volume_1h = sum(_trade_amount_usd(t) for t in trades)
    if volume_1h < cfg.min_volume_1h:
        return None

    return OrderbookSkewSignal(
        market_id=market_id,
        dominant_side=dominant,
        skew_ratio=skew,
        prob_change_1h=prob_change_1h,
        current_probability=current_probability,
        volume_1h_usd=volume_1h,
        raw=orderbook,
    )


def classify_wallet(
    pnl_data: dict[str, Any],
    thresholds: Thresholds,
) -> tuple[str, dict[str, float | int]]:
    """Return (tag, summary). Summary keys: cumulative_pnl_usd, win_rate, trade_count."""
    cfg = thresholds.wallet_classification

    cum_pnl = float(pnl_data.get("cumulative_pnl_usd") or pnl_data.get("realized_pnl_usd") or 0.0)
    win_rate = float(pnl_data.get("win_rate") or 0.0)
    if win_rate > 1.5:  # some APIs return percent points
        win_rate /= 100.0
    trade_count = int(pnl_data.get("trade_count") or 0)
    cum_volume = float(
        pnl_data.get("cumulative_volume_usd") or pnl_data.get("volume_usd") or 0.0
    )

    if trade_count < cfg.new.max_trade_count:
        tag = "New"
    elif (
        cum_pnl >= cfg.smart_trader.min_cumulative_pnl_usd
        and win_rate >= cfg.smart_trader.min_win_rate
    ):
        tag = "Smart Trader"
    elif cum_volume >= cfg.whale.min_cumulative_volume_usd:
        tag = "Whale"
    else:
        tag = "Regular"

    return tag, {
        "cumulative_pnl_usd": cum_pnl,
        "win_rate": win_rate,
        "trade_count": trade_count,
    }


# ----- response field helpers (tolerate schema variations) -----

def _trade_id(t: dict[str, Any]) -> str | None:
    for k in ("trade_id", "id", "tx_hash", "hash"):
        v = t.get(k)
        if v:
            return str(v)
    return None


def _trade_wallet(t: dict[str, Any]) -> str | None:
    for k in ("wallet_address", "address", "trader", "user_address", "from"):
        v = t.get(k)
        if v:
            return str(v).lower()
    return None


def _trade_amount_usd(t: dict[str, Any]) -> float:
    for k in ("amount_usd", "size_usd", "volume_usd", "usd_amount", "notional_usd"):
        v = t.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return 0.0


def _trade_side(t: dict[str, Any]) -> str:
    for k in ("side", "outcome", "direction"):
        v = t.get(k)
        if v:
            return str(v).upper()
    return ""


def _trade_price(t: dict[str, Any]) -> float | None:
    for k in ("price", "entry_price", "fill_price"):
        v = t.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


def _trade_timestamp(t: dict[str, Any]) -> datetime | None:
    for k in ("timestamp", "ts", "time", "executed_at", "created_at"):
        v = t.get(k)
        if v is None:
            continue
        if isinstance(v, (int, float)):
            ts = v / 1000.0 if v > 1e12 else v
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        if isinstance(v, str):
            try:
                return datetime.fromisoformat(v.replace("Z", "+00:00"))
            except ValueError:
                continue
    return None


def _orderbook_side_liquidity(orderbook: dict[str, Any], side: str) -> float:
    """Sum of price × size across bid levels for the given side."""
    side_data = orderbook.get(side.lower()) or orderbook.get(side)
    if isinstance(side_data, dict):
        bids = side_data.get("bids") or []
    elif isinstance(side_data, list):
        bids = side_data
    else:
        return 0.0
    total = 0.0
    for level in bids:
        if not isinstance(level, dict):
            continue
        try:
            total += float(level.get("price") or 0) * float(
                level.get("size") or level.get("quantity") or 0
            )
        except (TypeError, ValueError):
            continue
    return total


