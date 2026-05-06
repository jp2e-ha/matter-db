# matter-db

[![sync](https://github.com/jp2e-ha/matter-db/actions/workflows/sync.yml/badge.svg)](https://github.com/jp2e-ha/matter-db/actions/workflows/sync.yml)
[![data freshness](https://img.shields.io/github/last-commit/jp2e-ha/matter-db/main?label=last%20sync)](https://github.com/jp2e-ha/matter-db/commits/main)

A continuously-updated SQLite database of every Matter-certified smart-home
product, mirrored from the CSA **Distributed Compliance Ledger** (DCL).

The DCL is a Cosmos-SDK blockchain run by the Connectivity Standards
Alliance; every Matter device's vendor, product, firmware, and
certification record is published to it. This repo runs `matter-db sync`
once a day against the public observer node at
[`on.dcl.csa-iot.org`](https://on.dcl.csa-iot.org/), commits the resulting
SQLite file straight to `data/matter.db`, and writes a human-readable
diff to [`CHANGES.md`](./CHANGES.md).

## What's in here

```
matter-db/
├── data/matter.db              ← the SQLite mirror, committed by CI
├── CHANGES.md                  ← Markdown diff from the most recent sync
├── changes-latest.json         ← same diff, machine-readable
├── src/matter_db/              ← sync engine
├── tests/                      ← pytest suite (httpx.MockTransport)
├── .github/workflows/sync.yml  ← daily 07:00 UTC sync + commit-back
├── docs/findings.md            ← Session 1 reconnaissance report
└── samples/                    ← raw JSON pulled from each DCL endpoint
```

## How the auto-update works

1. `.github/workflows/sync.yml` fires at **07:00 UTC daily** (or on
   `workflow_dispatch`).
2. The job checks out the repo (so the previous `data/matter.db` is on
   disk), runs `matter-db sync` to merge the latest DCL state into it,
   then runs `matter-db diff --since-last` twice to produce
   `CHANGES.md` and `changes-latest.json`.
3. The job commits `data/matter.db`, `CHANGES.md`, and
   `changes-latest.json` back to `main` with a one-line headline of the
   form `sync: +12 products, +1 vendors, ~3 updated, ~0 stale (run 47)`.
4. A `concurrency: matter-db-sync` group ensures no two sync runs ever
   overlap. If a run fails, an issue is opened automatically with the
   tail of the sync log attached.

A typical sync touches:

- 1 vendor list page (~421 vendors)
- ~9 compliance-info pages (~4,269 records)
- ~3,700 unique model lookups (concurrent, semaphore-bounded)
- ~4,300 unique model-version lookups (same)

With Semaphore(5) and a 200ms per-worker politeness delay it completes
in roughly 8–12 minutes against the public observer node.

## Reading `CHANGES.md`

Four sections, each capped at 20 example rows:

- **New products** — compliance rows whose `first_seen_at` was bumped
  during the most recent sync.
- **Updated products** — rows whose `last_updated_at` advanced (the SHA-256
  of the canonical-JSON record changed).
- **New vendors** — vendors whose Vendor ID showed up for the first time.
- **Stale vendors** — vendors that the most recent sync did *not*
  observe. The DCL list endpoint occasionally hiccups; a single appearance
  in this list is a soft warning, not a deletion signal.

The headline at the top of every commit message has the four counts so
you can scroll the [commit history][commits] without reading the full
diff each day.

[commits]: https://github.com/jp2e-ha/matter-db/commits/main

## Querying the database locally

`data/matter.db` is just a SQLite 3 file. Clone the repo and point
anything that speaks SQLite at it:

```sh
sqlite3 data/matter.db
```

The two views are usually all you need.

**Every certified Matter product, with vendor + firmware joined in:**

```sql
SELECT vendor_name, product_name, software_version_string,
       certification_date, certificate_id, product_url
FROM matter_certified_products
ORDER BY certification_date DESC
LIMIT 20;
```

**Top 10 vendors by certified product count:**

```sql
SELECT vendor_name, COUNT(*) AS products
FROM matter_certified_products
GROUP BY vendor_id
ORDER BY products DESC
LIMIT 10;
```

**Vendors registered on the DCL but with no certified products yet
(useful as a leading indicator of upcoming Matter activity):**

```sql
SELECT vendor_name, company_legal_name, landing_url, first_seen_at
FROM matter_vendor_watchlist
LIMIT 50;
```

**All firmware versions for a specific product (Aqara Hub M2):**

```sql
SELECT software_version_string, certification_date,
       certificate_id, ota_url
FROM matter_certified_products
WHERE vendor_name = 'Aqara' AND part_number = 'AG035'
ORDER BY software_version DESC;
```

## Running the sync yourself

```sh
uv sync
cp .env.example .env          # DCL_BASE_URL=https://on.dcl.csa-iot.org
uv run matter-db sync         # ~10 min against the public observer
uv run matter-db status       # last 5 runs + row counts
uv run matter-db diff --since-last
```

Smaller commands:

```sh
uv run matter-db sync --dry-run                    # walks but rolls back
uv run matter-db diff --since 5 --format json -o /tmp/diff.json
uv run matter-db diff --since-last --format headline   # one-line summary
```

## Tests

```sh
uv run pytest -q
```

The suite uses `httpx.MockTransport` so it never touches the network. It
covers pagination, 404-as-empty, retries, the async semaphore cap,
upsert idempotency, the watchlist and certified-products views, the diff
categorizer, and that the GitHub Actions workflow YAML parses.

## Source

- Spec: [zigbee-alliance/distributed-compliance-ledger](https://github.com/zigbee-alliance/distributed-compliance-ledger) (`docs/static/openapi.yml`).
- Public observer node: [`https://on.dcl.csa-iot.org`](https://on.dcl.csa-iot.org).
- Reconnaissance notes (endpoint shapes, gotchas, status enum): [`docs/findings.md`](./docs/findings.md).
