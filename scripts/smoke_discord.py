"""One-off Discord webhook smoke test.

Sends a single sample whale-trade embed and a single sample orderbook-skew
embed using the configured DISCORD_WEBHOOK_URL, then exits. Useful to verify
the webhook is wired up correctly and the Embed formatting renders as
expected before relying on cron-triggered runs.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import Settings
from src.detector import LargeTrade, OrderbookSkewSignal
from src.discord_client import DiscordClient
from src.formatter import format_large_trade_embed, format_orderbook_skew_embed


async def main() -> int:
    logging.basicConfig(level="INFO", format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    settings = Settings()
    if not settings.discord_webhook_url:
        print("DISCORD_WEBHOOK_URL not set; aborting.", file=sys.stderr)
        return 1

    market = {
        "market_id": "0xsmoke",
        "slug": "will-bitcoin-hit-150k-by-june-30-2026",
        "question": "[SMOKE TEST] Will Bitcoin hit $150k by June 30, 2026?",
    }
    trade = LargeTrade(
        trade_id="0xsmoke-trade-1",
        market_id="0xsmoke",
        wallet_address="0xabcdef0123456789abcdef0123456789abcdef01",
        side="BUY YES",
        amount_usd=75_000,
        price=0.62,
        timestamp=datetime.now(timezone.utc),
        reason="single",
        raw={},
    )
    skew = OrderbookSkewSignal(
        market_id="0xsmoke",
        dominant_side="YES",
        skew_ratio=4.2,
        prob_change_1h=0.07,
        current_probability=0.62,
        volume_1h_usd=45_000,
        raw={},
    )

    embeds = [
        format_large_trade_embed(
            market=market,
            trade=trade,
            wallet_tag="Smart Trader",
            wallet_summary={"cumulative_pnl_usd": 200_000, "win_rate": 0.65, "trade_count": 42},
            current_probability=0.62,
            delta_24h=0.05,
        ),
        format_orderbook_skew_embed(market=market, signal=skew),
    ]

    async with DiscordClient(settings.discord_webhook_url) as discord:
        for i, embed in enumerate(embeds, 1):
            title_safe = embed["title"].encode("ascii", "replace").decode("ascii")
            print(f"sending embed {i}/{len(embeds)}: {title_safe}")
            await discord.send_embed(embed)

    print("Done. Check the Discord channel.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
