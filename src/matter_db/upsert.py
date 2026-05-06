"""Idempotent upsert with hash-based change detection.

Three timestamp invariants per row:

  - first_seen_at: set on first insert, never modified.
  - last_seen_at:  always bumped to "now" on every observation, even if
                   the record is byte-identical.
  - last_updated_at: bumped only when the SHA-256 of canonical JSON
                     differs from the stored hash.

Canonical JSON: sort_keys=True, compact separators. This makes the hash
stable across Python runs regardless of dict iteration order.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def raw_hash(data: Any) -> str:
    return hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()


@dataclass
class UpsertResult:
    inserted: bool
    updated: bool       # raw_hash changed
    seen: bool = True   # always true when called


@dataclass
class TableSpec:
    """Describes a table for the generic _upsert function."""
    name: str
    pk_cols: tuple[str, ...]
    flat_cols: tuple[str, ...]   # excludes raw_json/raw_hash/timestamps


# ---- per-entity field extraction ---------------------------------------

def vendor_flat(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "vendor_id": raw.get("vendorID"),
        "vendor_name": raw.get("vendorName"),
        "company_legal_name": raw.get("companyLegalName"),
        "landing_url": raw.get("vendorLandingPageURL"),
    }


def model_flat(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "vendor_id": raw.get("vid"),
        "product_id": raw.get("pid"),
        "product_name": raw.get("productName"),
        "product_label": raw.get("productLabel"),
        "part_number": raw.get("partNumber"),
        "product_url": raw.get("productUrl"),
        "device_type_id": raw.get("deviceTypeId"),
    }


def model_version_flat(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "vendor_id": raw.get("vid"),
        "product_id": raw.get("pid"),
        "software_version": raw.get("softwareVersion"),
        "software_version_string": raw.get("softwareVersionString"),
        "cd_version_number": raw.get("cdVersionNumber"),
        "ota_url": raw.get("otaUrl"),
        "firmware_information": raw.get("firmwareInformation"),
    }


def compliance_flat(raw: dict[str, Any]) -> dict[str, Any]:
    # The spec's status field is named softwareVersionCertificationStatus.
    # Some compliance-info responses also carry cDCertificateId for the
    # CSA-issued cert id.
    return {
        "vendor_id": raw.get("vid"),
        "product_id": raw.get("pid"),
        "software_version": raw.get("softwareVersion"),
        "certification_type": raw.get("certificationType"),
        "certification_status": raw.get("softwareVersionCertificationStatus"),
        "date": raw.get("date"),
        "reason": raw.get("reason"),
        "certificate_id": raw.get("cDCertificateId"),
    }


VENDOR_SPEC = TableSpec(
    name="vendors",
    pk_cols=("vendor_id",),
    flat_cols=("vendor_name", "company_legal_name", "landing_url"),
)
MODEL_SPEC = TableSpec(
    name="models",
    pk_cols=("vendor_id", "product_id"),
    flat_cols=("product_name", "product_label", "part_number",
               "product_url", "device_type_id"),
)
MODEL_VERSION_SPEC = TableSpec(
    name="model_versions",
    pk_cols=("vendor_id", "product_id", "software_version"),
    flat_cols=("software_version_string", "cd_version_number",
               "ota_url", "firmware_information"),
)
COMPLIANCE_SPEC = TableSpec(
    name="compliance_records",
    pk_cols=("vendor_id", "product_id", "software_version", "certification_type"),
    flat_cols=("certification_status", "date", "reason", "certificate_id"),
)


# ---- the generic upsert ------------------------------------------------

def _upsert(
    conn: sqlite3.Connection,
    spec: TableSpec,
    flat: dict[str, Any],
    raw: dict[str, Any],
    *,
    now: str,
) -> UpsertResult:
    pk_values = tuple(flat[c] for c in spec.pk_cols)
    if any(v is None for v in pk_values):
        raise ValueError(
            f"{spec.name}: missing primary-key value(s) in {flat!r}"
        )

    raw_text = canonical_json(raw)
    h = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()

    pk_where = " AND ".join(f"{c} = ?" for c in spec.pk_cols)
    cur = conn.execute(
        f"SELECT raw_hash FROM {spec.name} WHERE {pk_where}", pk_values,
    )
    existing = cur.fetchone()

    if existing is None:
        cols = (*spec.pk_cols, *spec.flat_cols,
                "raw_json", "raw_hash",
                "first_seen_at", "last_seen_at", "last_updated_at")
        values = (
            *pk_values,
            *(flat[c] for c in spec.flat_cols),
            raw_text, h,
            now, now, now,
        )
        placeholders = ",".join("?" * len(cols))
        conn.execute(
            f"INSERT INTO {spec.name} ({','.join(cols)}) VALUES ({placeholders})",
            values,
        )
        return UpsertResult(inserted=True, updated=False)

    if existing["raw_hash"] == h:
        # same record — bump last_seen_at only.
        conn.execute(
            f"UPDATE {spec.name} SET last_seen_at = ? WHERE {pk_where}",
            (now, *pk_values),
        )
        return UpsertResult(inserted=False, updated=False)

    # changed — bump everything.
    set_cols = (*spec.flat_cols, "raw_json", "raw_hash",
                "last_seen_at", "last_updated_at")
    set_clause = ", ".join(f"{c} = ?" for c in set_cols)
    set_values = (
        *(flat[c] for c in spec.flat_cols),
        raw_text, h,
        now, now,
    )
    conn.execute(
        f"UPDATE {spec.name} SET {set_clause} WHERE {pk_where}",
        (*set_values, *pk_values),
    )
    return UpsertResult(inserted=False, updated=True)


# ---- per-entity wrappers ----------------------------------------------

def upsert_vendor(conn, raw, *, now: str | None = None) -> UpsertResult:
    return _upsert(conn, VENDOR_SPEC, vendor_flat(raw), raw,
                   now=now or now_iso())


def upsert_model(conn, raw, *, now: str | None = None) -> UpsertResult:
    return _upsert(conn, MODEL_SPEC, model_flat(raw), raw,
                   now=now or now_iso())


def upsert_model_version(conn, raw, *, now: str | None = None) -> UpsertResult:
    return _upsert(conn, MODEL_VERSION_SPEC, model_version_flat(raw), raw,
                   now=now or now_iso())


def upsert_compliance(conn, raw, *, now: str | None = None) -> UpsertResult:
    return _upsert(conn, COMPLIANCE_SPEC, compliance_flat(raw), raw,
                   now=now or now_iso())
