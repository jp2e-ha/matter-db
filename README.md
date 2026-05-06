# matter-db

Reconnaissance scaffold for an automatically-updating database of every
Matter-certified smart home product. Data source: the Connectivity Standards
Alliance Distributed Compliance Ledger (DCL).

This repo currently contains only a probe script and a written findings
report. There is no schema, ORM, or sync logic yet — that comes after
recon.

## Layout

```
matter-db/
├── pyproject.toml
├── .env.example         # DCL_BASE_URL=https://on.dcl.csa-iot.org
├── probe/probe.py       # reconnaissance script
├── samples/             # JSON dumps from each endpoint
└── docs/findings.md     # written report
```

## Run

```
cp .env.example .env
uv sync
uv run python probe/probe.py
```

Samples land in `samples/`, the report is in `docs/findings.md`.
