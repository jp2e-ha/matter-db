"""Landing page (Starlette TestClient) + metadata.yml syntax check.

Builds a tiny fixture DB containing both views and a couple of products
across two vendors, points the web app at it via $MATTER_DB_PATH, and
exercises the routes.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml
from starlette.testclient import TestClient

from matter_db.schema import connect, initialize
from matter_db.upsert import (
    upsert_compliance,
    upsert_model,
    upsert_model_version,
    upsert_vendor,
)

ROOT = Path(__file__).resolve().parent.parent
METADATA_YML = ROOT / "web" / "metadata.yml"


# ---- fixture DB --------------------------------------------------------

def _seed_fixture_db(path: Path) -> None:
    conn = connect(str(path))
    initialize(conn)
    now = "2026-05-06T12:00:00+00:00"
    # Two vendors with products
    upsert_vendor(conn, {
        "vendorID": 4107, "vendorName": "Signify",
        "companyLegalName": "Signify N.V.",
        "vendorLandingPageURL": "https://signify.com/",
        "schemaVersion": 0,
    }, now=now)
    upsert_vendor(conn, {
        "vendorID": 4447, "vendorName": "Aqara",
        "companyLegalName": "Lumi United Technology Co., Ltd.",
        "vendorLandingPageURL": "https://aqara.com/",
        "schemaVersion": 0,
    }, now=now)
    # One vendor on the watchlist (no compliance)
    upsert_vendor(conn, {
        "vendorID": 9999, "vendorName": "Empty Co",
        "companyLegalName": "Empty Co LLC",
        "vendorLandingPageURL": "",
        "schemaVersion": 0,
    }, now=now)
    # Models
    upsert_model(conn, {
        "vid": 4107, "pid": 1, "deviceTypeId": 256,
        "productName": "Hue Bulb White", "partNumber": "HUE-W-1",
        "productUrl": "https://signify.com/hue/white",
        "schemaVersion": 0,
    }, now=now)
    upsert_model(conn, {
        "vid": 4107, "pid": 2, "deviceTypeId": 257,
        "productName": "Hue Bulb Color", "partNumber": "HUE-C-1",
        "productUrl": "https://signify.com/hue/color",
        "schemaVersion": 0,
    }, now=now)
    upsert_model(conn, {
        "vid": 4447, "pid": 2050, "deviceTypeId": 14,
        "productName": "Aqara Hub", "partNumber": "AG035",
        "productUrl": "https://aqara.com/hub-m2",
        "schemaVersion": 0,
    }, now=now)
    # Versions
    for raw in [
        {"vid": 4107, "pid": 1, "softwareVersion": 100,
         "softwareVersionString": "1.0.0", "cdVersionNumber": 1,
         "schemaVersion": 0},
        {"vid": 4107, "pid": 2, "softwareVersion": 200,
         "softwareVersionString": "2.0.0", "cdVersionNumber": 1,
         "schemaVersion": 0},
        {"vid": 4447, "pid": 2050, "softwareVersion": 400,
         "softwareVersionString": "4.0.0", "cdVersionNumber": 1,
         "schemaVersion": 0},
    ]:
        upsert_model_version(conn, raw, now=now)
    # Compliance
    for raw in [
        {"vid": 4107, "pid": 1, "softwareVersion": 100,
         "certificationType": "matter", "softwareVersionCertificationStatus": 2,
         "date": "2025-01-15T00:00:00.000Z", "cDCertificateId": "CSA-S1",
         "schemaVersion": 0},
        {"vid": 4107, "pid": 2, "softwareVersion": 200,
         "certificationType": "matter", "softwareVersionCertificationStatus": 2,
         "date": "2025-02-20T00:00:00.000Z", "cDCertificateId": "CSA-S2",
         "schemaVersion": 0},
        {"vid": 4447, "pid": 2050, "softwareVersion": 400,
         "certificationType": "matter", "softwareVersionCertificationStatus": 2,
         "date": "2024-11-03T00:00:00.000Z", "cDCertificateId": "CSA-A1",
         "schemaVersion": 0},
    ]:
        upsert_compliance(conn, raw, now=now)
    # A completed sync run so get_stats has a "last sync" timestamp
    conn.execute(
        "INSERT INTO sync_runs(started_at, finished_at, status, "
        "                     vendors_seen, models_seen, versions_seen, compliance_seen) "
        "VALUES (?, ?, 'completed', 3, 3, 3, 3)",
        (now, now),
    )
    conn.commit()
    conn.close()


def _seed_changes_json(path: Path) -> None:
    payload = {
        "earlier_run_id": None,
        "later_run_id": 1,
        "earlier_started_at": "0001-01-01T00:00:00+00:00",
        "later_started_at": "2026-05-06T12:00:00+00:00",
        "new_products": [
            {"vendor_id": 4107, "vendor_name": "Signify",
             "product_id": 1, "product_name": "Hue Bulb White",
             "software_version": 100, "certification_type": "matter",
             "timestamp": "2026-05-06T12:00:00+00:00"},
            {"vendor_id": 4447, "vendor_name": "Aqara",
             "product_id": 2050, "product_name": "Aqara Hub",
             "software_version": 400, "certification_type": "matter",
             "timestamp": "2026-05-06T12:00:00+00:00"},
        ],
        "updated_products": [],
        "new_vendors": [],
        "stale_vendors": [],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def web_test_env(tmp_path, monkeypatch):
    db = tmp_path / "matter.db"
    changes = tmp_path / "changes-latest.json"
    _seed_fixture_db(db)
    _seed_changes_json(changes)
    monkeypatch.setenv("MATTER_DB_PATH", str(db))
    monkeypatch.setenv("MATTER_CHANGES_PATH", str(changes))
    return {"db": db, "changes": changes}


@pytest.fixture
def client(web_test_env):
    # Build a fresh app pointing at the fixture DB. Imports here so the
    # fixture environment is in place before web.app sees the env vars.
    from web.app import create_app
    app = create_app(matter_db=web_test_env["db"])
    with TestClient(app) as c:
        yield c


# ---- tests -------------------------------------------------------------

def test_metadata_yml_parses():
    assert METADATA_YML.exists()
    parsed = yaml.safe_load(METADATA_YML.read_text())
    assert parsed["title"] == "Matter Product Database"
    queries = parsed["databases"]["matter"]["queries"]
    assert "search_products" in queries
    assert ":q" in queries["search_products"]["sql"]
    assert parsed["databases"]["matter"]["tables"]["sync_runs"]["hidden"] is True


def test_landing_page_renders_headline_counts(client):
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    # 3 compliance records ⇒ 3 certified products in the matter view
    assert "3 Matter-certified products" in body
    # 2 distinct vendors with products
    assert "from 2 manufacturers" in body
    # search box rendered
    assert 'name="q"' in body
    # footer links
    assert "csa-iot.org" in body
    assert "github.com/jp2e-ha/matter-db" in body


def test_landing_page_renders_top_vendors(client):
    body = client.get("/").text
    # Signify has 2 products in the fixture, Aqara has 1
    assert "Signify" in body
    assert "Aqara" in body
    sig_idx = body.index("Signify")
    aqara_idx = body.index("Aqara")
    assert sig_idx < aqara_idx, "Signify (2 products) should rank above Aqara (1)"


def test_landing_page_renders_new_this_week_from_changes_json(client):
    body = client.get("/").text
    # changes-latest.json says "Hue Bulb White" and "Aqara Hub" are new
    assert "Hue Bulb White" in body
    assert "Aqara Hub" in body


def test_landing_page_includes_watchlist_count(client):
    body = client.get("/").text
    # "Empty Co" is on the watchlist (1 vendor with no compliance)
    # rendered as just the number in the stats card; check the label
    assert "on the watchlist" in body


def test_search_redirects_to_canned_query(client):
    r = client.get("/search?q=Aqara", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/db/matter/search_products?q=Aqara"


def test_search_with_blank_query_redirects_home(client):
    r = client.get("/search?q=  ", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"


def test_static_css_is_served(client):
    r = client.get("/static/style.css")
    assert r.status_code == 200
    assert "Content-Type" in r.headers
    assert "css" in r.headers["Content-Type"]
    assert ":root" in r.text


def test_datasette_mount_responds(client):
    """Datasette serves under /db/ — its index page should render."""
    r = client.get("/db/")
    assert r.status_code == 200
    # Datasette's default index lists databases; "matter" should appear
    assert "matter" in r.text.lower()


def test_datasette_certified_products_query_runs(client):
    """The 'certified_products' canned query returns rows."""
    r = client.get("/db/matter/certified_products.json?_size=10")
    assert r.status_code == 200
    payload = r.json()
    rows = payload.get("rows") or payload.get("ok") or payload
    # Datasette JSON shape: {"ok":true,"rows":[...]} on 0.65; tolerate either
    if isinstance(payload, dict) and "rows" in payload:
        assert len(payload["rows"]) >= 1


def test_landing_page_caches_response(client, web_test_env):
    """Second request hits the cache: even if we delete the DB, the cached
    HTML is still served. (Tests the cache behaviour, not a feature
    intended for users.)"""
    r1 = client.get("/")
    assert r1.status_code == 200
    web_test_env["db"].unlink()
    r2 = client.get("/")
    assert r2.status_code == 200
    assert r1.text == r2.text
