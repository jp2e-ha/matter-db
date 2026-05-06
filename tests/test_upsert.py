"""Idempotent-upsert behaviour: hash-driven last_updated_at, always-bump last_seen_at."""

from __future__ import annotations

from matter_db.upsert import (
    upsert_vendor,
    upsert_model,
    upsert_compliance,
)


VENDOR_RAW = {
    "vendorID": 4447,
    "vendorName": "Aqara",
    "companyLegalName": "Lumi United Technology Co., Ltd.",
    "vendorLandingPageURL": "https://aqara.com/",
    "creator": "cosmos1abc",
    "schemaVersion": 0,
}


def test_first_insert_sets_all_three_timestamps_equal(db_conn):
    res = upsert_vendor(db_conn, VENDOR_RAW, now="2026-05-06T00:00:00+00:00")
    assert res.inserted and not res.updated
    row = db_conn.execute(
        "SELECT first_seen_at, last_seen_at, last_updated_at, vendor_name "
        "FROM vendors WHERE vendor_id = ?",
        (4447,),
    ).fetchone()
    assert row["first_seen_at"] == "2026-05-06T00:00:00+00:00"
    assert row["last_seen_at"] == "2026-05-06T00:00:00+00:00"
    assert row["last_updated_at"] == "2026-05-06T00:00:00+00:00"
    assert row["vendor_name"] == "Aqara"


def test_identical_record_only_bumps_last_seen_at(db_conn):
    upsert_vendor(db_conn, VENDOR_RAW, now="2026-05-06T00:00:00+00:00")
    res = upsert_vendor(db_conn, VENDOR_RAW, now="2026-05-07T00:00:00+00:00")
    assert not res.inserted and not res.updated

    row = db_conn.execute(
        "SELECT first_seen_at, last_seen_at, last_updated_at FROM vendors "
        "WHERE vendor_id = ?", (4447,),
    ).fetchone()
    assert row["first_seen_at"] == "2026-05-06T00:00:00+00:00"
    assert row["last_seen_at"] == "2026-05-07T00:00:00+00:00"
    assert row["last_updated_at"] == "2026-05-06T00:00:00+00:00"


def test_changed_record_bumps_last_updated_and_flat_columns(db_conn):
    upsert_vendor(db_conn, VENDOR_RAW, now="2026-05-06T00:00:00+00:00")

    changed = dict(VENDOR_RAW)
    changed["vendorName"] = "Aqara (Lumi)"
    changed["vendorLandingPageURL"] = "https://aqara.com/global"
    res = upsert_vendor(db_conn, changed, now="2026-05-07T00:00:00+00:00")
    assert not res.inserted
    assert res.updated

    row = db_conn.execute(
        "SELECT first_seen_at, last_seen_at, last_updated_at, "
        "       vendor_name, landing_url "
        "FROM vendors WHERE vendor_id = ?", (4447,),
    ).fetchone()
    assert row["first_seen_at"] == "2026-05-06T00:00:00+00:00"
    assert row["last_seen_at"] == "2026-05-07T00:00:00+00:00"
    assert row["last_updated_at"] == "2026-05-07T00:00:00+00:00"
    assert row["vendor_name"] == "Aqara (Lumi)"
    assert row["landing_url"] == "https://aqara.com/global"


def test_canonical_hash_is_key_order_independent(db_conn):
    upsert_vendor(db_conn, VENDOR_RAW, now="2026-05-06T00:00:00+00:00")
    # same data, different dict construction order — hash must match,
    # so this counts as "no change" rather than "updated"
    reordered = {k: VENDOR_RAW[k] for k in reversed(list(VENDOR_RAW))}
    res = upsert_vendor(db_conn, reordered, now="2026-05-07T00:00:00+00:00")
    assert not res.updated

    row = db_conn.execute(
        "SELECT last_updated_at FROM vendors WHERE vendor_id = ?", (4447,),
    ).fetchone()
    assert row["last_updated_at"] == "2026-05-06T00:00:00+00:00"


def test_compliance_extracts_certificate_id_and_status(db_conn):
    raw = {
        "vid": 4447, "pid": 2050, "softwareVersion": 400,
        "certificationType": "matter",
        "softwareVersionCertificationStatus": 2,
        "date": "2022-11-03T00:00:00.000Z",
        "cDCertificateId": "CSA22083MAT40083-24",
        "history": [], "schemaVersion": 0,
    }
    upsert_compliance(db_conn, raw, now="2026-05-06T00:00:00+00:00")
    row = db_conn.execute(
        "SELECT certification_status, certificate_id, date "
        "FROM compliance_records "
        "WHERE vendor_id = ? AND product_id = ? AND software_version = ? "
        "  AND certification_type = ?",
        (4447, 2050, 400, "matter"),
    ).fetchone()
    assert row["certification_status"] == 2
    assert row["certificate_id"] == "CSA22083MAT40083-24"
    assert row["date"] == "2022-11-03T00:00:00.000Z"


def test_model_flat_columns(db_conn):
    raw = {
        "vid": 4447, "pid": 2050, "deviceTypeId": 14,
        "productName": "Aqara Hub", "productLabel": "Aqara Hub",
        "partNumber": "AG035", "productUrl": "https://aqara.com/hub",
        "schemaVersion": 0,
    }
    upsert_model(db_conn, raw, now="2026-05-06T00:00:00+00:00")
    row = db_conn.execute(
        "SELECT product_name, part_number, device_type_id, product_url "
        "FROM models WHERE vendor_id = ? AND product_id = ?",
        (4447, 2050),
    ).fetchone()
    assert row["product_name"] == "Aqara Hub"
    assert row["part_number"] == "AG035"
    assert row["device_type_id"] == 14
    assert row["product_url"] == "https://aqara.com/hub"
