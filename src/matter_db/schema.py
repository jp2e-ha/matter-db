"""Schema for the local DCL mirror.

Plain sqlite3 (stdlib): the schema is fixed, explicit, and small. There's
no dynamic-shape data to make sqlite-utils worth a dependency.

Every entity table follows the same shape: a natural primary key (whatever
the DCL uses), a handful of flat columns lifted out of the raw JSON for
fast querying, and three columns for change-tracking:

    raw_json         — the canonical-JSON-serialized record from DCL
    raw_hash         — SHA-256 of raw_json, used to detect changes
    first_seen_at    — set once on first insert
    last_seen_at     — bumped on every sync that observes the row
    last_updated_at  — bumped only when raw_hash changes
"""

from __future__ import annotations

import sqlite3

# DDL is split into small statements so each is independently inspectable
# and so failures point to a specific statement.

DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS vendors (
        vendor_id           INTEGER PRIMARY KEY,
        vendor_name         TEXT,
        company_legal_name  TEXT,
        landing_url         TEXT,
        raw_json            TEXT NOT NULL,
        raw_hash            TEXT NOT NULL,
        first_seen_at       TEXT NOT NULL,
        last_seen_at        TEXT NOT NULL,
        last_updated_at     TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS models (
        vendor_id        INTEGER NOT NULL,
        product_id       INTEGER NOT NULL,
        product_name     TEXT,
        product_label    TEXT,
        part_number      TEXT,
        product_url      TEXT,
        device_type_id   INTEGER,
        raw_json         TEXT NOT NULL,
        raw_hash         TEXT NOT NULL,
        first_seen_at    TEXT NOT NULL,
        last_seen_at     TEXT NOT NULL,
        last_updated_at  TEXT NOT NULL,
        PRIMARY KEY (vendor_id, product_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS model_versions (
        vendor_id                INTEGER NOT NULL,
        product_id               INTEGER NOT NULL,
        software_version         INTEGER NOT NULL,
        software_version_string  TEXT,
        cd_version_number        INTEGER,
        ota_url                  TEXT,
        firmware_information     TEXT,
        raw_json                 TEXT NOT NULL,
        raw_hash                 TEXT NOT NULL,
        first_seen_at            TEXT NOT NULL,
        last_seen_at             TEXT NOT NULL,
        last_updated_at          TEXT NOT NULL,
        PRIMARY KEY (vendor_id, product_id, software_version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS compliance_records (
        vendor_id            INTEGER NOT NULL,
        product_id           INTEGER NOT NULL,
        software_version     INTEGER NOT NULL,
        certification_type   TEXT    NOT NULL,
        certification_status INTEGER,
        date                 TEXT,
        reason               TEXT,
        certificate_id       TEXT,
        raw_json             TEXT NOT NULL,
        raw_hash             TEXT NOT NULL,
        first_seen_at        TEXT NOT NULL,
        last_seen_at         TEXT NOT NULL,
        last_updated_at      TEXT NOT NULL,
        PRIMARY KEY (vendor_id, product_id, software_version, certification_type)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cert_status_lookup (
        status_int INTEGER PRIMARY KEY,
        label      TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sync_runs (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at      TEXT NOT NULL,
        finished_at     TEXT,
        status          TEXT NOT NULL,
        error_message   TEXT,
        vendors_seen    INTEGER NOT NULL DEFAULT 0,
        models_seen     INTEGER NOT NULL DEFAULT 0,
        versions_seen   INTEGER NOT NULL DEFAULT 0,
        compliance_seen INTEGER NOT NULL DEFAULT 0,
        notes           TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_models_vendor          ON models(vendor_id)",
    "CREATE INDEX IF NOT EXISTS idx_versions_vid_pid       ON model_versions(vendor_id, product_id)",
    "CREATE INDEX IF NOT EXISTS idx_compliance_vid_pid     ON compliance_records(vendor_id, product_id)",
    "CREATE INDEX IF NOT EXISTS idx_compliance_status_type ON compliance_records(certification_type, certification_status)",
]

# Seed values for the cert_status_lookup. We only know status=2 from the
# wild as of recon; anything else is left to surface as NULL via LEFT JOIN.
CERT_STATUS_SEED: list[tuple[int, str]] = [
    (2, "certified"),
]

VIEW_DDL = """
CREATE VIEW IF NOT EXISTS matter_certified_products AS
SELECT
    c.vendor_id,
    c.product_id,
    c.software_version,
    c.certification_type,
    c.certification_status,
    csl.label                  AS certification_status_label,
    c.date                     AS certification_date,
    c.certificate_id,
    v.vendor_name,
    v.company_legal_name,
    m.product_name,
    m.product_label,
    m.part_number,
    m.product_url,
    m.device_type_id,
    mv.software_version_string,
    mv.cd_version_number,
    mv.ota_url,
    mv.firmware_information,
    c.last_updated_at
FROM compliance_records AS c
LEFT JOIN cert_status_lookup AS csl ON csl.status_int = c.certification_status
LEFT JOIN vendors            AS v   ON v.vendor_id = c.vendor_id
LEFT JOIN models             AS m   ON m.vendor_id = c.vendor_id
                                   AND m.product_id = c.product_id
LEFT JOIN model_versions     AS mv  ON mv.vendor_id        = c.vendor_id
                                   AND mv.product_id       = c.product_id
                                   AND mv.software_version = c.software_version
WHERE LOWER(c.certification_type) = 'matter'
  AND c.certification_status      = 2
"""


def connect(db_path: str) -> sqlite3.Connection:
    """Open a connection with sensible defaults."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def initialize(conn: sqlite3.Connection) -> None:
    """Create tables, seed the lookup, create the view. Idempotent."""
    with conn:
        for stmt in DDL_STATEMENTS:
            conn.execute(stmt)
        conn.executemany(
            "INSERT OR IGNORE INTO cert_status_lookup(status_int, label) VALUES (?, ?)",
            CERT_STATUS_SEED,
        )
        conn.execute(VIEW_DDL)
