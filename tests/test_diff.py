"""Diff between two sync_runs.

Seeds two fake runs with hand-crafted entity rows so we can place a row
in any of the four diff buckets and assert categorization.
"""

from __future__ import annotations

import json

import pytest

from matter_db.diff import (
    EPOCH_SENTINEL,
    compute_diff,
    headline_for_commit,
    render_json,
    render_markdown,
    resolve_diff_window,
)
from matter_db.upsert import (
    canonical_json,
    raw_hash as compute_hash,
    upsert_compliance,
    upsert_model,
    upsert_vendor,
)

T1 = "2026-05-01T00:00:00+00:00"
T2 = "2026-05-08T00:00:00+00:00"


def _seed_run(conn, started_at: str, status: str = "completed") -> int:
    cur = conn.execute(
        "INSERT INTO sync_runs(started_at, finished_at, status, "
        "                      vendors_seen, models_seen, versions_seen, compliance_seen) "
        "VALUES (?, ?, ?, 0, 0, 0, 0)",
        (started_at, started_at, status),
    )
    return cur.lastrowid


def _seed_vendor(conn, *, vendor_id, vendor_name,
                 first_seen, last_seen=None, last_updated=None):
    last_seen = last_seen or first_seen
    last_updated = last_updated or first_seen
    raw = {"vendorID": vendor_id, "vendorName": vendor_name,
           "companyLegalName": vendor_name, "vendorLandingPageURL": ""}
    conn.execute(
        "INSERT INTO vendors(vendor_id, vendor_name, company_legal_name, "
        "                    landing_url, raw_json, raw_hash, "
        "                    first_seen_at, last_seen_at, last_updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (vendor_id, vendor_name, vendor_name, "",
         canonical_json(raw), compute_hash(raw),
         first_seen, last_seen, last_updated),
    )


def _seed_compliance(conn, *, vid, pid, sv, ctype="matter",
                     first_seen, last_seen=None, last_updated=None,
                     status=2):
    last_seen = last_seen or first_seen
    last_updated = last_updated or first_seen
    raw = {"vid": vid, "pid": pid, "softwareVersion": sv,
           "certificationType": ctype,
           "softwareVersionCertificationStatus": status,
           "date": "2025-01-01T00:00:00.000Z",
           "cDCertificateId": f"CSA-{vid}-{pid}-{sv}"}
    conn.execute(
        "INSERT INTO compliance_records(vendor_id, product_id, software_version, "
        "                                certification_type, certification_status, "
        "                                date, certificate_id, "
        "                                raw_json, raw_hash, "
        "                                first_seen_at, last_seen_at, last_updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (vid, pid, sv, ctype, status, raw["date"], raw["cDCertificateId"],
         canonical_json(raw), compute_hash(raw),
         first_seen, last_seen, last_updated),
    )


def _seed_model(conn, *, vid, pid, name, first_seen):
    raw = {"vid": vid, "pid": pid, "productName": name, "deviceTypeId": 1}
    conn.execute(
        "INSERT INTO models(vendor_id, product_id, product_name, "
        "                   raw_json, raw_hash, "
        "                   first_seen_at, last_seen_at, last_updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (vid, pid, name, canonical_json(raw), compute_hash(raw),
         first_seen, first_seen, first_seen),
    )


def test_categorizes_each_kind_of_change(db_conn):
    run1 = _seed_run(db_conn, T1)
    run2 = _seed_run(db_conn, T2)

    # Vendor 1: existed before run1, still observed at run2 → quiet (no diff bucket).
    _seed_vendor(db_conn, vendor_id=1, vendor_name="Old Co",
                 first_seen=T1, last_seen=T2, last_updated=T1)
    # Vendor 2: NEW — first_seen between T1 and T2 (== T2).
    _seed_vendor(db_conn, vendor_id=2, vendor_name="New Co",
                 first_seen=T2, last_seen=T2, last_updated=T2)
    # Vendor 3: STALE — last_seen at T1, never seen at T2.
    _seed_vendor(db_conn, vendor_id=3, vendor_name="Gone Co",
                 first_seen=T1, last_seen=T1, last_updated=T1)

    # Compliance row A: NEW product (first_seen at T2).
    _seed_model(db_conn, vid=2, pid=1, name="Brand-new gadget", first_seen=T2)
    _seed_compliance(db_conn, vid=2, pid=1, sv=100,
                     first_seen=T2, last_seen=T2, last_updated=T2)
    # Compliance row B: UPDATED — first_seen at T1 (existed), last_updated at T2.
    _seed_model(db_conn, vid=1, pid=10, name="Old gadget", first_seen=T1)
    _seed_compliance(db_conn, vid=1, pid=10, sv=200,
                     first_seen=T1, last_seen=T2, last_updated=T2)
    # Compliance row C: unchanged — last_updated at T1.
    _seed_compliance(db_conn, vid=1, pid=11, sv=300,
                     first_seen=T1, last_seen=T2, last_updated=T1)

    earlier_id, earlier_t, later_id, later_t = resolve_diff_window(
        db_conn, since_last=True,
    )
    assert (earlier_id, later_id) == (run1, run2)

    report = compute_diff(
        db_conn, earlier_t, later_t,
        earlier_run_id=earlier_id, later_run_id=later_id,
    )

    assert report.counts == {
        "new_vendors": 1,
        "stale_vendors": 1,
        "new_products": 1,
        "updated_products": 1,
    }
    assert {v.vendor_id for v in report.new_vendors} == {2}
    assert {v.vendor_id for v in report.stale_vendors} == {3}
    assert {(p.vendor_id, p.product_id) for p in report.new_products} == {(2, 1)}
    assert {(p.vendor_id, p.product_id) for p in report.updated_products} == {(1, 10)}

    # joined-in name from models table appears in the new_products row
    np = report.new_products[0]
    assert np.product_name == "Brand-new gadget"
    assert np.software_version == 100


def test_first_run_treats_everything_as_new(db_conn):
    """When only one completed run exists, --since-last falls back to
    EPOCH_SENTINEL and every row in the DB shows up as new."""
    run = _seed_run(db_conn, T2)
    _seed_vendor(db_conn, vendor_id=42, vendor_name="Solo",
                 first_seen=T2, last_seen=T2, last_updated=T2)
    _seed_compliance(db_conn, vid=42, pid=7, sv=10,
                     first_seen=T2, last_seen=T2, last_updated=T2)

    earlier_id, earlier_t, later_id, later_t = resolve_diff_window(
        db_conn, since_last=True,
    )
    assert earlier_id is None
    assert earlier_t == EPOCH_SENTINEL
    assert later_id == run

    report = compute_diff(
        db_conn, earlier_t, later_t,
        earlier_run_id=earlier_id, later_run_id=later_id,
    )
    assert report.counts["new_vendors"] == 1
    assert report.counts["new_products"] == 1
    assert report.counts["stale_vendors"] == 0


def test_resolve_diff_window_with_explicit_since(db_conn):
    r1 = _seed_run(db_conn, "2026-04-01T00:00:00+00:00")
    r2 = _seed_run(db_conn, "2026-04-15T00:00:00+00:00")
    r3 = _seed_run(db_conn, T2)
    earlier_id, _, later_id, _ = resolve_diff_window(db_conn, since=r1)
    assert (earlier_id, later_id) == (r1, r3)


def test_renderers_produce_well_formed_output(db_conn):
    """Smoke: markdown/json/headline don't blow up and contain the headline counts."""
    run1 = _seed_run(db_conn, T1)
    run2 = _seed_run(db_conn, T2)
    _seed_vendor(db_conn, vendor_id=1, vendor_name="Apple-like",
                 first_seen=T2, last_seen=T2, last_updated=T2)
    earlier_id, earlier_t, later_id, later_t = resolve_diff_window(
        db_conn, since_last=True,
    )
    report = compute_diff(
        db_conn, earlier_t, later_t,
        earlier_run_id=earlier_id, later_run_id=later_id,
    )
    md = render_markdown(report)
    assert "matter-db sync diff" in md
    assert "| new vendors | 1 |" in md
    assert "Apple-like" in md

    js = render_json(report)
    parsed = json.loads(js)
    assert parsed["earlier_run_id"] == earlier_id
    assert parsed["later_run_id"] == later_id
    assert len(parsed["new_vendors"]) == 1

    line = headline_for_commit(report)
    assert line.startswith("sync: ")
    assert f"run {later_id}" in line


def test_markdown_truncates_with_more_footer(db_conn):
    """20-row cap plus '…and N more' footer."""
    _seed_run(db_conn, T1)
    run2 = _seed_run(db_conn, T2)
    for i in range(25):
        _seed_vendor(db_conn, vendor_id=1000 + i, vendor_name=f"V{i}",
                     first_seen=T2, last_seen=T2, last_updated=T2)
    earlier_id, earlier_t, later_id, later_t = resolve_diff_window(
        db_conn, since_last=True,
    )
    report = compute_diff(
        db_conn, earlier_t, later_t,
        earlier_run_id=earlier_id, later_run_id=later_id,
    )
    assert report.counts["new_vendors"] == 25
    md = render_markdown(report)
    # showing first 20, plus footer about 5 more
    assert "and 5 more" in md
