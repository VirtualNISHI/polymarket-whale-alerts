"""Async Discord webhook client."""
from __future__ import annotations

import logging
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

log = logging.getLogger(__name__)


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or 500 <= code < 600
    return False


class DiscordClient:
    def __init__(self, webhook_url: str, timeout: float = 15.0):
        self._url = webhook_url
        self._client = httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "DiscordClient":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.aclose()

    async def send_embed(self, embed: dict[str, Any]) -> None:
        if not self._url:
            log.warning("Discord webhook URL not configured; skipping send")
            return
        payload = {"embeds": [embed]}
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(3),
                wait=wait_exponential(multiplier=1, min=1, max=8),
                retry=retry_if_exception(_is_retryable),
                reraise=True,
            ):
                with attempt:
                    resp = await self._client.post(self._url, json=payload)
                    resp.raise_for_status()
        except Exception as e:
            log.exception("Discord send failed: %s", e)
