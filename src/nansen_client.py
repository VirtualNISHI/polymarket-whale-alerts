"""Nansen API client — ToS-redistribution-safe endpoints only.

Per Nansen Redistribution Policy (https://docs.nansen.ai/guides/redistribution-guide):

  FREE TIER (no attribution required, this client exposes):
    - profiler/address/current-balance
    - profiler/address/pnl-summary
    - profiler/address/historical-balance, transactions, transfers

  ATTRIBUTION TIER (must show "Powered by Nansen" or @nansen_ai):
    - prediction-market/address-summary
    - tgm/transfers, tgm/flows, tgm/dex-trades

  PROHIBITED / RESTRICTED (NEVER exposed here, DO NOT add):
    - smart-money/* (holdings, dex-trades, dcas, perp-trades, leaderboards)
    - profiler/address-labels (raw label strings)
    - any pnl-leaderboard

Public posting integrations MUST include:
  - X / Twitter:    a "@nansen_ai" mention or "Data: @nansen_ai" line
  - Discord / web:  "Powered by Nansen" link to https://nansen.ai
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)

NANSEN_BASE_URL = "https://api.nansen.ai/api/v1"

# Per docs: 20 req/s, 300 req/min. We pace at 20 rps; the per-minute cap is
# well within reach if a single caller stays under 5 req/s sustained.
_MIN_INTERVAL_S = 0.05

# Attribution constants — required by Nansen redistribution policy
ATTRIBUTION_X = "Data: @nansen_ai"
ATTRIBUTION_DISCORD = "Powered by [Nansen](https://nansen.ai)"
ATTRIBUTION_FOOTER_PLAIN = "Powered by Nansen"


class NansenClient:
    """Thin sync wrapper around Nansen REST API.

    Only exposes ToS-safe endpoints. Returns None on any error (auth, network,
    rate limit, 4xx, 5xx) so callers can degrade gracefully without breaking
    the host bot. Errors are logged at WARNING.

    If ``api_key`` is empty (env var unset), every call returns None — the
    client becomes a no-op so production bots can ship without Nansen.
    """

    def __init__(self, api_key: Optional[str] = None, *, timeout: float = 15.0) -> None:
        self.api_key = api_key if api_key is not None else os.environ.get("NANSEN_API_KEY", "")
        self.timeout = timeout
        self._client: Optional[httpx.Client] = None
        self._last_call: float = 0.0
        self._lock = threading.Lock()
        self.enabled = bool(self.api_key)
        if not self.enabled:
            log.info("NansenClient disabled (NANSEN_API_KEY not set) — all calls will return None")

    def _get_client(self) -> Optional[httpx.Client]:
        if not self.enabled:
            return None
        if self._client is None:
            self._client = httpx.Client(
                timeout=self.timeout,
                headers={
                    "apikey": self.api_key,
                    "Content-Type": "application/json",
                },
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    def __enter__(self) -> "NansenClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _post(self, path: str, payload: dict[str, Any]) -> Optional[dict[str, Any]]:
        client = self._get_client()
        if client is None:
            return None
        with self._lock:
            wait = _MIN_INTERVAL_S - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()
        url = f"{NANSEN_BASE_URL}/{path.lstrip('/')}"
        try:
            resp = client.post(url, json=payload)
        except httpx.HTTPError as e:
            log.warning("Nansen request error %s: %s", path, e)
            return None
        if resp.status_code == 429:
            log.warning("Nansen rate-limited (429) on %s", path)
            return None
        if resp.status_code >= 400:
            log.warning("Nansen %d on %s: %s", resp.status_code, path, resp.text[:200])
            return None
        try:
            return resp.json()
        except ValueError:
            log.warning("Nansen returned non-JSON for %s", path)
            return None

    # ------------------------------------------------------------
    #  ToS-safe endpoints
    # ------------------------------------------------------------

    def address_pnl_summary(
        self,
        address: str,
        *,
        chain: str = "ethereum",
        date_from: str,
        date_to: str,
    ) -> Optional[dict[str, Any]]:
        """Aggregated on-chain PnL for an address. FREE tier (no attribution).

        Response fields (top-level):
            realized_pnl_usd, realized_pnl_percent, win_rate,
            traded_token_count, traded_times, top5_tokens
        """
        return self._post(
            "profiler/address/pnl-summary",
            {
                "address": address,
                "chain": chain,
                "date": {"from": date_from, "to": date_to},
            },
        )

    def address_current_balance(
        self,
        address: str,
        *,
        chain: str = "ethereum",
        per_page: int = 10,
    ) -> Optional[dict[str, Any]]:
        """Current token holdings for an address. FREE tier."""
        return self._post(
            "profiler/address/current-balance",
            {
                "address": address,
                "chain": chain,
                "hide_spam_token": True,
                "pagination": {"page": 1, "per_page": per_page},
            },
        )

    def prediction_market_address_summary(self, address: str) -> Optional[dict[str, Any]]:
        """Polymarket aggregated PnL view of an address. ATTRIBUTION tier.

        Returns the single matching record (or None). Fields:
            realized_pnl_usd, unrealized_pnl_usd, total_pnl_usd,
            win_rate, markets_won, markets_traded, wallet_age_days
        """
        wrap = self._post(
            "prediction-market/address-summary",
            {"address": address, "pagination": {"page": 1, "per_page": 1}},
        )
        if not wrap:
            return None
        rows = wrap.get("data") or []
        return rows[0] if rows else None

    def token_transfers(
        self,
        token_address: str,
        *,
        chain: str = "ethereum",
        date_from: str,
        date_to: str,
        per_page: int = 50,
    ) -> Optional[dict[str, Any]]:
        """Large on-chain token transfers. ATTRIBUTION tier.

        IMPORTANT: response items may include Nansen entity labels (e.g.
        'Binance Hot Wallet'). Per ToS, raw labels MUST NOT be redistributed.
        Use labels for INTERNAL filtering only; redact before public posting.
        """
        return self._post(
            "tgm/transfers",
            {
                "chain": chain,
                "token_address": token_address,
                "date": {"from": date_from, "to": date_to},
                "pagination": {"page": 1, "per_page": per_page},
            },
        )
