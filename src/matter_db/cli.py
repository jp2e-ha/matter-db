"""matter-db CLI: sync, sync --dry-run, status."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from .client import DCLClient, DEFAULT_BASE_URL
from .schema import connect, initialize
from .sync import run_sync

DEFAULT_DB_PATH = "data/matter.db"
console = Console()


def _resolve_db_path(explicit: str | None) -> Path:
    return Path(explicit or os.getenv("MATTER_DB_PATH", DEFAULT_DB_PATH))


def _resolve_base_url(explicit: str | None) -> str:
    return explicit or os.getenv("DCL_BASE_URL", DEFAULT_BASE_URL)


def cmd_sync(args: argparse.Namespace) -> int:
    db_path = _resolve_db_path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    base_url = _resolve_base_url(args.base_url)

    console.log(f"db   = {db_path}")
    console.log(f"dcl  = {base_url}")
    console.log(f"mode = {'dry-run' if args.dry_run else 'sync'}")

    conn = connect(str(db_path))
    initialize(conn)

    with DCLClient(base_url) as client:
        report = run_sync(conn, client, dry_run=args.dry_run)

    counts = report.counts
    console.print(
        f"[bold]{'dry-run' if args.dry_run else 'sync'}[/bold] "
        f"run_id={report.run_id}  status={report.status}\n"
        f"  vendors_seen    = {counts.vendors_seen}\n"
        f"  compliance_seen = {counts.compliance_seen}\n"
        f"  models_seen     = {counts.models_seen}  (404: {len(counts.models_404)})\n"
        f"  versions_seen   = {counts.versions_seen}  (404: {len(counts.versions_404)})"
    )
    if report.error:
        console.print(f"[red]error:[/red] {report.error}")
        return 1
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    db_path = _resolve_db_path(args.db)
    if not db_path.exists():
        console.print(f"[yellow]no database at {db_path}[/yellow]")
        return 1

    conn = connect(str(db_path))

    runs_table = Table(title="last 5 sync_runs", show_header=True,
                       header_style="bold")
    for col in ("id", "started_at", "finished_at", "status",
                "vendors", "compliance", "models", "versions",
                "error", "notes"):
        runs_table.add_column(col)
    rows = conn.execute(
        """SELECT id, started_at, finished_at, status,
                  vendors_seen, compliance_seen, models_seen, versions_seen,
                  error_message, notes
             FROM sync_runs ORDER BY id DESC LIMIT 5"""
    ).fetchall()
    for r in rows:
        runs_table.add_row(
            str(r["id"]),
            r["started_at"] or "",
            r["finished_at"] or "",
            r["status"] or "",
            str(r["vendors_seen"]),
            str(r["compliance_seen"]),
            str(r["models_seen"]),
            str(r["versions_seen"]),
            (r["error_message"] or "")[:60],
            (r["notes"] or "")[:80],
        )
    console.print(runs_table)

    counts_table = Table(title="row counts", show_header=True,
                         header_style="bold")
    counts_table.add_column("table")
    counts_table.add_column("rows", justify="right")
    for tbl in ("vendors", "models", "model_versions",
                "compliance_records", "matter_certified_products"):
        n = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        counts_table.add_row(tbl, str(n))
    console.print(counts_table)
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(prog="matter-db")
    parser.add_argument("--db", help="path to the SQLite database file")
    parser.add_argument("--base-url", help="DCL observer node base URL")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_sync = sub.add_parser("sync", help="run a full-walk sync against the DCL")
    p_sync.add_argument("--dry-run", action="store_true",
                        help="fetch everything and roll back; report what would change")
    p_sync.set_defaults(func=cmd_sync)

    p_status = sub.add_parser("status", help="show last sync runs and row counts")
    p_status.set_defaults(func=cmd_status)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
