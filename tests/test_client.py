"""DCLClient: pagination, 404-as-None, retries on 5xx and transport errors."""

from __future__ import annotations

from itertools import count

import httpx
import pytest

from matter_db.client import DCLClient
from tests.conftest import make_client


def test_paginated_walk_follows_next_key():
    """Three vendors split across two pages; iter_paginated yields all."""
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

    with make_client(handler) as client:
        items = list(client.iter_paginated(
            "/dcl/vendorinfo/vendors", items_key="vendorInfo", page_size=10))

    assert [i["vendorID"] for i in items] == [1, 2, 3]


def test_get_model_returns_none_on_404():
    """Apple-style: vendor record exists, models endpoint 404s."""
    def handler(req):
        assert req.url.path == "/dcl/model/models/4937/1"
        return httpx.Response(404, json={"code": 5, "message": "not found"})

    with make_client(handler) as client:
        result = client.get_model(4937, 1)
    assert result is None


def test_get_model_returns_inner_dict_on_200():
    body = {"vid": 4447, "pid": 2050, "productName": "Aqara Hub"}
    def handler(req):
        return httpx.Response(200, json={"model": body})
    with make_client(handler) as client:
        m = client.get_model(4447, 2050)
    assert m == body


def test_5xx_triggers_retry_then_succeeds():
    """First two calls return 503, third returns 200."""
    counter = count()

    def handler(req):
        n = next(counter)
        if n < 2:
            return httpx.Response(503, json={"message": "boom"})
        return httpx.Response(200, json={"model": {"vid": 1, "pid": 2}})

    with make_client(handler, max_retries=3) as client:
        m = client.get_model(1, 2)
    assert m == {"vid": 1, "pid": 2}
    assert next(counter) == 3  # exactly three calls were made


def test_5xx_exhausting_retries_raises():
    counter = count()

    def handler(req):
        next(counter)
        return httpx.Response(500, json={"message": "down"})

    with make_client(handler, max_retries=2) as client:
        with pytest.raises(httpx.HTTPStatusError):
            client.get_model(1, 2)

    # max_retries=2 means up to 3 attempts total (initial + 2 retries)
    assert next(counter) == 3


def test_transport_error_triggers_retry_then_succeeds():
    counter = count()

    def handler(req):
        n = next(counter)
        if n == 0:
            raise httpx.ConnectError("dropped")
        return httpx.Response(200, json={"model": {"vid": 9, "pid": 9}})

    with make_client(handler, max_retries=2) as client:
        m = client.get_model(9, 9)
    assert m == {"vid": 9, "pid": 9}
