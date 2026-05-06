"""Async HTTP client for the CSA Distributed Compliance Ledger REST gateway.

Built on httpx.AsyncClient. Three behaviors layered into every request:

  - An asyncio.Semaphore (default 5 slots) caps in-flight requests so a
    fan-out walk over thousands of (vid,pid) pairs cannot overwhelm a
    public observer node.
  - A configurable per-worker sleep_between (default 200ms) — applied
    after each request inside the semaphore-held window — preserves the
    politeness budget regardless of concurrency.
  - Retries with exponential backoff on transport errors and 5xx, and a
    `none_on_404` mode for single-record lookups so callers can tell
    "no row" apart from a real failure.

Pagination walks (vendors, compliance) are inherently sequential — each
page's cursor depends on the previous page — so Semaphore=5 has no effect
on them. The semaphore is what speeds up the model/version fan-out.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, AsyncIterator

import httpx

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://on.dcl.csa-iot.org"
REQUEST_TIMEOUT = 30.0
SLEEP_BETWEEN = 0.2     # politeness, per worker
MAX_RETRIES = 3          # 3 retries after the first try ⇒ 4 attempts total
BACKOFF_BASE = 1.0       # 1s, 2s, 4s
DEFAULT_CONCURRENCY = 5


class DCLClient:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep_between: float = SLEEP_BETWEEN,
        timeout: float = REQUEST_TIMEOUT,
        max_retries: int = MAX_RETRIES,
        max_concurrency: int = DEFAULT_CONCURRENCY,
    ):
        self.base_url = base_url.rstrip("/")
        self.sleep_between = sleep_between
        self.max_retries = max_retries
        self.sem = asyncio.Semaphore(max_concurrency)
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            transport=transport,
            headers={
                "accept": "application/json",
                "user-agent": "matter-db-sync/0.1",
            },
        )

    async def __aenter__(self) -> "DCLClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def close(self) -> None:
        await self.client.aclose()

    # -- low-level get with retry, semaphore, per-worker sleep ----------

    async def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        none_on_404: bool = False,
    ) -> dict[str, Any] | None:
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            async with self.sem:
                try:
                    resp = await self.client.get(path, params=params)
                except httpx.TransportError as exc:
                    last_exc = exc
                    await self._sleep_backoff(attempt, str(exc), path)
                    continue

                if resp.status_code == 404 and none_on_404:
                    await asyncio.sleep(self.sleep_between)
                    return None
                if 500 <= resp.status_code < 600:
                    last_exc = httpx.HTTPStatusError(
                        f"{resp.status_code} from {path}",
                        request=resp.request, response=resp,
                    )
                    await self._sleep_backoff(attempt, f"HTTP {resp.status_code}", path)
                    continue
                resp.raise_for_status()
                await asyncio.sleep(self.sleep_between)
                return resp.json()

        assert last_exc is not None
        raise last_exc

    async def _sleep_backoff(self, attempt: int, reason: str, path: str) -> None:
        if attempt >= self.max_retries:
            return
        backoff = BACKOFF_BASE * (2 ** attempt) + random.random() * 0.2
        log.warning(
            "DCL retry %d/%d for %s (%s); sleeping %.1fs",
            attempt + 1, self.max_retries, path, reason, backoff,
        )
        await asyncio.sleep(backoff)

    # -- public typed helpers -------------------------------------------

    async def iter_paginated(
        self,
        path: str,
        items_key: str,
        *,
        page_size: int = 500,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield every item from a Cosmos-paginated list endpoint.

        Pages are fetched serially (each cursor depends on the previous
        response), so the semaphore here only ever has one slot in use.
        """
        next_key: str | None = None
        while True:
            params: dict[str, Any] = {"pagination.limit": str(page_size)}
            if next_key:
                params["pagination.key"] = next_key
            else:
                params["pagination.count_total"] = "true"
            page = await self._get(path, params=params)
            assert page is not None  # list endpoints should not 404
            for item in page.get(items_key, []) or []:
                yield item
            pagination = page.get("pagination") or {}
            next_key = pagination.get("next_key") or None
            if not next_key:
                break

    async def get_vendors(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        async for item in self.iter_paginated(
            "/dcl/vendorinfo/vendors", items_key="vendorInfo"):
            out.append(item)
        return out

    async def get_compliance_records(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        async for item in self.iter_paginated(
            "/dcl/compliance/compliance-info", items_key="complianceInfo"):
            out.append(item)
        return out

    async def get_model(self, vid: int, pid: int) -> dict[str, Any] | None:
        resp = await self._get(f"/dcl/model/models/{vid}/{pid}", none_on_404=True)
        if resp is None:
            return None
        return resp.get("model")

    async def get_model_version(
        self, vid: int, pid: int, software_version: int,
    ) -> dict[str, Any] | None:
        resp = await self._get(
            f"/dcl/model/versions/{vid}/{pid}/{software_version}",
            none_on_404=True,
        )
        if resp is None:
            return None
        return resp.get("modelVersion")
