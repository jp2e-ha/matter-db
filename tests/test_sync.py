"""End-to-end: run sync against the synthetic MockTransport scenario, assert
row counts and that the matter_certified_products view returns the right set."""

from __future__ import annotations

from matter_db.sync import run_sync


def test_full_sync_counts_and_view(db_conn, scenario_client):
    report = run_sync(db_conn, scenario_client)
    assert report.status == "completed"
    counts = report.counts
    assert counts.vendors_seen == 3
    assert counts.compliance_seen == 4
    # 3 unique (vid,pid) reachable models; (9999,7) is the 404 ⇒ models_seen=3
    assert counts.models_seen == 3
    assert counts.models_404 == [(9999, 7)]
    # 3 unique (vid,pid,sv) reachable versions; (9999,7,300) is 404
    assert counts.versions_seen == 3
    assert counts.versions_404 == [(9999, 7, 300)]

    # the headline view: only matter + certified rows
    rows = db_conn.execute(
        "SELECT vendor_id, product_id, software_version, "
        "       certification_status_label, vendor_name, product_name, "
        "       certification_date "
        "FROM matter_certified_products ORDER BY vendor_id, product_id"
    ).fetchall()
    assert [(r["vendor_id"], r["product_id"], r["software_version"]) for r in rows] == [
        (1001, 1, 100),
        (1002, 5, 200),
        # zigbee row excluded; ghost (9999,7,300) included even though no
        # model row exists — the LEFT JOIN preserves the compliance record
        (9999, 7, 300),
    ]

    # status label resolved via lookup table
    assert all(r["certification_status_label"] == "certified" for r in rows)

    # vendor + model joined for the rows that have them
    foo = next(r for r in rows if r["vendor_id"] == 1001)
    assert foo["vendor_name"] == "Foo Corp"
    assert foo["product_name"] == "Foo Light"

    # ghost row: no vendor / model joined, but compliance fields present
    ghost = next(r for r in rows if r["vendor_id"] == 9999)
    assert ghost["vendor_name"] is None
    assert ghost["product_name"] is None
    assert ghost["certification_date"] == "2025-03-01T00:00:00.000Z"


def test_sync_run_row_records_status_and_notes(db_conn, scenario_client):
    report = run_sync(db_conn, scenario_client)
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


def test_dry_run_rolls_back_entity_writes_but_keeps_sync_run(
    db_conn, scenario_client,
):
    report = run_sync(db_conn, scenario_client, dry_run=True)
    assert report.status == "completed (dry-run)"

    # entity tables empty — rollback worked
    for tbl in ("vendors", "models", "model_versions", "compliance_records"):
        n = db_conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        assert n == 0, f"{tbl} should be empty after dry-run; got {n}"

    # but the sync_runs row is recorded
    row = db_conn.execute(
        "SELECT status, vendors_seen FROM sync_runs WHERE id = ?",
        (report.run_id,),
    ).fetchone()
    assert row["status"] == "completed (dry-run)"
    assert row["vendors_seen"] == 3  # in-memory count is reported
