# ADR 0006: One Lakeflow job per business date; write-audit-publish through staging schemas

- Status: accepted

## Context

The source runs one Airflow DAG, `aml_daily`, per business date: it waits for the files, opens the
Nessie branch `etl` from `main`, ingests, audits bronze, builds and audits silver, builds and audits
gold, and publishes by merging `etl` into `main`. Readers query `main` only, so they never see a
layer that failed its tests, and the merge publishes every table at once. A lock keeps two runs from
sharing the branch.

In the cloud, ingestion (ADR 0004) and the dbt project (ADR 0005) exist as separate jobs. What is
missing is the order between them, the audits as gates, the publication, failure handling, and a
business date parameter. Two facts of the target shape the design, both checked on the dev
workspace before writing any code:

- Unity Catalog has no branches. The Delta substitutes for a zero-copy branch fail on these tables:
  `SHALLOW CLONE` of a table with column masks is refused
  (`COLUMN_MASKS_TABLE_CLONE_SOURCE_NOT_SUPPORTED`), and so is time travel, hence `RESTORE`
  (`COLUMN_MASKS_FEATURE_NOT_SUPPORTED.TIME_TRAVEL`). Five silver tables carry masks (ADR 0005).
- `CREATE TABLE ... LIKE` copies columns and properties, not masks, tags or comments.

## Decision

**One Lakeflow job, `aml_daily`, in the bundle, with the source DAG's tasks**: `ingest` (runs the
`ingest_bronze` job), `audit_bronze`, `open_staging`, `build_silver`, `audit_silver`, `build_gold`,
`audit_gold`, `publish`. A `business_date` job parameter (`YYYY-MM-DD`, or `all` to rebuild every
date) reaches dbt as a variable and the Python tasks as an argument. The on-premises scheduler,
which stays (ADR 0001), starts the job after it delivered the date; no trigger runs in the cloud.
`max_concurrent_runs: 1` with queueing replaces the source's lock.

**Write-audit-publish through staging schemas.** dbt writes every layer into `<layer>_staging`
(`reference_staging`, `silver_staging`, `gold_staging`), which no group can read; `generate_schema_name`
appends the suffix, so the models are unchanged. Each layer's tests run as a separate task after its
build; dbt selectors split the tests so that each runs in exactly one audit (`make dbt-selectors`
checks it in CI). The bronze audit is a dbt test that reconciles each bronze slice of the date with
the ingestion's log: one delivery, accepted, with the rows its manifest declared.

**Staging kept in step with the published layers, date by date.** Every dated row already carries
the pipeline run that wrote it (`_pipeline_run_id`). `open_staging` compares, per dated table, the
run of each staging slice with the run of the published slice, and copies the published slice over
every one that differs (`INSERT INTO ... REPLACE WHERE business_date IN (...)`). A failed run's
slices are therefore undone before the next build reads history (the rules look back up to seven
days), with no clone and no time travel. In steady state nothing is copied; the first run copied the
history once. Whole tables (histories, snapshots, references) are rebuilt by every run.

**Publication by copy, checked before any write.** For every staging table, `publish` first checks
that its columns equal the published table's, that the published masks include the model's, and that
every row to publish was written by this run; only then does it write, table by table: the date's
slice with `REPLACE WHERE`, or the whole table with `INSERT OVERWRITE`, each one atomic Delta commit
that keeps the published table's masks, tags and grants. A missing published table is created
`LIKE` its staging table with the staging table's masks, tags and comments set before its first row.
A mask the model gained is set before the rows; a mask that differs is an error for review, never
applied automatically. The publication is then recorded in `observability.publication` with each
table's rows and Delta version.

**Failures.** Audits and ingestion are never retried: their failures are deterministic. Building,
staging and publishing are retried once after a minute. Serverless retries a task without a retry
policy on its own and the bundle drops `max_retries: 0` as unset, so the audits set
`disable_auto_optimization`. A failure, or a run longer than 30 minutes, mails the deployer
(variable `alert_recipient`). An empty `business_date` fails the first audit: a repair started
without job parameters runs with their defaults, and must not turn into a rebuild of every date.
`make repair` reruns a failed run from `open_staging` with its own parameters.

## Alternatives considered

| Option | Why not |
|---|---|
| Keep Airflow and call Databricks from it | Two schedulers to run and secure for one pipeline; the bundle versions the job with the code it runs, and the on-premises Airflow keeps only what stays there |
| One dbt task running `dbt build` | Tests run next to each model, so a failing gold test could follow a published silver table; no gate between layers, no repair from a layer |
| Build in place, `RESTORE` on a failed audit | Readers see unaudited rows until the restore; and time travel is refused on masked tables |
| `SHALLOW CLONE` of the published layer at each run (zero-copy branch) | Refused on tables with column masks |
| Masks only on the published tables, unmasked staging that can be cloned | Two security definitions for one table; dbt applies the model's masks wherever it builds |
| Copy the whole published layer into staging at each run | 5,078,345 transfers copied to stage the 11 of 2022-09-18; the slice reset copies only what a failure left |
| Publish by `CREATE OR REPLACE TABLE ... AS SELECT` | Recreates the table: masks, tags and history are lost and set again after the rows exist |
| Lakeflow declarative pipelines with expectations | A rewrite of a tested dbt project; expectations drop or fail rows, not whole dates; one active pipeline per type on Free Edition |
| File arrival trigger on the landing volume | The business date is the unit of work and the on-premises scheduler knows when a date is complete; a trigger per file would start runs for half-delivered dates |

## Consequences

- Readers never see a layer that failed an audit: tested with a duplicated transfer id planted in
  bronze, the run stopped at `audit_silver`, published silver kept its 23 transfers of the date, and
  the next date's run reset the unaudited slice before building.
- Delta has no transaction across tables: during `publish`, a reader can see silver of the new date
  next to gold of the previous publication, for the minute the copies take. Consumers that need a
  consistent cut read the dates listed in `observability.publication`. The source's single merge
  did not have this gap.
- Every run copies the whole tables (about 1.2 million rows on dev). Cheap at this size; at a larger
  one, the histories would move to dated slices too.
- A change to a model's columns stops the daily runs (`SchemaChangedError`) until a rebuild
  (`business_date=all`), which recreates the changed tables: readers miss them for seconds. A schema
  change is a release, as on the source, where the merge of a changed table was reviewed.
- A repair from the failed audit only is refused by the publication: the tables built before it
  belong to an earlier attempt. The runbook repairs from `open_staging`.
- Each task starts its own serverless environment: a date takes about eight minutes end to end on
  Free Edition, and in `build_silver` (64 s) dbt's own work is 22 s. Fewer, larger tasks would be
  faster and lose the gate between layers.
