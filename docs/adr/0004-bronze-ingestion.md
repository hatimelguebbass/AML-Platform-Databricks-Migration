# ADR 0004: Bronze ingestion: a job that accepts whole deliveries, not a file stream

- Status: accepted

## Context

ADR 0003 delivers each business date to the `landing.deliveries` volume as a set of Parquet files,
one per table, sealed by a manifest written last. The manifest carries what the cloud must check
before trusting the data: SHA-256 and size of each file, the declared schema, the pseudonymization
key id and control totals per column (nulls, sums, ranges, distinct counts). A date can be delivered
again: the same content gives the same delivery id, a correction gives a new id next to the old one.

Bronze must hold exactly what the source exported, never a partial date, never a date twice, and
must refuse anything the manifest does not vouch for. It is the first layer the cloud transformations
(step 4) and the parity checks (step 7) read.

Databricks offers three ways to load files into Delta: Auto Loader (`cloudFiles`, a Structured
Streaming source that discovers new files), `COPY INTO` (SQL, idempotent per file) and declarative
pipelines (Lakeflow Spark Declarative Pipelines, formerly Delta Live Tables) built on either.

## Decision

**A Python job, `ingest_bronze`, decides delivery by delivery.** It lists sealed deliveries in the
volume, picks per business date the most recent one by `exported_at`, and for that delivery:

1. checks, before reading any row, that the manifest lies at its own `<date>/<delivery_id>`, that
   the id derives from the content digest, that the gate reported no finding, that the key id is
   the one of the deliveries already accepted, that tables and columns match the landing contract
   exactly, and that every file is present with its size and SHA-256;
2. reads each table with Spark using the contract's schema and recomputes the manifest's control
   totals in one aggregation per table;
3. only if everything agrees, replaces the table's slice of the date (`replaceWhere
   _delivery_date = D`) in every bronze table, and logs the delivery `accepted`.

Any failed check logs the delivery `rejected` with every reason; the job goes on with the other
dates and fails at the end. Older deliveries of a date are logged `superseded`. The log,
`landing.delivery_log`, is append-only; the latest row of a delivery is its status. A decided
delivery is not looked at again unless an operator passes its id in the job's `retry` parameter.

**The landing contract** (`src/lakemig/ingestion/landing_contract.json`) is frozen from real
deliveries by `make contract` and reviewed like code. CI checks that it carries exactly the columns
the export policy lets cross. Bronze tables are created from it, with `NOT NULL` where the export
declares a column non-nullable, and the job stops if a table no longer matches it.

**No rescued-data column.** A column that is not in the contract rejects the delivery. Every column
crossing the boundary was classified and allowed by the export policy (ADR 0003); a column the
contract does not know was not, so it may be personal data and must not land anywhere in the
cloud, not even as a JSON blob in `_rescued_data`. A new column is a reviewed change on both sides:
classification, policy, contract.

**Lineage columns** on every bronze row: `_delivery_id`, `_delivery_date`, `_delivery_file` (from
the file metadata column) and `_bronze_loaded_at`. The source's own lineage (`_business_date`,
`_ingested_at`, Kafka coordinates) stays as exported.

**The volume is the quarantine.** Landed files are never moved or modified: a rejected delivery
stays where it landed, and its log row says why. A corrected export arrives under a new id.

## Alternatives considered

| Option | Why not |
|---|---|
| Auto Loader | Built for an open-ended stream of independent files: its unit is the file, ours is a delivery of eight files sealed by a manifest. It would pick up Parquet files before their manifest lands, and has no place to reconcile totals across a whole delivery before committing. Its schema inference and evolution (`addNewColumns`, `rescue`) is the behaviour the contract forbids. Its checkpoint would be a second source of truth beside the log |
| `COPY INTO` | Idempotent per file path, so a corrected delivery (new path) would add rows instead of replacing the date, and a retry after a partial failure cannot replace a slice. No hook to verify checksums or totals before the load |
| Declarative pipeline with expectations | Expectations judge rows, not deliveries: they cannot express "this file's SHA-256 matches" or "the sum of this column equals the trailer". Free Edition allows one active pipeline per type, which step 5 may need. The pipeline owns its tables, which complicates the slice replacement a correction requires |
| Evolve bronze to whatever lands (`mergeSchema`) | See "No rescued-data column": the contract is a privacy control, not only a schema |
| Checks in a dbt test after loading | Bad rows would already be in bronze when the test fails, read by anyone with access |

## Consequences

- A delivery is all or nothing for consumers: the step-5 orchestration reads a date only once its
  log row says `accepted`. A crash between two tables leaves the date's slices partly replaced but
  not logged; the next run replays the whole delivery, and `replaceWhere` makes that idempotent.
- The run reads each file twice (checks, then write), and computes SHA-256 through the volume's
  file system. At about 22 MB per date this costs seconds; at a size where it does not, the totals
  can be computed during the write with a single pass, keeping the same decision.
- Snapshot tables (`party_member`, `sanctions_hit`) keep one full copy per business date in bronze;
  silver takes the latest. History is a feature for the parity checks.
- The key id check means that rotating the pseudonymization key is a planned migration (reload of
  every date under the new key), not something a delivery can do by itself.
- Ingestion runs on serverless compute in both environments; it needs `READ VOLUME` on the landing
  volume and `MODIFY` on the bronze schema, nothing on the storage account.
- Rejections fail the job, so the step-5 alerting covers them without a separate check.
