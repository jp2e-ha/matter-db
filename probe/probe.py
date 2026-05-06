"""Reconnaissance probe for the CSA Distributed Compliance Ledger.

Pulls a small, representative sample from each read-only endpoint we expect
to need for an "every certified Matter product" catalog and writes the raw
JSON to samples/. No schema design, no DB, no sync loop — recon only.
"""

from __future__ import annotations

import json
import os
import random
import time
from pathlib import Path
from typing import Any, Iterable

import httpx
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
DEFAULT_BASE = "https://on.dcl.csa-iot.org"
FALLBACK_BASES = [
    "https://on.dcl.csa-iot.org",
    "https://on.dcl.dev.dsr-corporation.com",
]

REQUEST_TIMEOUT = 30.0
SLEEP_BETWEEN = 0.2  # 200ms politeness
MAX_RETRIES = 4

console = Console()


def pick_base_url() -> str:
    load_dotenv(ROOT / ".env")
    explicit = os.getenv("DCL_BASE_URL")
    candidates = [explicit] if explicit else []
    candidates += [b for b in FALLBACK_BASES if b != explicit]
    for base in candidates:
        if not base:
            continue
        try:
            r = httpx.get(f"{base}/dcl/vendorinfo/vendors",
                          params={"pagination.limit": "1"},
                          timeout=10.0)
            if r.status_code == 200:
                console.log(f"[green]Observer node OK:[/green] {base}")
                return base
            console.log(f"[yellow]{base} -> HTTP {r.status_code}[/yellow]")
        except httpx.HTTPError as exc:
            console.log(f"[yellow]{base} unreachable: {exc}[/yellow]")
    raise SystemExit("No reachable DCL observer node found.")


class DCL:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(
            base_url=self.base_url,
            timeout=REQUEST_TIMEOUT,
            headers={"accept": "application/json",
                     "user-agent": "matter-db-probe/0.1"},
        )

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                r = self.client.get(path, params=params)
                if r.status_code == 404:
                    return {"_status": 404, "_path": path}
                if 500 <= r.status_code < 600:
                    raise httpx.HTTPStatusError(
                        f"5xx", request=r.request, response=r)
                r.raise_for_status()
                time.sleep(SLEEP_BETWEEN)
                return r.json()
            except (httpx.HTTPStatusError, httpx.TransportError) as exc:
                if attempt == MAX_RETRIES:
                    raise
                backoff = (2 ** (attempt - 1)) + random.random() * 0.3
                console.log(
                    f"[yellow]retry {attempt}/{MAX_RETRIES - 1} {path}: "
                    f"{exc} (sleep {backoff:.1f}s)[/yellow]")
                time.sleep(backoff)
        raise RuntimeError("unreachable")

    def get_paginated(self, path: str, items_key: str,
                      page_size: int = 500,
                      max_pages: int | None = None) -> dict[str, Any]:
        """Walk Cosmos SDK pagination, return a single merged response."""
        merged: dict[str, Any] = {}
        all_items: list[Any] = []
        next_key: str | None = None
        pages = 0
        while True:
            params: dict[str, Any] = {"pagination.limit": str(page_size)}
            if next_key:
                params["pagination.key"] = next_key
            else:
                # first page: also ask for total count
                params["pagination.count_total"] = "true"
            resp = self.get(path, params=params)
            if not merged:
                merged = {k: v for k, v in resp.items() if k != items_key}
            items = resp.get(items_key, []) or []
            all_items.extend(items)
            pagination = resp.get("pagination") or {}
            next_key = pagination.get("next_key") or None
            pages += 1
            console.log(
                f"  {path} page {pages}: +{len(items)} (total so far {len(all_items)})")
            if not next_key:
                break
            if max_pages and pages >= max_pages:
                break
        merged[items_key] = all_items
        merged["_pages_fetched"] = pages
        return merged


def write_sample(name: str, data: Any) -> Path:
    path = SAMPLES / name
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    console.log(f"[cyan]wrote[/cyan] {path.relative_to(ROOT)}  "
                f"({path.stat().st_size:,} bytes)")
    return path


def pick_vendors(vendors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pick a big-name vendor, a mid-size one, and a small one.

    We don't yet know model counts per vendor, so 'big/mid/small' here is by
    well-known brand recognition — Apple, Aqara, then a random small VID at
    the end. The probe will reveal actual size when it pulls each vendor's
    models.
    """
    by_id = {v["vendorID"]: v for v in vendors if "vendorID" in v}

    def find_by_name(needle: str) -> dict[str, Any] | None:
        needle_lc = needle.lower()
        for v in vendors:
            for key in ("vendorName", "companyPreferredName", "companyLegalName"):
                if needle_lc in (v.get(key) or "").lower():
                    return v
        return None

    chosen: list[dict[str, Any]] = []
    for needle in ("Apple", "Aqara", "Google"):
        v = find_by_name(needle)
        if v and v not in chosen:
            chosen.append(v)
        if len(chosen) >= 3:
            break

    if len(chosen) < 3:
        # backfill from any other vendors we haven't picked
        for v in vendors:
            if v not in chosen:
                chosen.append(v)
            if len(chosen) >= 3:
                break

    return chosen[:3]


def pick_models_for_versions(per_vendor_models: dict[int, list[dict[str, Any]]],
                             count: int = 5) -> list[tuple[int, int]]:
    """Pick up to `count` (vid,pid) pairs spread across the sampled vendors."""
    picks: list[tuple[int, int]] = []
    # round-robin across vendors so we don't bias to one
    queues = {vid: list(prods) for vid, prods in per_vendor_models.items()}
    while len(picks) < count and any(queues.values()):
        for vid in list(queues.keys()):
            q = queues[vid]
            if not q:
                continue
            prod = q.pop(0)
            pid = prod.get("pid")
            if pid is not None:
                picks.append((vid, pid))
                if len(picks) >= count:
                    break
    return picks


def summarize(vendors: list[dict[str, Any]],
              per_vendor_models: dict[int, list[dict[str, Any]]],
              version_samples: list[dict[str, Any]],
              compliance_samples: list[dict[str, Any]]) -> None:
    table = Table(title="Probe summary", show_header=True, header_style="bold")
    table.add_column("section")
    table.add_column("count", justify="right")
    table.add_row("vendors", str(len(vendors)))
    for vid, prods in per_vendor_models.items():
        vname = next((v.get("vendorName") or v.get("companyPreferredName")
                      or "?" for v in vendors if v.get("vendorID") == vid), "?")
        table.add_row(f"  vid={vid} ({vname}) products", str(len(prods)))
    table.add_row("model versions sampled", str(len(version_samples)))
    table.add_row("compliance records sampled", str(len(compliance_samples)))
    console.print(table)


def main() -> None:
    base = pick_base_url()
    dcl = DCL(base)

    # 1. all vendors (paginated)
    console.rule("[bold]1. vendors (full, paginated)")
    vendors_resp = dcl.get_paginated(
        "/dcl/vendorinfo/vendors", items_key="vendorInfo", page_size=500)
    vendors = vendors_resp.get("vendorInfo", [])
    write_sample("vendors.json", vendors_resp)

    # 2. models for 3 vendors (big/mid/small heuristic)
    console.rule("[bold]2. models for 3 vendors")
    chosen = pick_vendors(vendors)
    per_vendor_models: dict[int, list[dict[str, Any]]] = {}
    for v in chosen:
        vid = v["vendorID"]
        console.log(f"vendor vid={vid}: {v.get('vendorName') or v.get('companyPreferredName')}")
        resp = dcl.get(f"/dcl/model/models/{vid}")
        write_sample(f"models_vid-{vid}.json", resp)
        # /dcl/model/models/{vid} returns vendorProducts.products (compact list)
        products = (resp.get("vendorProducts") or {}).get("products") or []
        per_vendor_models[vid] = products
        console.log(f"  -> {len(products)} products")

    # also grab one full Model record per chosen vendor's first product, for
    # field-level reference
    console.rule("[bold]2b. one full Model record per chosen vendor")
    for vid, prods in per_vendor_models.items():
        if not prods:
            continue
        pid = prods[0]["pid"]
        full = dcl.get(f"/dcl/model/models/{vid}/{pid}")
        write_sample(f"model_full_vid-{vid}_pid-{pid}.json", full)

    # 3. versions + compliance for 5 models across vendors
    console.rule("[bold]3. versions + compliance for 5 models")
    picks = pick_models_for_versions(per_vendor_models, count=5)
    version_samples: list[dict[str, Any]] = []
    compliance_samples: list[dict[str, Any]] = []
    for vid, pid in picks:
        versions_resp = dcl.get(f"/dcl/model/versions/{vid}/{pid}")
        write_sample(f"versions_vid-{vid}_pid-{pid}.json", versions_resp)
        version_samples.append(versions_resp)

        sw_versions = ((versions_resp.get("modelVersions") or {})
                       .get("softwareVersions") or [])
        if not sw_versions:
            console.log(f"  vid={vid} pid={pid}: no software versions listed")
            continue

        # For each software version, try to find any compliance-info entry.
        # certificationType is a path segment we don't know upfront, but
        # /dcl/compliance/compliance-info (list) is filterable only via
        # pagination, not vid/pid, so we rely on a small probe: try the two
        # known certificationType strings.
        for sv in sw_versions[:2]:  # cap at 2 versions per model for brevity
            found = False
            for ctype in ("matter", "zigbee"):
                resp = dcl.get(
                    f"/dcl/compliance/compliance-info/{vid}/{pid}/{sv}/{ctype}")
                if resp.get("_status") == 404:
                    continue
                write_sample(
                    f"compliance_vid-{vid}_pid-{pid}_sv-{sv}_{ctype}.json", resp)
                compliance_samples.append(resp)
                found = True
                break
            if not found:
                console.log(
                    f"  vid={vid} pid={pid} sv={sv}: no compliance-info under matter/zigbee")

    # 4. one page of the global compliance-info list, for shape reference
    console.rule("[bold]4. compliance-info list (first page only)")
    ci_page = dcl.get("/dcl/compliance/compliance-info",
                      params={"pagination.limit": "20",
                              "pagination.count_total": "true"})
    write_sample("compliance-info_page1.json", ci_page)

    # 5. one page of certified/revoked/provisional, for status flag shape
    for status_path in ("certified-models", "revoked-models", "provisional-models"):
        page = dcl.get(f"/dcl/compliance/{status_path}",
                       params={"pagination.limit": "20",
                               "pagination.count_total": "true"})
        write_sample(f"{status_path.replace('-', '_')}_page1.json", page)

    summarize(vendors, per_vendor_models, version_samples, compliance_samples)


if __name__ == "__main__":
    main()
