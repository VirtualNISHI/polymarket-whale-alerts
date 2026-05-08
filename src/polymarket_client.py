"""Async HTTP client for Polymarket public APIs (Gamma / CLOB / Data).

No authentication required. Three base hosts:
  - Gamma:  events / markets metadata             https://gamma-api.polymarket.com
  - CLOB:   per-token orderbook + price history   https://clob.polymarket.com
  - Data:   public trade feed + user positions    https://data-api.polymarket.com

Method semantics mirror the Nansen MCP client this replaces; downstream code
(`detector`, `formatter`, `job`) consumes normalized dicts produced here.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

log = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
DATA_BASE = "https://data-api.polymarket.com"


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return 500 <= exc.response.status_code < 600
    return False


class PolymarketClient:
    def __init__(
        self,
        *,
        rate_limit_seconds: float = 0.2,
        timeout: float = 30.0,
        user_agent: str = "polymarket-whale-alerts/0.1",
    ) -> None:
        self._rate_limit_seconds = rate_limit_seconds
        self._last_call_at = 0.0
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={"Accept": "application/json", "User-Agent": user_agent},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "PolymarketClient":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.aclose()

    async def _wait_rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_call_at
        wait = self._rate_limit_seconds - elapsed
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_call_at = time.monotonic()

    async def _get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        await self._wait_rate_limit()
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=1, max=8),
            retry=retry_if_exception(_is_retryable),
            reraise=True,
        ):
            with attempt:
                resp = await self._client.get(url, params=params)
                resp.raise_for_status()
                return resp.json()
        return None  # unreachable

    # ---------- markets / screening ----------

    async def list_active_markets(
        self,
        *,
        min_volume_usd: float,
        limit: int,
        excluded_categories: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return CLOB-enabled active markets sorted by 24h volume desc.

        Pulls events sorted by 24h volume, flattens their nested markets, applies
        per-market volume / status filters, and trims to `limit`. We over-fetch
        events because each event aggregates several markets, only some of which
        meet our criteria.
        """
        params = {
            "active": "true",
            "closed": "false",
            "archived": "false",
            "order": "volume_24hr",
            "ascending": "false",
            "limit": max(limit * 4, 100),
            "volume_min": str(min_volume_usd),
        }
        events = await self._get(f"{GAMMA_BASE}/events", params=params)
        if not isinstance(events, list):
            return []

        excluded = {c.lower() for c in (excluded_categories or [])}

        flat: list[dict[str, Any]] = []
        for event in events:
            if not isinstance(event, dict):
                continue
            event_category = (event.get("category") or "").lower()
            if event_category and event_category in excluded:
                continue
            for market in event.get("markets") or []:
                if not isinstance(market, dict):
                    continue
                if market.get("closed") or market.get("archived"):
                    continue
                if market.get("active") is False:
                    continue
                if market.get("enableOrderBook") is False:
                    continue
                if _to_float(market.get("volume24hr")) < min_volume_usd:
                    continue
                normalized = _normalize_market(market, event)
                if normalized is not None:
                    flat.append(normalized)

        flat.sort(key=lambda m: m.get("volume_24h", 0.0), reverse=True)
        return flat[:limit]

    # ---------- orderbook ----------

    async def get_orderbook(
        self, *, yes_token_id: str | None, no_token_id: str | None
    ) -> dict[str, Any]:
        """Return ``{'yes': {'bids': [...]}, 'no': {'bids': [...]}, 'mid_price': float}``."""
        yes_book = (
            await self._get(f"{CLOB_BASE}/book", params={"token_id": yes_token_id})
            if yes_token_id else {}
        )
        no_book = (
            await self._get(f"{CLOB_BASE}/book", params={"token_id": no_token_id})
            if no_token_id else {}
        )

        yes_bids = _book_levels(yes_book, "bids")
        yes_asks = _book_levels(yes_book, "asks")
        no_bids = _book_levels(no_book, "bids")

        return {
            "yes": {"bids": yes_bids},
            "no": {"bids": no_bids},
            "mid_price": _mid_from_book(yes_bids, yes_asks),
        }

    # ---------- trades ----------

    async def get_recent_trades(
        self,
        *,
        condition_id: str,
        since: datetime | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Public trade feed for a single market. Normalizes wallet/amount/side fields."""
        params = {
            "market": condition_id,
            "limit": str(limit),
            "takerOnly": "false",
        }
        resp = await self._get(f"{DATA_BASE}/trades", params=params)
        if not isinstance(resp, list):
            return []

        since_ts = since.timestamp() if since else None
        out: list[dict[str, Any]] = []
        for raw in resp:
            if not isinstance(raw, dict):
                continue

            ts_raw = raw.get("timestamp")
            ts_seconds = _to_float(ts_raw)
            if since_ts is not None and ts_seconds and ts_seconds < since_ts:
                continue

            size = _to_float(raw.get("size"))
            price = _to_float(raw.get("price"))
            amount_usd = size * price
            wallet = (raw.get("proxyWallet") or "").lower() or None
            action = (raw.get("side") or "").upper()      # BUY or SELL
            outcome = (raw.get("outcome") or "").upper()   # YES or NO
            side_label = f"{action} {outcome}".strip() if action or outcome else ""

            normalized = {
                **raw,
                "trade_id": raw.get("transactionHash"),
                "wallet_address": wallet,
                "amount_usd": amount_usd,
                "side": side_label,
                "price": price,
                "timestamp": ts_seconds,
            }
            out.append(normalized)
        return out

    # ---------- wallet PnL ----------

    async def get_wallet_pnl_summary(self, *, address: str) -> dict[str, Any]:
        """Aggregate open Polymarket positions into a Smart-Trader-style summary.

        Phase 1 simplification: PnL/win-rate are computed only over currently
        held positions (Polymarket's /positions endpoint). Closed-position
        history is not pulled. Tune thresholds in `config/thresholds.yaml`
        accordingly — Polymarket-internal numbers are smaller than the
        cross-protocol Nansen numbers the original SPEC envisioned.
        """
        params = {"user": address, "sizeThreshold": "0", "limit": "500"}
        resp = await self._get(f"{DATA_BASE}/positions", params=params)
        if not isinstance(resp, list):
            return _empty_pnl()

        positions = [p for p in resp if isinstance(p, dict)]
        if not positions:
            return _empty_pnl()

        cum_pnl = sum(_to_float(p.get("cashPnl")) for p in positions)
        cum_volume = sum(
            _to_float(p.get("totalBought")) or _to_float(p.get("initialValue"))
            for p in positions
        )
        wins = sum(1 for p in positions if _to_float(p.get("cashPnl")) > 0)

        return {
            "cumulative_pnl_usd": cum_pnl,
            "win_rate": wins / len(positions),
            "trade_count": len(positions),
            "cumulative_volume_usd": cum_volume,
        }


# ---------- helpers ----------

def _empty_pnl() -> dict[str, Any]:
    return {
        "cumulative_pnl_usd": 0.0,
        "win_rate": 0.0,
        "trade_count": 0,
        "cumulative_volume_usd": 0.0,
    }


def _normalize_market(market: dict[str, Any], event: dict[str, Any]) -> dict[str, Any] | None:
    condition_id = market.get("conditionId")
    if not condition_id:
        return None

    prices = _parse_string_array(market.get("outcomePrices"))
    yes_price = _to_float(prices[0]) if prices else 0.0

    token_ids = _parse_string_array(market.get("clobTokenIds"))
    yes_token = token_ids[0] if len(token_ids) >= 1 else None
    no_token = token_ids[1] if len(token_ids) >= 2 else None

    return {
        "market_id": condition_id,
        "id": condition_id,
        "slug": market.get("slug") or event.get("slug"),
        "question": market.get("question") or event.get("title"),
        "category": event.get("category") or market.get("category"),
        "current_probability": yes_price,
        "probability_change_24h": _to_float(market.get("oneDayPriceChange")),
        "probability_change_1h": _to_float(market.get("oneHourPriceChange")),
        "volume_24h": _to_float(market.get("volume24hr")),
        "yes_token_id": yes_token,
        "no_token_id": no_token,
        "raw": market,
    }


def _parse_string_array(value: Any) -> list[str]:
    """Polymarket encodes outcomePrices / clobTokenIds as a JSON-string in some
    code paths and as a real array in others; accept both."""
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        if isinstance(parsed, list):
            return [str(v) for v in parsed]
    return []


def _to_float(v: Any) -> float:
    if v is None or v == "":
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _book_levels(book: Any, key: str) -> list[dict[str, Any]]:
    if not isinstance(book, dict):
        return []
    levels = book.get(key) or []
    if not isinstance(levels, list):
        return []
    return [lv for lv in levels if isinstance(lv, dict)]


def _mid_from_book(bids: list[dict[str, Any]], asks: list[dict[str, Any]]) -> float:
    best_bid = _to_float(bids[0].get("price")) if bids else 0.0
    best_ask = _to_float(asks[0].get("price")) if asks else 0.0
    if best_bid and best_ask:
        return (best_bid + best_ask) / 2
    return best_bid or best_ask
