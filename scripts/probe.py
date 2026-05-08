"""One-off probe: pick the highest-volume market and dump shape / size of
trades, orderbook, and a sample wallet's PnL summary so we can sanity-check
whether the detector is seeing realistic numbers.
"""
from __future__ import annotations

import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.polymarket_client import PolymarketClient


async def main() -> None:
    async with PolymarketClient() as client:
        markets = await client.list_active_markets(min_volume_usd=100_000, limit=5)
        print(f"Top {len(markets)} markets:")
        for i, m in enumerate(markets):
            print(
                f"  [{i}] vol24h=${m['volume_24h']:>12,.0f}  "
                f"prob={m['current_probability']:.3f}  "
                f"Δ1h={m['probability_change_1h']:+.3f}  "
                f"slug={m['slug']}"
            )

        if not markets:
            return

        m = markets[0]
        print(f"\n--- Probing market: {m['question']!r} ({m['slug']}) ---")

        trades = await client.get_recent_trades(condition_id=m["market_id"], limit=200)
        print(f"\n{len(trades)} trades fetched (last hour-ish)")
        if trades:
            sizes = sorted((t["amount_usd"] for t in trades), reverse=True)
            print(f"  amount_usd top 5:    {[f'${s:,.0f}' for s in sizes[:5]]}")
            print(f"  amount_usd median:   ${sizes[len(sizes) // 2]:,.0f}")
            print(f"  amount_usd total:    ${sum(sizes):,.0f}")
            sides = Counter(t.get("side", "") for t in trades)
            print(f"  side distribution:   {dict(sides)}")
            sample = trades[0]
            print("\n  sample trade keys:", sorted(sample.keys()))
            print("  sample trade:", json.dumps(
                {k: sample[k] for k in ("trade_id", "wallet_address", "amount_usd", "side", "price", "timestamp")},
                indent=2,
            ))

        ob = await client.get_orderbook(yes_token_id=m["yes_token_id"], no_token_id=m["no_token_id"])
        yes_liq = sum(float(b.get("price", 0)) * float(b.get("size", 0)) for b in ob["yes"]["bids"])
        no_liq = sum(float(b.get("price", 0)) * float(b.get("size", 0)) for b in ob["no"]["bids"])
        print(f"\norderbook YES bids liq=${yes_liq:,.0f}  ({len(ob['yes']['bids'])} levels)")
        print(f"orderbook NO  bids liq=${no_liq:,.0f}  ({len(ob['no']['bids'])} levels)")
        print(f"mid_price={ob.get('mid_price')}")
        if yes_liq and no_liq:
            ratio = max(yes_liq, no_liq) / min(yes_liq, no_liq)
            print(f"skew ratio: {ratio:.2f}x")

        if trades:
            sample_wallet = trades[0]["wallet_address"]
            print(f"\n--- Sample wallet PnL: {sample_wallet} ---")
            pnl = await client.get_wallet_pnl_summary(address=sample_wallet)
            print(json.dumps(pnl, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
