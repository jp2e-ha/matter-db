"""Async full-walk sync orchestrator.

Walk order, driven by Session-1 findings:

  1. vendors                    — paginated list (sequential pages)
  2. compliance_records         — paginated list (sequential pages)
  3. unique (vid, pid) from #2  — fetch each Model concurrently
                                  (Semaphore(5) inside the client);
                                  404 ⇒ no model exists.
  4. unique (vid, pid, sv) from #2 — fetch each ModelVersion concurrently;
                                     404 ⇒ skip.

The HTTP fan-out is driven by `asyncio.gather`, but every SQLite write
runs from the main coroutine after results arrive — sqlite3 connections
are not safe to share across coroutines, so we never let two coroutines
touch the connection.

Normal mode commits per phase, and inside the long fan-outs commits
every `commit_every` (default 200) rows so a process kill mid-sync
preserves everything that finished up to the last checkpoint. Dry-run
keeps a single transaction open and ROLLBACKs at the end.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable

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
    if not dry_run:
        conn.commit()


async def _fanout(
    targets: list[tuple[Any, ...]],
    fetcher: Callable[..., Awaitable[Any]],
    *,
    on_result: Callable[[tuple[Any, ...], Any], None],
    batch_size: int,
    on_batch_done: Callable[[], None],
) -> None:
    """Fan out `fetcher(*target)` over `targets`, batched.

    Within each batch all calls run concurrently — the client's semaphore
    keeps in-flight count bounded. After all calls in a batch finish,
    `on_result` is invoked sequentially for each (target, result) pair so
    SQLite writes are single-coroutine, and then `on_batch_done` lets the
    caller commit.
    """
    for i in range(0, len(targets), batch_size):
        chunk = targets[i:i + batch_size]
        tasks = [asyncio.create_task(fetcher(*t)) for t in chunk]
        results = await asyncio.gather(*tasks)
        for target, result in zip(chunk, results):
            on_result(target, result)
        on_batch_done()


async def run_sync(
    conn: sqlite3.Connection,
    client: DCLClient,
    *,
    dry_run: bool = False,
    commit_every: int = 200,
) -> SyncReport:
    started = now_iso()
    run_id = open_sync_run(conn, now=started)
    conn.commit()

    counts = SyncCounts()
    if dry_run:
        conn.execute("BEGIN")

    try:
        log.info("walking vendors…")
        vendors = await client.get_vendors()
        for v in vendors:
            upsert_vendor(conn, v, now=started)
            counts.vendors_seen += 1
        log.info("vendors: %d", counts.vendors_seen)
        _checkpoint(conn, dry_run)

        log.info("walking compliance records…")
        compliance_rows = await client.get_compliance_records()
        for c in compliance_rows:
            upsert_compliance(conn, c, now=started)
            counts.compliance_seen += 1
        log.info("compliance: %d", counts.compliance_seen)
        _checkpoint(conn, dry_run)

        # Build unique fan-out targets from compliance rows.
        unique_models_set: set[tuple[int, int]] = set()
        unique_versions_set: set[tuple[int, int, int]] = set()
        for c in compliance_rows:
            vid = c.get("vid")
            pid = c.get("pid")
            sv = c.get("softwareVersion")
            if vid is not None and pid is not None:
                unique_models_set.add((int(vid), int(pid)))
            if vid is not None and pid is not None and sv is not None:
                unique_versions_set.add((int(vid), int(pid), int(sv)))
        unique_models = sorted(unique_models_set)
        unique_versions = sorted(unique_versions_set)

        log.info("walking %d unique models (concurrent fan-out)…",
                 len(unique_models))

        def on_model(target: tuple[Any, ...], result: Any) -> None:
            vid, pid = target
            if result is None:
                counts.models_404.append((vid, pid))
            else:
                upsert_model(conn, result, now=started)
                counts.models_seen += 1

        await _fanout(
            unique_models,
            client.get_model,
            on_result=on_model,
            batch_size=commit_every,
            on_batch_done=lambda: _checkpoint(conn, dry_run),
        )

        log.info("walking %d unique model versions (concurrent fan-out)…",
                 len(unique_versions))

        def on_version(target: tuple[Any, ...], result: Any) -> None:
            vid, pid, sv = target
            if result is None:
                counts.versions_404.append((vid, pid, sv))
            else:
                upsert_model_version(conn, result, now=started)
                counts.versions_seen += 1

        await _fanout(
            unique_versions,
            client.get_model_version,
            on_result=on_version,
            batch_size=commit_every,
            on_batch_done=lambda: _checkpoint(conn, dry_run),
        )

        if dry_run:
            conn.execute("ROLLBACK")
            log.info("dry-run: rolled back entity upserts")

    except Exception as exc:
        if dry_run:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
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
