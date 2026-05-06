"""Diff between two sync_runs.

The change-detection model is built into the entity tables:
  - first_seen_at  — set once on insert, equal to the run's started_at.
  - last_updated_at — bumped on every hash change, equal to the run's started_at.
  - last_seen_at   — bumped on every observation.

So "new in window" = first_seen_at falls in (earlier, later].
"Updated in window" = last_updated_at falls in (earlier, later] AND the
row already existed before the window (otherwise it'd just be New).
"Stale" = last_seen_at < later.started_at (i.e. the most recent run did
not observe this row — could mean the DCL hiccupped, or genuinely gone).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Sequence

# SQLite ISO 8601 strings sort lexicographically, so this sentinel
# predates any real sync_runs.started_at value.
EPOCH_SENTINEL = "0001-01-01T00:00:00+00:00"
MAX_EXAMPLES = 20


@dataclass
class DiffRow:
    """Generic row shape used in all four diff sections."""
    vendor_id: int | None = None
    vendor_name: str | None = None
    product_id: int | None = None
    product_name: str | None = None
    software_version: int | None = None
    certification_type: str | None = None
    timestamp: str | None = None  # first_seen_at / last_updated_at / last_seen_at


@dataclass
class DiffReport:
    earlier_run_id: int | None
    later_run_id: int
    earlier_started_at: str
    later_started_at: str
    new_vendors: list[DiffRow] = field(default_factory=list)
    stale_vendors: list[DiffRow] = field(default_factory=list)
    new_products: list[DiffRow] = field(default_factory=list)
    updated_products: list[DiffRow] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        return {
            "new_vendors": len(self.new_vendors),
            "stale_vendors": len(self.stale_vendors),
            "new_products": len(self.new_products),
            "updated_products": len(self.updated_products),
        }


# ---- run resolution helpers -------------------------------------------

def _completed_runs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Most-recent-first list of completed (non-dry-run) sync runs."""
    return list(conn.execute(
        "SELECT id, started_at FROM sync_runs "
        "WHERE status = 'completed' ORDER BY id DESC"
    ))


def resolve_diff_window(
    conn: sqlite3.Connection,
    *,
    since: int | None = None,
    since_last: bool = False,
) -> tuple[int | None, str, int, str]:
    """Pick (earlier_id, earlier_started_at, later_id, later_started_at).

    - `since=N` ⇒ earlier = run N (by id), later = most recent.
    - `since_last=True` ⇒ earlier = second-most-recent, later = most recent.
    - both unset is also treated as `since_last`.
    - If there's only one completed run, earlier_id is None and
      earlier_started_at is EPOCH_SENTINEL ("everything is new").
    """
    runs = _completed_runs(conn)
    if not runs:
        raise SystemExit("no completed sync runs in the database")

    later = runs[0]

    if since is not None:
        earlier_row = conn.execute(
            "SELECT id, started_at FROM sync_runs WHERE id = ?", (since,),
        ).fetchone()
        if earlier_row is None:
            raise SystemExit(f"no sync_run with id={since}")
        return (earlier_row["id"], earlier_row["started_at"],
                later["id"], later["started_at"])

    if len(runs) >= 2:
        earlier = runs[1]
        return (earlier["id"], earlier["started_at"],
                later["id"], later["started_at"])

    # only the latest run exists
    return (None, EPOCH_SENTINEL, later["id"], later["started_at"])


# ---- the diff queries -------------------------------------------------

def compute_diff(
    conn: sqlite3.Connection,
    earlier_started_at: str,
    later_started_at: str,
    *,
    earlier_run_id: int | None,
    later_run_id: int,
) -> DiffReport:
    new_vendors = [
        DiffRow(
            vendor_id=r["vendor_id"], vendor_name=r["vendor_name"],
            timestamp=r["first_seen_at"],
        )
        for r in conn.execute(
            "SELECT vendor_id, vendor_name, first_seen_at FROM vendors "
            "WHERE first_seen_at > ? AND first_seen_at <= ? "
            "ORDER BY first_seen_at, vendor_id",
            (earlier_started_at, later_started_at),
        )
    ]

    stale_vendors = [
        DiffRow(
            vendor_id=r["vendor_id"], vendor_name=r["vendor_name"],
            timestamp=r["last_seen_at"],
        )
        for r in conn.execute(
            "SELECT vendor_id, vendor_name, last_seen_at FROM vendors "
            "WHERE last_seen_at < ? "
            "ORDER BY last_seen_at, vendor_id",
            (later_started_at,),
        )
    ]

    new_products = [
        DiffRow(
            vendor_id=r["vendor_id"], vendor_name=r["vendor_name"],
            product_id=r["product_id"], product_name=r["product_name"],
            software_version=r["software_version"],
            certification_type=r["certification_type"],
            timestamp=r["first_seen_at"],
        )
        for r in conn.execute(
            "SELECT c.vendor_id, c.product_id, c.software_version, "
            "       c.certification_type, c.first_seen_at, "
            "       v.vendor_name, m.product_name "
            "FROM compliance_records c "
            "LEFT JOIN vendors v ON v.vendor_id = c.vendor_id "
            "LEFT JOIN models  m ON m.vendor_id = c.vendor_id "
            "                   AND m.product_id = c.product_id "
            "WHERE c.first_seen_at > ? AND c.first_seen_at <= ? "
            "ORDER BY c.first_seen_at, c.vendor_id, c.product_id, "
            "         c.software_version",
            (earlier_started_at, later_started_at),
        )
    ]

    # Updated rows: last_updated_at advanced into the window AND the row
    # was first seen before the window. Equality first_seen_at == later
    # would mean the row is brand new, not updated — exclude with the
    # second predicate.
    updated_products = [
        DiffRow(
            vendor_id=r["vendor_id"], vendor_name=r["vendor_name"],
            product_id=r["product_id"], product_name=r["product_name"],
            software_version=r["software_version"],
            certification_type=r["certification_type"],
            timestamp=r["last_updated_at"],
        )
        for r in conn.execute(
            "SELECT c.vendor_id, c.product_id, c.software_version, "
            "       c.certification_type, c.last_updated_at, "
            "       v.vendor_name, m.product_name "
            "FROM compliance_records c "
            "LEFT JOIN vendors v ON v.vendor_id = c.vendor_id "
            "LEFT JOIN models  m ON m.vendor_id = c.vendor_id "
            "                   AND m.product_id = c.product_id "
            "WHERE c.last_updated_at > ? AND c.last_updated_at <= ? "
            "  AND c.first_seen_at  <= ? "
            "ORDER BY c.last_updated_at, c.vendor_id, c.product_id",
            (earlier_started_at, later_started_at, earlier_started_at),
        )
    ]

    return DiffReport(
        earlier_run_id=earlier_run_id,
        later_run_id=later_run_id,
        earlier_started_at=earlier_started_at,
        later_started_at=later_started_at,
        new_vendors=new_vendors,
        stale_vendors=stale_vendors,
        new_products=new_products,
        updated_products=updated_products,
    )


# ---- rendering --------------------------------------------------------

def _truncate(rows: Sequence[DiffRow], limit: int = MAX_EXAMPLES) -> tuple[list[DiffRow], int]:
    if len(rows) <= limit:
        return list(rows), 0
    return list(rows[:limit]), len(rows) - limit


def _vendor_lines(rows: Sequence[DiffRow]) -> list[str]:
    out = ["| vendor_id | vendor | timestamp |", "|---:|---|---|"]
    for r in rows:
        out.append(f"| {r.vendor_id} | {r.vendor_name or '_(unknown)_'} | {r.timestamp} |")
    return out


def _product_lines(rows: Sequence[DiffRow]) -> list[str]:
    out = ["| vendor | product | sw_version | cert_type | timestamp |",
           "|---|---|---:|---|---|"]
    for r in rows:
        out.append(
            f"| {r.vendor_name or r.vendor_id} | "
            f"{r.product_name or f'{r.vendor_id}/{r.product_id}'} | "
            f"{r.software_version} | {r.certification_type} | {r.timestamp} |"
        )
    return out


def render_markdown(report: DiffReport) -> str:
    earlier_label = (
        f"run {report.earlier_run_id}"
        if report.earlier_run_id is not None
        else "(no prior run — first sync)"
    )
    counts = report.counts
    headline = (
        f"+{counts['new_products']} products, "
        f"~{counts['updated_products']} updated, "
        f"+{counts['new_vendors']} vendors, "
        f"~{counts['stale_vendors']} stale"
    )

    lines: list[str] = []
    lines.append(f"# matter-db sync diff")
    lines.append("")
    lines.append(f"Comparing **{earlier_label}** (`{report.earlier_started_at}`) "
                 f"to **run {report.later_run_id}** (`{report.later_started_at}`).")
    lines.append("")
    lines.append(f"**Summary:** {headline}")
    lines.append("")
    lines.append("| change | count |")
    lines.append("|---|---:|")
    lines.append(f"| new products | {counts['new_products']} |")
    lines.append(f"| updated products | {counts['updated_products']} |")
    lines.append(f"| new vendors | {counts['new_vendors']} |")
    lines.append(f"| stale vendors | {counts['stale_vendors']} |")
    lines.append("")

    sections = [
        ("New products", report.new_products, _product_lines),
        ("Updated products", report.updated_products, _product_lines),
        ("New vendors", report.new_vendors, _vendor_lines),
        ("Stale vendors", report.stale_vendors, _vendor_lines),
    ]
    for title, rows, render_fn in sections:
        lines.append(f"## {title} ({len(rows)})")
        lines.append("")
        if not rows:
            lines.append("_None._")
        else:
            shown, more = _truncate(rows)
            lines.extend(render_fn(shown))
            if more:
                lines.append("")
                lines.append(f"_…and {more} more (showing first {MAX_EXAMPLES})._")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_json(report: DiffReport) -> str:
    return json.dumps(asdict(report), indent=2, sort_keys=True) + "\n"


def headline_for_commit(report: DiffReport) -> str:
    """Short headline used in CI commit messages."""
    counts = report.counts
    return (
        f"sync: +{counts['new_products']} products, "
        f"+{counts['new_vendors']} vendors, "
        f"~{counts['updated_products']} updated, "
        f"~{counts['stale_vendors']} stale "
        f"(run {report.later_run_id})"
    )
