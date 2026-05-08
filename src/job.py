"""Main detection job: screen markets, detect anomalies, notify, persist."""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from src.config import Settings, Thresholds, load_thresholds
from src.db import Database
from src.detector import (
    LargeTrade,
    classify_wallet,
    detect_large_trades,
    detect_orderbook_skew,
)
from src.discord_client import DiscordClient
from src.formatter import format_large_trade_embed, format_orderbook_skew_embed
from src.polymarket_client import PolymarketClient

log = logging.getLogger(__name__)

JOB_NAME = "whale_alerts"
SKEW_DEDUP_WINDOW_SECONDS = 3600


@dataclass
class Summary:
    screened: int = 0
    detected: int = 0
    notified: int = 0
    skipped: int = 0


def _market_id(m: dict[str, Any]) -> str:
    return str(m.get("market_id") or m.get("id") or "")


def _market_slug(m: dict[str, Any]) -> str | None:
    v = m.get("slug") or m.get("market_slug")
    return str(v) if v else None


def _float_field(m: dict[str, Any], keys: tuple[str, ...]) -> float:
    for k in keys:
        v = m.get(k)
        if v is not None and v != "":
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return 0.0


def _market_probability(m: dict[str, Any]) -> float:
    return _float_field(m, ("current_probability", "probability", "mid_price", "yes_price"))


def _market_delta_24h(m: dict[str, Any]) -> float:
    return _float_field(m, ("probability_change_24h", "oneDayPriceChange", "delta_24h"))


def _market_delta_1h(m: dict[str, Any]) -> float:
    return _float_field(m, ("probability_change_1h", "oneHourPriceChange"))


async def _classify_with_cache(
    *,
    db: Database,
    client: PolymarketClient,
    address: str,
    thresholds: Thresholds,
) -> tuple[str, dict[str, Any]]:
    cached = db.get_wallet_cache(address, ttl_seconds=thresholds.cache.wallet_ttl_seconds)
    if cached is not None:
        return cached["tag"], {
            "cumulative_pnl_usd": cached["cumulative_pnl_usd"],
            "win_rate": cached["win_rate"],
            "trade_count": cached["trade_count"],
        }
    try:
        pnl = await client.get_wallet_pnl_summary(address=address)
    except Exception as e:
        log.warning("PnL fetch failed for %s: %s", address, e)
        return "Regular", {"cumulative_pnl_usd": 0.0, "win_rate": 0.0, "trade_count": 0}

    tag, summary = classify_wallet(pnl, thresholds)
    db.set_wallet_cache(
        address,
        tag=tag,
        cumulative_pnl_usd=float(summary["cumulative_pnl_usd"]),
        win_rate=float(summary["win_rate"]),
        trade_count=int(summary["trade_count"]),
    )
    return tag, summary


def _is_dup_large_trade(db: Database, trade: LargeTrade) -> bool:
    if trade.trade_id and db.is_trade_alerted(trade.trade_id):
        return True
    if not trade.trade_id and db.is_alert_seen_composite(
        market_id=trade.market_id,
        wallet_address=trade.wallet_address,
        amount_usd=trade.amount_usd,
        within_seconds=3600,
    ):
        return True
    return False


async def _process_market(
    *,
    market: dict[str, Any],
    client: PolymarketClient,
    db: Database,
    discord: DiscordClient,
    thresholds: Thresholds,
    since: datetime,
    dry_run: bool,
    summary: Summary,
) -> None:
    market_id = _market_id(market)
    if not market_id:
        log.debug("Skipping market with no id: %s", market)
        summary.skipped += 1
        return

    slug = _market_slug(market)
    one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)

    try:
        trades = await client.get_recent_trades(
            condition_id=market_id, since=one_hour_ago
        )
    except Exception as e:
        log.warning("trades fetch failed for %s: %s", market_id, e)
        summary.skipped += 1
        return

    try:
        orderbook = await client.get_orderbook(
            yes_token_id=market.get("yes_token_id"),
            no_token_id=market.get("no_token_id"),
        )
    except Exception as e:
        log.warning("orderbook fetch failed for %s: %s", market_id, e)
        orderbook = {}

    # Large trades
    large_trades = detect_large_trades(
        trades=trades, market_id=market_id, thresholds=thresholds, since=since
    )
    for trade in large_trades:
        if _is_dup_large_trade(db, trade):
            summary.skipped += 1
            continue
        summary.detected += 1
        wallet_tag, wallet_summary = await _classify_with_cache(
            db=db, client=client, address=trade.wallet_address, thresholds=thresholds
        )
        embed = format_large_trade_embed(
            market=market,
            trade=trade,
            wallet_tag=wallet_tag,
            wallet_summary=wallet_summary,
            current_probability=_market_probability(market),
            delta_24h=_market_delta_24h(market),
        )
        if dry_run:
            log.info("[dry-run] large_trade embed: %s", json.dumps(embed, ensure_ascii=False))
        else:
            await discord.send_embed(embed)
            db.record_alert(
                alert_type="large_trade",
                market_id=market_id,
                market_slug=slug,
                wallet_address=trade.wallet_address,
                trade_id=trade.trade_id,
                amount_usd=trade.amount_usd,
                side=trade.side,
                probability=_market_probability(market),
                wallet_tag=wallet_tag,
                raw_payload={"trade": trade.raw, "wallet_summary": wallet_summary},
            )
        summary.notified += 1

    # Orderbook skew
    if not orderbook:
        return
    signal = detect_orderbook_skew(
        market_id=market_id,
        orderbook=orderbook,
        prob_change_1h=_market_delta_1h(market),
        current_probability=_market_probability(market),
        trades=trades,
        thresholds=thresholds,
    )
    if not signal:
        return
    if db.is_skew_alert_recent(market_id, within_seconds=SKEW_DEDUP_WINDOW_SECONDS):
        summary.skipped += 1
        return

    summary.detected += 1
    embed = format_orderbook_skew_embed(market=market, signal=signal)
    if dry_run:
        log.info("[dry-run] skew embed: %s", json.dumps(embed, ensure_ascii=False))
    else:
        await discord.send_embed(embed)
        db.record_alert(
            alert_type="orderbook_skew",
            market_id=market_id,
            market_slug=slug,
            amount_usd=signal.volume_1h_usd,
            side=signal.dominant_side,
            probability=signal.current_probability,
            raw_payload={
                "skew_ratio": signal.skew_ratio,
                "prob_change_1h": signal.prob_change_1h,
            },
        )
    summary.notified += 1


async def run_once(
    *, settings: Settings, thresholds: Thresholds, dry_run: bool
) -> Summary:
    db = Database(settings.db_path)
    db.init_schema()

    last_polled_at = db.get_last_polled_at(JOB_NAME)
    now = datetime.now(timezone.utc)
    since = last_polled_at or (now - timedelta(hours=1))
    log.info(
        "Polling since %s (now=%s, dry_run=%s)",
        since.isoformat(), now.isoformat(), dry_run,
    )

    summary = Summary()

    async with PolymarketClient(
        user_agent=settings.polymarket_user_agent
    ) as client, DiscordClient(settings.discord_webhook_url) as discord:
        try:
            markets = await client.list_active_markets(
                min_volume_usd=thresholds.screening.min_volume_usd,
                limit=thresholds.screening.top_n_markets,
                excluded_categories=thresholds.screening.excluded_categories or None,
            )
        except Exception as e:
            log.exception("market screening failed: %s", e)
            return summary

        summary.screened = len(markets)
        log.info("Screened %d markets", summary.screened)

        for market in markets:
            try:
                await _process_market(
                    market=market,
                    client=client,
                    db=db,
                    discord=discord,
                    thresholds=thresholds,
                    since=since,
                    dry_run=dry_run,
                    summary=summary,
                )
            except Exception:
                log.exception("Market %s processing failed", _market_id(market))
                summary.skipped += 1

    if not dry_run:
        db.set_last_polled_at(JOB_NAME, now)

    log.info(
        "Run summary | screened=%d detected=%d notified=%d skipped=%d",
        summary.screened, summary.detected, summary.notified, summary.skipped,
    )
    return summary


def main(*, dry_run: bool = False) -> Summary:
    settings = Settings()
    thresholds = load_thresholds()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    return asyncio.run(
        run_once(settings=settings, thresholds=thresholds, dry_run=dry_run or settings.dry_run)
    )
