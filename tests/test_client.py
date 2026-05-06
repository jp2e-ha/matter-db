"""DCLClient: pagination, 404-as-None, retries on 5xx and transport errors."""

from __future__ import annotations

import asyncio
from itertools import count

import httpx
import pytest

from matter_db.client import DCLClient
from tests.conftest import make_client


async def test_paginated_walk_follows_next_key():
    pages = {
        None: {
            "vendorInfo": [{"vendorID": 1, "vendorName": "A"},
                           {"vendorID": 2, "vendorName": "B"}],
            "pagination": {"next_key": "PAGE2", "total": "3"},
        },
        "PAGE2": {
            "vendorInfo": [{"vendorID": 3, "vendorName": "C"}],
            "pagination": {"next_key": None},
        },
    }

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/dcl/vendorinfo/vendors"
        key = req.url.params.get("pagination.key")
        return httpx.Response(200, json=pages[key])

    async with make_client(handler) as client:
        items = []
        async for item in client.iter_paginated(
            "/dcl/vendorinfo/vendors", items_key="vendorInfo", page_size=10):
            items.append(item)

    assert [i["vendorID"] for i in items] == [1, 2, 3]


async def test_get_model_returns_none_on_404():
    def handler(req):
        assert req.url.path == "/dcl/model/models/4937/1"
        return httpx.Response(404, json={"code": 5, "message": "not found"})

    async with make_client(handler) as client:
        result = await client.get_model(4937, 1)
    assert result is None


async def test_get_model_returns_inner_dict_on_200():
    body = {"vid": 4447, "pid": 2050, "productName": "Aqara Hub"}
    def handler(req):
        return httpx.Response(200, json={"model": body})
    async with make_client(handler) as client:
        m = await client.get_model(4447, 2050)
    assert m == body


async def test_5xx_triggers_retry_then_succeeds():
    counter = count()

    def handler(req):
        n = next(counter)
        if n < 2:
            return httpx.Response(503, json={"message": "boom"})
        return httpx.Response(200, json={"model": {"vid": 1, "pid": 2}})

    async with make_client(handler, max_retries=3) as client:
        m = await client.get_model(1, 2)
    assert m == {"vid": 1, "pid": 2}
    assert next(counter) == 3  # exactly three calls were made


async def test_5xx_exhausting_retries_raises():
    counter = count()

    def handler(req):
        next(counter)
        return httpx.Response(500, json={"message": "down"})

    async with make_client(handler, max_retries=2) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await client.get_model(1, 2)

    assert next(counter) == 3


async def test_transport_error_triggers_retry_then_succeeds():
    counter = count()

    def handler(req):
        n = next(counter)
        if n == 0:
            raise httpx.ConnectError("dropped")
        return httpx.Response(200, json={"model": {"vid": 9, "pid": 9}})

    async with make_client(handler, max_retries=2) as client:
        m = await client.get_model(9, 9)
    assert m == {"vid": 9, "pid": 9}


async def test_concurrency_semaphore_caps_in_flight():
    """Semaphore(2) on the client should never let more than 2 requests
    overlap, even when the caller fires 10 in parallel."""
    in_flight = 0
    max_seen = 0
    lock = asyncio.Lock()

    async def handler(req):  # async handler so we can sleep inside
        nonlocal in_flight, max_seen
        async with lock:
            in_flight += 1
            max_seen = max(max_seen, in_flight)
        await asyncio.sleep(0.05)
        async with lock:
            in_flight -= 1
        # path is /dcl/model/models/{vid}/{pid}
        parts = req.url.path.strip("/").split("/")
        return httpx.Response(200, json={
            "model": {"vid": int(parts[3]), "pid": int(parts[4])}
        })

    async with make_client(handler, max_concurrency=2) as client:
        results = await asyncio.gather(
            *[client.get_model(1, i) for i in range(10)]
        )

    assert all(r is not None for r in results)
    assert max_seen == 2, f"semaphore should cap at 2, saw {max_seen}"
