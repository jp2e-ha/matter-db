"""End-to-end async sync against the synthetic MockTransport scenario."""

from __future__ import annotations

import asyncio

import pytest

from matter_db.sync import run_sync


async def test_full_sync_counts_and_view(db_conn, scenario_client):
    report = await run_sync(db_conn, scenario_client)
    assert report.status == "completed"
    counts = report.counts
    assert counts.vendors_seen == 3
    assert counts.compliance_seen == 4
    assert counts.models_seen == 3
    assert counts.models_404 == [(9999, 7)]
    assert counts.versions_seen == 3
    assert counts.versions_404 == [(9999, 7, 300)]

    rows = db_conn.execute(
        "SELECT vendor_id, product_id, software_version, "
        "       certification_status_label, vendor_name, product_name, "
        "       certification_date "
        "FROM matter_certified_products ORDER BY vendor_id, product_id"
    ).fetchall()
    assert [(r["vendor_id"], r["product_id"], r["software_version"]) for r in rows] == [
        (1001, 1, 100),
        (1002, 5, 200),
        (9999, 7, 300),
    ]
    assert all(r["certification_status_label"] == "certified" for r in rows)

    foo = next(r for r in rows if r["vendor_id"] == 1001)
    assert foo["vendor_name"] == "Foo Corp"
    assert foo["product_name"] == "Foo Light"

    ghost = next(r for r in rows if r["vendor_id"] == 9999)
    assert ghost["vendor_name"] is None
    assert ghost["product_name"] is None
    assert ghost["certification_date"] == "2025-03-01T00:00:00.000Z"


async def test_sync_run_row_records_status_and_notes(db_conn, scenario_client):
    report = await run_sync(db_conn, scenario_client)
    assert report.status == "completed"

    row = db_conn.execute(
        "SELECT status, vendors_seen, models_seen, versions_seen, "
        "       compliance_seen, notes, error_message "
        "FROM sync_runs WHERE id = ?", (report.run_id,),
    ).fetchone()
    assert row["status"] == "completed"
    assert row["vendors_seen"] == 3
    assert row["compliance_seen"] == 4
    assert row["models_seen"] == 3
    assert row["versions_seen"] == 3
    assert row["error_message"] is None
    assert "9999/7" in row["notes"]
    assert "models 404" in row["notes"]


async def test_dry_run_rolls_back_entity_writes_but_keeps_sync_run(
    db_conn, scenario_client,
):
    report = await run_sync(db_conn, scenario_client, dry_run=True)
    assert report.status == "completed (dry-run)"

    for tbl in ("vendors", "models", "model_versions", "compliance_records"):
        n = db_conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        assert n == 0, f"{tbl} should be empty after dry-run; got {n}"

    row = db_conn.execute(
        "SELECT status, vendors_seen FROM sync_runs WHERE id = ?",
        (report.run_id,),
    ).fetchone()
    assert row["status"] == "completed (dry-run)"
    assert row["vendors_seen"] == 3


async def test_concurrent_fanout_does_not_create_duplicate_rows(db_conn, scenario):
    """Run sync twice in a row (back-to-back) with concurrent fan-out and
    assert the entity tables still satisfy their primary-key constraints
    and contain exactly the same rows.

    A bug where two coroutines simultaneously upsert the same record
    would manifest as either an IntegrityError on PK collision or as
    duplicate rows after relaxing the PK; this test exercises the same
    record twice (because we run the full sync twice) and ensures the
    second run is a no-op for row counts.
    """
    from tests.conftest import make_client, scenario_handler

    # Use a higher concurrency to maximize the chance of races.
    handler = scenario_handler(scenario)

    async with make_client(handler, max_concurrency=8) as c1:
        r1 = await run_sync(db_conn, c1, commit_every=2)
    async with make_client(handler, max_concurrency=8) as c2:
        r2 = await run_sync(db_conn, c2, commit_every=2)

    assert r1.status == "completed"
    assert r2.status == "completed"

    # Same number of rows after second run as after the first.
    counts = {}
    for tbl in ("vendors", "models", "model_versions",
                "compliance_records", "matter_certified_products"):
        counts[tbl] = db_conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]

    assert counts == {
        "vendors": 3,
        "models": 3,
        "model_versions": 3,
        "compliance_records": 4,
        "matter_certified_products": 3,
    }

    # Hash-based update detection: identical input ⇒ no last_updated_at bump.
    # Both runs should leave each row's first_seen_at == last_updated_at.
    rows = db_conn.execute(
        "SELECT first_seen_at, last_updated_at, last_seen_at FROM vendors"
    ).fetchall()
    for r in rows:
        assert r["first_seen_at"] == r["last_updated_at"], (
            "second sync should not have bumped last_updated_at "
            "for an unchanged record"
        )
        assert r["last_seen_at"] >= r["first_seen_at"]


async def test_concurrent_fanout_handles_burst_with_small_semaphore(
    db_conn, scenario,
):
    """A pathologically small Semaphore(1) (effectively serial) must still
    produce identical results to the parallel case — proves the SQLite
    write path doesn't depend on concurrency."""
    from tests.conftest import make_client, scenario_handler
    handler = scenario_handler(scenario)

    async with make_client(handler, max_concurrency=1) as client:
        report = await run_sync(db_conn, client, commit_every=2)
    assert report.status == "completed"
    assert report.counts.models_seen == 3
    assert report.counts.versions_seen == 3
