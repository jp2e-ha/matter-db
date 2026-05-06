"""HTTP client for the CSA Distributed Compliance Ledger REST gateway.

Wraps httpx with three behaviors that show up across every endpoint:

  - Cosmos SDK pagination (pagination.key cursor + pagination.limit).
  - Retries with exponential backoff on transport errors and 5xx.
  - 404 returned as None for "single-record" lookups, so the caller can
    distinguish "no row" from a real error. This is the Apple-style case
    documented in docs/findings.md: a vendor exists but has no Models.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any, Iterator

import httpx

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://on.dcl.csa-iot.org"
REQUEST_TIMEOUT = 30.0
SLEEP_BETWEEN = 0.2  # politeness between requests (seconds)
MAX_RETRIES = 3       # 3 attempts after the first try, so 4 total
BACKOFF_BASE = 1.0    # 1s, 2s, 4s


class DCLClient:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep_between: float = SLEEP_BETWEEN,
        timeout: float = REQUEST_TIMEOUT,
        max_retries: int = MAX_RETRIES,
    ):
        self.base_url = base_url.rstrip("/")
        self.sleep_between = sleep_between
        self.max_retries = max_retries
        self.client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            transport=transport,
            headers={
                "accept": "application/json",
                "user-agent": "matter-db-sync/0.1",
            },
        )

    # context manager so callers can `with DCLClient(...) as c:`
    def __enter__(self) -> "DCLClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        self.client.close()

    # -- low-level get with retry + 404-as-None ---------------------------

    def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        none_on_404: bool = False,
    ) -> dict[str, Any] | None:
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self.client.get(path, params=params)
            except httpx.TransportError as exc:
                last_exc = exc
                self._sleep_backoff(attempt, str(exc), path)
                continue

            if resp.status_code == 404 and none_on_404:
                time.sleep(self.sleep_between)
                return None
            if 500 <= resp.status_code < 600:
                last_exc = httpx.HTTPStatusError(
                    f"{resp.status_code} from {path}",
                    request=resp.request, response=resp,
                )
                self._sleep_backoff(attempt, f"HTTP {resp.status_code}", path)
                continue
            resp.raise_for_status()
            time.sleep(self.sleep_between)
            return resp.json()

        assert last_exc is not None
        raise last_exc

    def _sleep_backoff(self, attempt: int, reason: str, path: str) -> None:
        if attempt >= self.max_retries:
            return
        backoff = BACKOFF_BASE * (2 ** attempt) + random.random() * 0.2
        log.warning(
            "DCL retry %d/%d for %s (%s); sleeping %.1fs",
            attempt + 1, self.max_retries, path, reason, backoff,
        )
        time.sleep(backoff)

    # -- public typed helpers ---------------------------------------------

    def iter_paginated(
        self,
        path: str,
        items_key: str,
        *,
        page_size: int = 500,
    ) -> Iterator[dict[str, Any]]:
        """Yield every item from a Cosmos-paginated list endpoint."""
        next_key: str | None = None
        page_no = 0
        while True:
            params: dict[str, Any] = {"pagination.limit": str(page_size)}
            if next_key:
                params["pagination.key"] = next_key
            else:
                params["pagination.count_total"] = "true"
            page = self._get(path, params=params)
            assert page is not None  # list endpoints should not 404
            page_no += 1
            for item in page.get(items_key, []) or []:
                yield item
            pagination = page.get("pagination") or {}
            next_key = pagination.get("next_key") or None
            if not next_key:
                break

    def get_vendors(self) -> list[dict[str, Any]]:
        return list(self.iter_paginated(
            "/dcl/vendorinfo/vendors", items_key="vendorInfo"))

    def get_compliance_records(self) -> list[dict[str, Any]]:
        return list(self.iter_paginated(
            "/dcl/compliance/compliance-info", items_key="complianceInfo"))

    def get_model(self, vid: int, pid: int) -> dict[str, Any] | None:
        """Return the inner Model dict, or None on 404."""
        resp = self._get(f"/dcl/model/models/{vid}/{pid}", none_on_404=True)
        if resp is None:
            return None
        return resp.get("model")

    def get_model_version(
        self, vid: int, pid: int, software_version: int,
    ) -> dict[str, Any] | None:
        """Return the inner ModelVersion dict, or None on 404."""
        resp = self._get(
            f"/dcl/model/versions/{vid}/{pid}/{software_version}",
            none_on_404=True,
        )
        if resp is None:
            return None
        return resp.get("modelVersion")
