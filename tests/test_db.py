"""Database dedup and cache tests."""
from __future__ import annotations

import tempfile

from src.db import Database


def _fresh_db() -> Database:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db = Database(tmp.name)
    db.init_schema()
    return db


def test_trade_dedup_by_trade_id():
    db = _fresh_db()
    assert not db.is_trade_alerted("t1")
    db.record_alert(
        alert_type="large_trade",
        market_id="m1",
        wallet_address="0xabc",
        trade_id="t1",
        amount_usd=60_000,
        side="YES",
    )
    assert db.is_trade_alerted("t1")
    assert not db.is_trade_alerted("t2")


def test_composite_dedup():
    db = _fresh_db()
    db.record_alert(
        alert_type="large_trade",
        market_id="m1",
        wallet_address="0xabc",
        amount_usd=60_000,
        side="YES",
    )
    assert db.is_alert_seen_composite("m1", "0xabc", 60_000.0, within_seconds=3600)
    assert not db.is_alert_seen_composite("m2", "0xabc", 60_000.0, within_seconds=3600)
    assert not db.is_alert_seen_composite("m1", "0xabc", 99_000.0, within_seconds=3600)


def test_skew_recent():
    db = _fresh_db()
    assert not db.is_skew_alert_recent("m1")
    db.record_alert(alert_type="orderbook_skew", market_id="m1", side="YES")
    assert db.is_skew_alert_recent("m1", within_seconds=60)
    assert not db.is_skew_alert_recent("m2", within_seconds=60)


def test_wallet_cache_roundtrip():
    db = _fresh_db()
    assert db.get_wallet_cache("0xabc", ttl_seconds=3600) is None
    db.set_wallet_cache("0xabc", "Smart Trader", 800_000, 0.65, 100)
    cached = db.get_wallet_cache("0xabc", ttl_seconds=3600)
    assert cached is not None
    assert cached["tag"] == "Smart Trader"
    assert cached["cumulative_pnl_usd"] == 800_000
    assert cached["win_rate"] == 0.65
    assert cached["trade_count"] == 100


def test_wallet_cache_ttl_expiry():
    db = _fresh_db()
    db.set_wallet_cache("0xabc", "Whale", 100_000, 0.5, 50)
    # Negative TTL forces "no fresh cache" path
    assert db.get_wallet_cache("0xabc", ttl_seconds=-1) is None
