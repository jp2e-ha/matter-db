"""Full-walk sync orchestrator.

Walk order, driven by Session-1 findings:

  1. vendors                    — paginated list
  2. compliance_records         — paginated list (the source of truth for
                                  certified products)
  3. unique (vid, pid) from #2  — fetch each Model; 404 ⇒ no model exists
  4. unique (vid, pid, sv) from #2 — fetch each ModelVersion; 404 ⇒ skip

A single sync_runs row is opened with status='running', and on successful
completion the counts and status='completed' are written. On failure the
exception message is captured and status='failed'.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Iterable

from .client import DCLClient
from .upsert import (
    now_iso,
    upsert_compliance,
    upsert_model,
    upsert_model_version,
    upsert_vendor,
)

log = logging.getLogger(__name__)


@dataclass
class SyncCounts:
    vendors_seen: int = 0
    models_seen: int = 0
    versions_seen: int = 0
    compliance_seen: int = 0
    models_404: list[tuple[int, int]] = field(default_factory=list)
    versions_404: list[tuple[int, int, int]] = field(default_factory=list)


@dataclass
class SyncReport:
    run_id: int
    counts: SyncCounts
    status: str
    error: str | None = None


def open_sync_run(conn: sqlite3.Connection, *, now: str) -> int:
    cur = conn.execute(
        "INSERT INTO sync_runs(started_at, status) VALUES (?, 'running')",
        (now,),
    )
    rid = cur.lastrowid
    assert rid is not None
    return rid


def finalize_sync_run(
    conn: sqlite3.Connection,
    run_id: int,
    counts: SyncCounts,
    *,
    status: str,
    error: str | None = None,
    now: str,
) -> None:
    notes_parts: list[str] = []
    if counts.models_404:
        notes_parts.append(
            f"models 404 ({len(counts.models_404)}): " +
            ", ".join(f"{v}/{p}" for v, p in counts.models_404[:20]) +
            ("…" if len(counts.models_404) > 20 else "")
        )
    if counts.versions_404:
        notes_parts.append(
            f"versions 404 ({len(counts.versions_404)}): " +
            ", ".join(f"{v}/{p}/{s}" for v, p, s in counts.versions_404[:20]) +
            ("…" if len(counts.versions_404) > 20 else "")
        )
    notes = " | ".join(notes_parts) or None
    conn.execute(
        """UPDATE sync_runs SET
              finished_at     = ?,
              status          = ?,
              error_message   = ?,
              vendors_seen    = ?,
              models_seen     = ?,
              versions_seen   = ?,
              compliance_seen = ?,
              notes           = ?
           WHERE id = ?""",
        (now, status, error,
         counts.vendors_seen, counts.models_seen,
         counts.versions_seen, counts.compliance_seen,
         notes, run_id),
    )


def _checkpoint(conn: sqlite3.Connection, dry_run: bool) -> None:
    """Commit the current chunk in normal mode; in dry-run we hold the
    single big transaction open until the end."""
    if not dry_run:
        conn.commit()


def run_sync(
    conn: sqlite3.Connection,
    client: DCLClient,
    *,
    dry_run: bool = False,
    commit_every: int = 200,
) -> SyncReport:
    """Execute one full-walk sync.

    Normal mode commits per phase (and every `commit_every` rows inside the
    long fan-out walks), so a process kill mid-sync preserves everything
    that finished up to the last checkpoint. Dry-run keeps the entire
    entity walk inside one big transaction so we can ROLLBACK at the end.

    The sync_runs row is opened and committed before the walk starts, and
    finalized in its own commit at the end — independent of the entity
    transaction, so the run is always observable.
    """
    started = now_iso()
    run_id = open_sync_run(conn, now=started)
    conn.commit()

    counts = SyncCounts()
    if dry_run:
        conn.execute("BEGIN")

    try:
        log.info("walking vendors…")
        for v in client.get_vendors():
            upsert_vendor(conn, v, now=started)
            counts.vendors_seen += 1
        log.info("vendors: %d", counts.vendors_seen)
        _checkpoint(conn, dry_run)

        log.info("walking compliance records…")
        compliance_rows: list[dict] = []
        for c in client.get_compliance_records():
            upsert_compliance(conn, c, now=started)
            counts.compliance_seen += 1
            compliance_rows.append(c)
        log.info("compliance: %d", counts.compliance_seen)
        _checkpoint(conn, dry_run)

        # Build unique fan-out targets from compliance rows.
        unique_models: set[tuple[int, int]] = set()
        unique_versions: set[tuple[int, int, int]] = set()
        for c in compliance_rows:
            vid = c.get("vid")
            pid = c.get("pid")
            sv = c.get("softwareVersion")
            if vid is not None and pid is not None:
                unique_models.add((int(vid), int(pid)))
            if vid is not None and pid is not None and sv is not None:
                unique_versions.add((int(vid), int(pid), int(sv)))

        log.info("walking %d unique models…", len(unique_models))
        since_commit = 0
        for vid, pid in sorted(unique_models):
            m = client.get_model(vid, pid)
            if m is None:
                counts.models_404.append((vid, pid))
            else:
                upsert_model(conn, m, now=started)
                counts.models_seen += 1
            since_commit += 1
            if since_commit >= commit_every:
                _checkpoint(conn, dry_run)
                since_commit = 0
        _checkpoint(conn, dry_run)

        log.info("walking %d unique model versions…", len(unique_versions))
        since_commit = 0
        for vid, pid, sv in sorted(unique_versions):
            mv = client.get_model_version(vid, pid, sv)
            if mv is None:
                counts.versions_404.append((vid, pid, sv))
            else:
                upsert_model_version(conn, mv, now=started)
                counts.versions_seen += 1
            since_commit += 1
            if since_commit >= commit_every:
                _checkpoint(conn, dry_run)
                since_commit = 0
        _checkpoint(conn, dry_run)

        if dry_run:
            conn.execute("ROLLBACK")
            log.info("dry-run: rolled back entity upserts")

    except Exception as exc:
        if dry_run:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
        else:
            # In normal mode there's no open transaction at this point —
            # checkpoints already committed, current upsert auto-rolled
            # back when the cursor is dropped.
            pass
        finalize_sync_run(
            conn, run_id, counts,
            status="failed", error=str(exc), now=now_iso(),
        )
        conn.commit()
        return SyncReport(run_id=run_id, counts=counts, status="failed",
                          error=str(exc))

    finalize_sync_run(
        conn, run_id, counts,
        status="completed (dry-run)" if dry_run else "completed",
        now=now_iso(),
    )
    conn.commit()
    return SyncReport(
        run_id=run_id, counts=counts,
        status="completed (dry-run)" if dry_run else "completed",
    )
