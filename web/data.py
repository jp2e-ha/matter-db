"""Database query helpers and a tiny in-memory response cache.

The web layer reads from data/matter.db and changes-latest.json. Both
file paths are resolved per-call so tests can swap them via env vars
without re-importing the module.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent

CACHE_TTL = 300  # 5 minutes


def db_path() -> Path:
    """Resolved at call time so tests can override via $MATTER_DB_PATH."""
    return Path(os.getenv("MATTER_DB_PATH", PROJECT_ROOT / "data" / "matter.db"))


def changes_path() -> Path:
    return Path(os.getenv("MATTER_CHANGES_PATH",
                          PROJECT_ROOT / "changes-latest.json"))


def _connect() -> sqlite3.Connection:
    """Open the DB read-only via a URI so the web layer cannot mutate it."""
    p = db_path()
    if not p.exists():
        raise FileNotFoundError(f"matter.db not found at {p}")
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


T = TypeVar("T")


def cached(cache: dict[str, tuple[float, Any]], key: str, fn: Callable[[], T]) -> T:
    now = time.monotonic()
    entry = cache.get(key)
    if entry and (now - entry[0]) < CACHE_TTL:
        return entry[1]
    val = fn()
    cache[key] = (now, val)
    return val


# ---- queries ----------------------------------------------------------

def get_stats() -> dict[str, Any]:
    conn = _connect()
    try:
        n_products = conn.execute(
            "SELECT COUNT(*) FROM matter_certified_products"
        ).fetchone()[0]
        n_vendors_total = conn.execute(
            "SELECT COUNT(*) FROM vendors"
        ).fetchone()[0]
        n_vendors_with_products = conn.execute(
            "SELECT COUNT(DISTINCT vendor_id) FROM matter_certified_products"
        ).fetchone()[0]
        n_watchlist = conn.execute(
            "SELECT COUNT(*) FROM matter_vendor_watchlist"
        ).fetchone()[0]

        last_sync_row = conn.execute(
            "SELECT started_at, finished_at FROM sync_runs "
            "WHERE status='completed' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        last_sync = last_sync_row["started_at"] if last_sync_row else None

        # "Last database change" — distinct from the sync *check* above. A
        # sync runs daily but only mutates a row's last_updated_at when its
        # content actually changed, so the newest last_updated_at across the
        # change-tracked tables is when the data last moved.
        last_change_row = conn.execute(
            "SELECT MAX(t) FROM ("
            "  SELECT MAX(last_updated_at) AS t FROM compliance_records "
            "  UNION ALL SELECT MAX(last_updated_at) FROM model_versions "
            "  UNION ALL SELECT MAX(last_updated_at) FROM models "
            "  UNION ALL SELECT MAX(last_updated_at) FROM vendors"
            ")"
        ).fetchone()
        last_change = last_change_row[0] if last_change_row else None

        # "Certified in the last 7 days" — based on the CSA-issued
        # certification date, not our sync first_seen_at, so the number is
        # stable across DB rebuilds and means what visitors expect it to.
        # DCL stores `date` as "YYYY-MM-DDTHH:MM:SS.sssZ"; we compare
        # against a YYYY-MM-DD prefix and rely on lexicographic order.
        cutoff_date = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
        n_added_7d = conn.execute(
            "SELECT COUNT(*) FROM compliance_records WHERE date >= ?",
            (cutoff_date,),
        ).fetchone()[0]

        return {
            "products": n_products,
            "vendors_total": n_vendors_total,
            "vendors_with_products": n_vendors_with_products,
            "vendors_watchlist": n_watchlist,
            "added_7d": n_added_7d,
            "last_sync": last_sync,
            "last_sync_relative": _relative_time(last_sync),
            "last_change": last_change,
            "last_change_relative": _relative_time(last_change),
        }
    finally:
        conn.close()


def get_top_vendors(limit: int = 10) -> list[dict[str, Any]]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT vendor_id, vendor_name, COUNT(*) AS n "
            "FROM matter_certified_products "
            "GROUP BY vendor_id ORDER BY n DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_new_this_week(limit: int = 15) -> list[dict[str, Any]]:
    """Prefer the diff JSON so we show the actual sync's "new" set; fall
    back to a 7-day SQL window if the diff isn't available (first run)."""
    cj = changes_path()
    if cj.exists():
        try:
            data = json.loads(cj.read_text(encoding="utf-8"))
            new_products = data.get("new_products") or []
            if new_products:
                return new_products[:limit]
        except (json.JSONDecodeError, ValueError):
            pass

    conn = _connect()
    try:
        cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        rows = conn.execute(
            "SELECT c.vendor_id, c.product_id, c.software_version, "
            "       c.certification_type, "
            "       v.vendor_name, m.product_name, c.first_seen_at AS timestamp "
            "FROM compliance_records c "
            "LEFT JOIN vendors v ON v.vendor_id = c.vendor_id "
            "LEFT JOIN models  m ON m.vendor_id = c.vendor_id "
            "                   AND m.product_id = c.product_id "
            "WHERE c.first_seen_at >= ? "
            "ORDER BY c.first_seen_at DESC LIMIT ?",
            (cutoff_iso, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---- helpers ----------------------------------------------------------

def _relative_time(iso_string: str | None) -> str:
    if not iso_string:
        return "never"
    try:
        # tolerate +00:00 and Z suffixes
        s = iso_string.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
    except ValueError:
        return iso_string
    delta = datetime.now(timezone.utc) - dt
    secs = int(delta.total_seconds())
    if secs < 60:
        return "just now"
    if secs < 3600:
        m = secs // 60
        return f"{m} minute{'s' if m != 1 else ''} ago"
    if secs < 86400:
        h = secs // 3600
        return f"{h} hour{'s' if h != 1 else ''} ago"
    days = secs // 86400
    return f"{days} day{'s' if days != 1 else ''} ago"
