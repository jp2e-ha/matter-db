# DCL reconnaissance — findings

Probe run: 2026-05-06 against `https://on.dcl.csa-iot.org`, the CSA-operated
public observer node. Spec source: the canonical Swagger 2.0 document at
[`zigbee-alliance/distributed-compliance-ledger/docs/static/openapi.yml`](https://github.com/zigbee-alliance/distributed-compliance-ledger/blob/master/docs/static/openapi.yml).
Raw responses live in [`../samples/`](../samples/).

## Endpoint families on the DCL

The ledger has six top-level groups under `/dcl/`. Only three matter for a
"every certified product" catalog; the rest are governance/PKI plumbing.

**Product-catalog (what we care about):**

| Endpoint | Returns |
| --- | --- |
| `GET /dcl/vendorinfo/vendors` | List of all vendors (paginated). One record per Vendor ID. |
| `GET /dcl/vendorinfo/vendors/{vendorID}` | Single vendor record. |
| `GET /dcl/model/models` | Full Model records, paginated — every (vid, pid) on the ledger with all metadata. |
| `GET /dcl/model/models/{vid}` | Compact `vendorProducts` list for one vendor: `[{pid, name, partNumber}]`. |
| `GET /dcl/model/models/{vid}/{pid}` | Full Model record for a single product. |
| `GET /dcl/model/versions/{vid}/{pid}` | List of softwareVersion integers published for a product. No metadata, just the list. |
| `GET /dcl/model/versions/{vid}/{pid}/{softwareVersion}` | Full ModelVersion record (firmware URL, OTA size, etc.). |
| `GET /dcl/compliance/compliance-info` | Full ComplianceInfo records, paginated — the canonical "is this certified" table. |
| `GET /dcl/compliance/compliance-info/{vid}/{pid}/{softwareVersion}/{certificationType}` | Single compliance record. |
| `GET /dcl/compliance/certified-models` | Filtered view of compliance-info — only entries with status = certified. Records are slim (`vid, pid, softwareVersion, certificationType, value=true`). |
| `GET /dcl/compliance/revoked-models` | Same shape, status = revoked. Empty in current ledger. |
| `GET /dcl/compliance/provisional-models` | Same shape, status = provisional. Empty in current ledger. |
| `GET /dcl/compliance/device-software-compliance` | DCL Compliance Document (CD) records, keyed by `cDCertificateId`. Tangential — links a CD blob to a (vid, pid, softwareVersion). |

**Skipped for product catalog:**

- `/dcl/auth/...` — Cosmos accounts / governance roles. Not product data.
- `/dcl/dclupgrade/...` — chain upgrade proposals.
- `/dcl/validator/...` — chain validator set.
- `/dcl/pki/...` — Matter Device Attestation PKI (PAA / PAI / NOC certs). Useful later for verifying device attestation but not for "is product X certified".

## Concrete answers

### 1. What endpoints does an "every certified product" sync need?

The minimum sync set is three list endpoints, all paginated via the Cosmos
SDK pagination contract (`pagination.key` for cursor, `pagination.total`
for an upfront row count):

1. `GET /dcl/vendorinfo/vendors` — vendor table.
2. `GET /dcl/model/models` — full Model rows for every product.
3. `GET /dcl/compliance/compliance-info` — certification records keyed by `(vid, pid, softwareVersion, certificationType)`.

Optionally, also walk:

4. `GET /dcl/model/versions/{vid}/{pid}` per product to enumerate
   `softwareVersions`, then `GET /dcl/model/versions/{vid}/{pid}/{softwareVersion}`
   per version for OTA / firmware metadata. There's no list endpoint that
   returns ModelVersion rows directly — they're only reachable through the
   per-(vid,pid,softwareVersion) lookup. This is the one part of the sync
   that is N×M instead of a clean "walk a list".

`certified-models` / `revoked-models` / `provisional-models` are
**derivable** from `compliance-info` (its `softwareVersionCertificationStatus`
field) — fetching them is redundant for our use case.

### 2. Natural primary key for a product

Two answers, depending on what "product" means:

- **Product (the model itself):** `(vendorID, productID)` — also written
  `(vid, pid)`. Confirmed: every Model, vendorProducts entry, ModelVersion
  list, and ComplianceInfo row in our samples uses this pair as its
  identifier. ([`models_vid-4447.json`](../samples/models_vid-4447.json),
  [`model_full_vid-4447_pid-2050.json`](../samples/model_full_vid-4447_pid-2050.json))
- **Certified product release (a shipped firmware):** `(vid, pid, softwareVersion, certificationType)` — this is the compliance-info key
  and the only thing that is actually "certified". ([`compliance_vid-4447_pid-2050_sv-400_matter.json`](../samples/compliance_vid-4447_pid-2050_sv-400_matter.json))

Practically: a row in the catalog should be the certified release, with
`(vid, pid)` foreign-keying back to the Model and `vid` foreign-keying to
the Vendor.

### 3. Model vs ModelVersion vs ComplianceInfo

Three separate ledger objects, with three different shapes and lifetimes:

- **Model** — the marketing product. Fields: `productName`, `productLabel`,
  `partNumber`, `productUrl`, `userManualUrl`, `supportUrl`, `deviceTypeId`,
  commissioning instructions, etc. Created once per (vid, pid). No
  certification status, no firmware version.
- **ModelVersion** — a specific firmware release for a Model. Keyed by
  `(vid, pid, softwareVersion)`. Fields: `softwareVersionString`,
  `firmwareInformation`, `otaUrl`, `otaFileSize`, `otaChecksum`,
  `releaseNotesUrl`, `cdVersionNumber`, min/max applicable software
  versions, `specificationVersion`. Still no certification status — this is
  metadata about the firmware artifact.
- **ComplianceInfo** — *this is the one that carries certification.*
  Keyed by `(vid, pid, softwareVersion, certificationType)`. Fields:
  `softwareVersionCertificationStatus`, `cDCertificateId` (e.g.
  `CSA22083MAT40083-24` — the CSA-issued cert ID), `date`, optional
  `reason`, plus a `history[]` of prior status transitions. The status enum
  values aren't documented in the spec; **status `2` is what every
  successfully certified row in our sample has**, so `2 = certified` is the
  working assumption. Provisional and revoked are exposed via the
  filter-view endpoints, both of which are currently empty.

So: **ComplianceInfo is the source of truth for "certified".** A product's
Model row can exist without any compliance-info, in which case the device
isn't (yet) certified — only published.

### 4. Stable / useful fields vs noisy ones

**Stable, populated, useful** (sampling Aqara hub + Google Nest + global page 1):

- Vendor: `vendorID`, `vendorName`, `companyLegalName` — all 421/421 populated.
- Model: `vid`, `pid`, `productName`, `partNumber`, `deviceTypeId`,
  `productUrl`. In Aqara's 104 products, name and partNumber are 100%
  populated.
- ModelVersion: `softwareVersion`, `softwareVersionString`, `cdVersionNumber`.
- ComplianceInfo: `softwareVersionCertificationStatus`, `date` (ISO 8601),
  `cDCertificateId`, `certificationType`. These are the load-bearing fields
  for "is this certified, when, and what's the cert number".

**Inconsistently populated:**

- Vendor `companyPreferredName`: 176/421 (42%). `vendorLandingPageURL`: 211/421 (50%). Don't rely on either.
- Model URL fields (`userManualUrl`, `supportUrl`, `lsfUrl`,
  `commissioningCustomFlowUrl`): often duplicates of `productUrl` (Aqara
  Hub has all four pointing at `aqara.com/en/product/hub-m2`). Marketing
  copy, not structured data.
- Model commissioning instruction strings: free-form prose, sometimes
  multi-step English with embedded numbering. Useful as text for display,
  not for filtering.
- ComplianceInfo's many optional metadata strings (`OSVersion`,
  `compliantPlatformUsed`, `transport`, `familyId`, `supportedClusters`,
  `parentChild`, `programType`, `certificationRoute`): empty across every
  row we sampled. They appear to be optional fields populated only for
  niche certification flows.
- ComplianceInfo's `history[]`: empty in every sample. Probably populated
  only on revocation or re-certification.

**Surprising holes:**

- Vendor record has no industry / category. Vendors are just an ID + names + URL.
- Model has `deviceTypeId` (a Matter cluster device type ID, e.g. `14` =
  Aggregator) but no human-readable category — we'd have to maintain our
  own mapping.
- ModelVersion has firmware info but no separate "release date". The
  `date` only lives on ComplianceInfo.

### 5. Sizing

- **Vendors:** **421 total** today (`pagination.total = 421` on a single
  unfiltered list call — entire vendor table fits in one ~125 KB JSON page).
- **Compliance records:** **4,269 total** (`pagination.total = 4269` on
  `/dcl/compliance/compliance-info?pagination.count_total=true`). Same
  number on `certified-models`, which makes sense — current ledger has
  zero revoked and zero provisional.
- **Models per vendor:** wildly skewed. From the three vendors sampled:
  Aqara has **104** products, Google has **2**, and Apple Home has **0**
  (the vendor record exists but `/dcl/model/models/4937` returns 404 — see
  the gotcha below). The long tail is real: "every certified product"
  realistically means enumerating ~4k compliance rows across a handful of
  high-volume vendors plus hundreds of vendors with one or two devices.

### 6. Surprises and gotchas

- **`/dcl/model/models/{vid}` 404s when a vendor has no published Models.**
  Apple Home (vid=4937) is a registered vendor but has no Model entries on
  the ledger, and the endpoint returns 404 rather than an empty list. Sync
  code must treat 404 here as "zero products", not as an error. Apple
  presumably stays off DCL because their Matter strategy is
  controller-side, not device-side. ([`models_vid-4937.json`](../samples/models_vid-4937.json))
- **`softwareVersionCertificationStatus` is an integer enum with no values
  documented in the spec.** Every certified row in our sample has `2`. We
  should treat this as opaque until we see a non-2 value in the wild, and
  resist coding `2 = certified` into any field name.
- **`certificationType` is a free-form path segment.** The spec just types
  it as `string`. Every row we found uses `"matter"`; older rows in the
  ledger likely use `"zigbee"` (the DCL was originally a Zigbee 3.0 cert
  ledger). Sync code should not hardcode `matter` — fetch via the list
  endpoint and let the server give us the value.
- **Cosmos pagination cursors are opaque base64 bytes.** They are
  position-dependent, not stable across writes — we cannot persist a cursor
  and resume a sync hours later. Fine for a full-walk sync, problematic
  for incremental.
- **No "updated since" / changefeed endpoint exists.** The DCL is a
  blockchain, so the underlying CometBFT RPC at port 26657 has block-level
  events, but the REST gateway doesn't expose them. Incremental sync will
  either need to (a) walk the full lists each time and diff, or (b) tap
  CometBFT WebSocket events. Out of scope for this session, but worth
  flagging now.
- **`deviceTypeId` is a Matter device type code, not a category string.**
  The mapping (e.g. `0x000E` = Aggregator, `0x010A` = OnOffLight) lives in
  the Matter Device Library spec, not in DCL. Any human-readable category
  in our DB will need to ship its own lookup table.
- **`pid` is only unique within a vendor.** Reusing `pid=1` across two
  different vendors (e.g., vid=24582/pid=1 = Google Nest Thermostat) is
  expected — never key on `pid` alone.
- **Two sample vendors had compliance-info-by-key 404s** (vid=24582/pid=1,
  pid=8194 sv=1011, etc.). Translation: not every Model + ModelVersion
  combination has a corresponding ComplianceInfo row. ModelVersion
  presence ≠ certification — confirmation that ComplianceInfo is the
  authoritative join.

## Endpoint set, condensed

For the eventual sync, in priority order:

```
1. /dcl/vendorinfo/vendors           (paginated, ~1 page)
2. /dcl/model/models                 (paginated, ~4k rows worth — needs pagination)
3. /dcl/compliance/compliance-info   (paginated, 4,269 rows today)
4. /dcl/model/versions/{vid}/{pid}   (per Model, for OTA / firmware metadata — optional v1)
5. /dcl/model/versions/{vid}/{pid}/{softwareVersion}  (per version, optional v1)
```

Everything else (`certified-models`, `revoked-models`, `provisional-models`,
`device-software-compliance`) is either a derived view of #3 or
out of scope for a product catalog.
