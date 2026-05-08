"""SQLite layer: alerts, poll state, wallet cache."""
from __future__ import annotations

import json
import logging
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_type TEXT NOT NULL,
    market_id TEXT NOT NULL,
    market_slug TEXT,
    wallet_address TEXT,
    trade_id TEXT,
    amount_usd REAL,
    side TEXT,
    probability REAL,
    wallet_tag TEXT,
    detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    notified_at TIMESTAMP,
    raw_payload TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_alerts_trade
    ON alerts(trade_id) WHERE trade_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_alerts_market ON alerts(market_id);
CREATE INDEX IF NOT EXISTS idx_alerts_detected ON alerts(detected_at);

CREATE TABLE IF NOT EXISTS poll_state (
    job_name TEXT PRIMARY KEY,
    last_polled_at TIMESTAMP,
    last_seen_trade_id TEXT
);

CREATE TABLE IF NOT EXISTS wallet_cache (
    wallet_address TEXT PRIMARY KEY,
    tag TEXT,
    cumulative_pnl_usd REAL,
    win_rate REAL,
    trade_count INTEGER,
    cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS alert_outcomes (
    alert_id INTEGER PRIMARY KEY,
    prob_at_1h REAL,
    prob_at_6h REAL,
    prob_at_24h REAL,
    final_resolution TEXT,
    FOREIGN KEY (alert_id) REFERENCES alerts(id)
);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, detect_types=sqlite3.PARSE_DECLTYPES)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    # ----- alerts -----

    def is_trade_alerted(self, trade_id: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM alerts WHERE trade_id = ? LIMIT 1", (trade_id,)
            ).fetchone()
            return row is not None

    def is_alert_seen_composite(
        self,
        market_id: str,
        wallet_address: str,
        amount_usd: float,
        within_seconds: int = 3600,
    ) -> bool:
        """Fallback dedup when trade_id is unavailable."""
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM alerts
                WHERE market_id = ?
                  AND wallet_address = ?
                  AND ABS(amount_usd - ?) < 0.01
                  AND detected_at >= datetime('now', ?)
                LIMIT 1
                """,
                (market_id, wallet_address, amount_usd, f"-{within_seconds} seconds"),
            ).fetchone()
            return row is not None

    def is_skew_alert_recent(self, market_id: str, within_seconds: int = 3600) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM alerts
                WHERE alert_type = 'orderbook_skew'
                  AND market_id = ?
                  AND detected_at >= datetime('now', ?)
                LIMIT 1
                """,
                (market_id, f"-{within_seconds} seconds"),
            ).fetchone()
            return row is not None

    def record_alert(
        self,
        *,
        alert_type: str,
        market_id: str,
        market_slug: str | None = None,
        wallet_address: str | None = None,
        trade_id: str | None = None,
        amount_usd: float | None = None,
        side: str | None = None,
        probability: float | None = None,
        wallet_tag: str | None = None,
        raw_payload: dict[str, Any] | None = None,
        notified: bool = True,
    ) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO alerts (
                    alert_type, market_id, market_slug, wallet_address, trade_id,
                    amount_usd, side, probability, wallet_tag,
                    notified_at, raw_payload
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    alert_type,
                    market_id,
                    market_slug,
                    wallet_address,
                    trade_id,
                    amount_usd,
                    side,
                    probability,
                    wallet_tag,
                    datetime.now(timezone.utc).isoformat() if notified else None,
                    json.dumps(raw_payload) if raw_payload is not None else None,
                ),
            )
            return cur.lastrowid or 0

    # ----- poll state -----

    def get_last_polled_at(self, job_name: str) -> datetime | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT last_polled_at FROM poll_state WHERE job_name = ?", (job_name,)
            ).fetchone()
            if not row or row["last_polled_at"] is None:
                return None
            value = row["last_polled_at"]
            if isinstance(value, datetime):
                return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
            return datetime.fromisoformat(value)

    def set_last_polled_at(self, job_name: str, at: datetime) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO poll_state (job_name, last_polled_at)
                VALUES (?, ?)
                ON CONFLICT(job_name) DO UPDATE SET last_polled_at = excluded.last_polled_at
                """,
                (job_name, at.isoformat()),
            )

    # ----- wallet cache -----

    def get_wallet_cache(
        self, wallet_address: str, ttl_seconds: int
    ) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT tag, cumulative_pnl_usd, win_rate, trade_count
                FROM wallet_cache
                WHERE wallet_address = ?
                  AND cached_at >= datetime('now', ?)
                """,
                (wallet_address, f"-{ttl_seconds} seconds"),
            ).fetchone()
            if not row:
                return None
            return {
                "tag": row["tag"],
                "cumulative_pnl_usd": row["cumulative_pnl_usd"],
                "win_rate": row["win_rate"],
                "trade_count": row["trade_count"],
            }

    def set_wallet_cache(
        self,
        wallet_address: str,
        tag: str,
        cumulative_pnl_usd: float,
        win_rate: float,
        trade_count: int,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO wallet_cache (
                    wallet_address, tag, cumulative_pnl_usd, win_rate, trade_count, cached_at
                )
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(wallet_address) DO UPDATE SET
                    tag = excluded.tag,
                    cumulative_pnl_usd = excluded.cumulative_pnl_usd,
                    win_rate = excluded.win_rate,
                    trade_count = excluded.trade_count,
                    cached_at = CURRENT_TIMESTAMP
                """,
                (wallet_address, tag, cumulative_pnl_usd, win_rate, trade_count),
            )


def _cli() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "init":
        from src.config import Settings
        settings = Settings()
        Database(settings.db_path).init_schema()
        print(f"Initialized DB at {settings.db_path}")
        return 0
    print("Usage: python -m src.db init", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(_cli())
