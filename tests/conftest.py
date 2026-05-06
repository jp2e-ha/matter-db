"""Shared test fixtures and the synthetic DCL scenario builder.

Tests use httpx.MockTransport rather than respx so that no external
patching is in play — each DCLClient gets its own pinned transport.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

from matter_db.client import DCLClient
from matter_db.schema import connect, initialize

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def db_conn(tmp_path):
    db_path = tmp_path / "matter.db"
    conn = connect(str(db_path))
    initialize(conn)
    yield conn
    conn.close()


def make_transport(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def make_client(handler, *, sleep_between: float = 0.0,
                max_retries: int = 3) -> DCLClient:
    return DCLClient(
        "https://dcl.test",
        transport=make_transport(handler),
        sleep_between=sleep_between,
        max_retries=max_retries,
        timeout=2.0,
    )


# ---- a tiny but complete DCL scenario for the e2e test ----------------

def synthetic_scenario() -> dict[str, Any]:
    """Three vendors:

      vid 1001 (Foo Corp)   — 1 model with 1 firmware version, certified
      vid 1002 (Bar Inc)    — 1 model, 1 firmware, certified
      vid 1003 (Apple-like) — vendor record, but the Models endpoint 404s
                              (we don't actually exercise it; the compliance
                              walk drives lookups, and 1003 has no
                              compliance records — the 404 case is exercised
                              by an extra fan-out target below)

    A fourth compliance row references vid 9999 / pid 7 — a model that
    will 404 — to verify "models 404" notes capture works.
    """
    vendors = [
        {"vendorID": 1001, "vendorName": "Foo Corp",
         "companyLegalName": "Foo Corporation",
         "vendorLandingPageURL": "https://foo.example/",
         "creator": "cosmos1foo", "schemaVersion": 0},
        {"vendorID": 1002, "vendorName": "Bar Inc",
         "companyLegalName": "Bar Incorporated",
         "vendorLandingPageURL": "https://bar.example/",
         "creator": "cosmos1bar", "schemaVersion": 0},
        {"vendorID": 1003, "vendorName": "Apple-like",
         "companyLegalName": "Apple-like LLC",
         "vendorLandingPageURL": "",
         "creator": "cosmos1apple", "schemaVersion": 0},
    ]

    compliance = [
        {"vid": 1001, "pid": 1, "softwareVersion": 100,
         "certificationType": "matter", "softwareVersionCertificationStatus": 2,
         "date": "2025-01-15T00:00:00.000Z",
         "cDCertificateId": "CSA-FOO-1", "softwareVersionString": "1.0.0",
         "history": [], "schemaVersion": 0},
        {"vid": 1002, "pid": 5, "softwareVersion": 200,
         "certificationType": "matter", "softwareVersionCertificationStatus": 2,
         "date": "2025-02-20T00:00:00.000Z",
         "cDCertificateId": "CSA-BAR-1", "softwareVersionString": "2.0.0",
         "history": [], "schemaVersion": 0},
        # zigbee row that should be excluded from the matter view
        {"vid": 1001, "pid": 2, "softwareVersion": 50,
         "certificationType": "zigbee", "softwareVersionCertificationStatus": 2,
         "date": "2024-09-10T00:00:00.000Z",
         "cDCertificateId": "CSA-FOO-2", "softwareVersionString": "0.5.0",
         "history": [], "schemaVersion": 0},
        # ghost compliance referencing a vendor with no Model on the ledger
        {"vid": 9999, "pid": 7, "softwareVersion": 300,
         "certificationType": "matter", "softwareVersionCertificationStatus": 2,
         "date": "2025-03-01T00:00:00.000Z",
         "cDCertificateId": "CSA-GHOST-1", "softwareVersionString": "3.0.0",
         "history": [], "schemaVersion": 0},
    ]

    models = {
        (1001, 1): {"vid": 1001, "pid": 1, "deviceTypeId": 256,
                    "productName": "Foo Light", "productLabel": "Foo Smart Bulb",
                    "partNumber": "FOO-001",
                    "productUrl": "https://foo.example/light",
                    "creator": "cosmos1foo", "schemaVersion": 0},
        (1002, 5): {"vid": 1002, "pid": 5, "deviceTypeId": 770,
                    "productName": "Bar Sensor",
                    "productLabel": "Bar Temperature Sensor",
                    "partNumber": "BAR-005",
                    "productUrl": "https://bar.example/sensor",
                    "creator": "cosmos1bar", "schemaVersion": 0},
        # zigbee compliance row's model
        (1001, 2): {"vid": 1001, "pid": 2, "deviceTypeId": 0,
                    "productName": "Foo Legacy Switch", "productLabel": "",
                    "partNumber": "FOO-LEGACY-002",
                    "productUrl": "https://foo.example/legacy",
                    "creator": "cosmos1foo", "schemaVersion": 0},
        # (9999, 7) intentionally absent → 404 ⇒ models_404 entry
    }

    versions = {
        (1001, 1, 100): {"vid": 1001, "pid": 1, "softwareVersion": 100,
                         "softwareVersionString": "1.0.0", "cdVersionNumber": 1,
                         "otaUrl": "https://foo.example/ota/100.bin",
                         "firmwareInformation": "stable",
                         "creator": "cosmos1foo", "schemaVersion": 0},
        (1002, 5, 200): {"vid": 1002, "pid": 5, "softwareVersion": 200,
                         "softwareVersionString": "2.0.0", "cdVersionNumber": 1,
                         "otaUrl": "https://bar.example/ota/200.bin",
                         "firmwareInformation": "stable",
                         "creator": "cosmos1bar", "schemaVersion": 0},
        (1001, 2, 50): {"vid": 1001, "pid": 2, "softwareVersion": 50,
                        "softwareVersionString": "0.5.0", "cdVersionNumber": 1,
                        "otaUrl": "", "firmwareInformation": "",
                        "creator": "cosmos1foo", "schemaVersion": 0},
        # (9999, 7, 300) absent → 404 ⇒ versions_404 entry
    }

    return {"vendors": vendors, "compliance": compliance,
            "models": models, "versions": versions}


def scenario_handler(scenario: dict[str, Any]) -> Callable[[httpx.Request], httpx.Response]:
    """Build a MockTransport handler that serves a synthetic_scenario."""
    vendors = scenario["vendors"]
    compliance = scenario["compliance"]
    models = scenario["models"]
    versions = scenario["versions"]

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        # paginated lists — serve everything in one page; the client will
        # still hit them with pagination params, but we ignore the cursor
        # since the dataset is tiny.
        if path == "/dcl/vendorinfo/vendors":
            return httpx.Response(200, json={
                "vendorInfo": vendors,
                "pagination": {"next_key": None, "total": str(len(vendors))},
            })
        if path == "/dcl/compliance/compliance-info":
            return httpx.Response(200, json={
                "complianceInfo": compliance,
                "pagination": {"next_key": None, "total": str(len(compliance))},
            })
        # models/{vid}/{pid}
        parts = path.strip("/").split("/")
        if len(parts) == 5 and parts[:3] == ["dcl", "model", "models"]:
            vid, pid = int(parts[3]), int(parts[4])
            m = models.get((vid, pid))
            if m is None:
                return httpx.Response(404, json={"code": 5,
                                                 "message": f"not found: {vid}/{pid}"})
            return httpx.Response(200, json={"model": m})
        # versions/{vid}/{pid}/{sv}
        if len(parts) == 6 and parts[:3] == ["dcl", "model", "versions"]:
            vid, pid, sv = int(parts[3]), int(parts[4]), int(parts[5])
            v = versions.get((vid, pid, sv))
            if v is None:
                return httpx.Response(404, json={"code": 5,
                                                 "message": f"not found"})
            return httpx.Response(200, json={"modelVersion": v})
        return httpx.Response(404, json={"code": 5, "message": f"unhandled {path}"})

    return handler


@pytest.fixture
def scenario():
    return synthetic_scenario()


@pytest.fixture
def scenario_client(scenario):
    return make_client(scenario_handler(scenario))
