"""Sanity-check the GitHub Actions workflow YAML parses and has the
shape we expect — schedule + dispatch triggers, the matter-db sync step,
and the commit-back step."""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "sync.yml"


def test_workflow_parses():
    assert WORKFLOW.exists(), f"workflow not found at {WORKFLOW}"
    parsed = yaml.safe_load(WORKFLOW.read_text())
    assert isinstance(parsed, dict)


def test_workflow_has_schedule_and_dispatch_triggers():
    parsed = yaml.safe_load(WORKFLOW.read_text())
    # YAML's `on:` becomes the boolean True key when read by safe_load
    # (because `on` is a YAML reserved word in 1.1). Look up under either key.
    on = parsed.get("on") or parsed.get(True)
    assert on is not None, "workflow must declare 'on:' triggers"
    assert "workflow_dispatch" in on
    assert "schedule" in on
    assert any("0 7 * * *" in s.get("cron", "") for s in on["schedule"])


def test_workflow_has_concurrency_group():
    parsed = yaml.safe_load(WORKFLOW.read_text())
    conc = parsed.get("concurrency")
    assert conc, "concurrency group is required to prevent overlapping syncs"
    assert "group" in conc


def test_workflow_writes_changes_md_and_commits():
    parsed = yaml.safe_load(WORKFLOW.read_text())
    steps = parsed["jobs"]["sync"]["steps"]
    run_blobs = "\n".join(s.get("run", "") for s in steps if "run" in s)
    assert "matter-db" in run_blobs or "matter_db.cli" in run_blobs
    assert "CHANGES.md" in run_blobs
    assert "changes-latest.json" in run_blobs
    assert "git commit" in run_blobs
    assert "git push" in run_blobs


def test_workflow_grants_required_permissions():
    parsed = yaml.safe_load(WORKFLOW.read_text())
    perms = parsed.get("permissions") or {}
    # commit-back needs contents:write; failure handler creates an issue
    assert perms.get("contents") == "write"
    assert perms.get("issues") == "write"
