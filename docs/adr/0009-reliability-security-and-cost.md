# ADR 0009: Reliability checks as an audited layer, operating evidence from the system tables

- Status: accepted

## Context

Steps 3 to 7 moved the data and proved the target computes what the source computes. Before the
cutover (step 9) the platform has to show it can be run: that a bad date is caught before readers
see it, that data moves where the code says it does, that only the expected roles touch it, and
what it costs against the estimate of step 1. Wave 4 of the plan, the source's data observability,
is still to port; it carries most of the first point.

Four facts shape the answers:

- **The source's checks are SQL already.** `volume_check` and `distribution_drift` compare each
  business date with the median and the distribution of earlier dates; the source ran them after
  its gold layer and failed the DAG on a blocking anomaly. Its reconciliation compared its own
  ingestion log with silver; on the target that log is replaced by the delivery manifests and
  `landing.delivery_log` (ADR 0004).
- **Unity Catalog records what ran, not what was meant.** `system.access.table_lineage` lists the
  table-to-table reads and writes of every query; `system.access.audit` lists who read, wrote and
  granted. Neither says whether that matches the design.
- **Two bills.** `system.billing.usage` meters DBUs at list price; Azure Cost Management charges
  the subscription, after the offer's discounts. The estimate of step 1 is in DBUs per meter.
- **The two large tables are read two ways.** `silver.payment_transfer` (5.1 million rows) and
  `silver.card_transaction` (1.8 million) are written one business date at a time and read by date
  in the pipeline, but by account or card across every date in an investigation.

## Decision

### The checks are a fourth layer, built and audited before publication

The three source models are ported as they were (`transform/models/observability/`,
`transform/macros/observability.sql`): same baselines (the last five unacknowledged dates of the
same kind, weekday or weekend), same ratio bounds, same population stability index and drift
threshold, same `acknowledged_anomaly` seed. They build in `observability_staging` after gold
(`build_observability`), and their tests run as an audit (`audit_observability`): a blocking
volume anomaly fails the run and the date is not published, as on the source. A drift or an
anomaly on a non-blocking dataset is a warning, reported and published.

A new model, `layer_reconciliation`, replaces the retired ingestion log: for each dated feed it
compares the records the delivery's manifest declared, the records in bronze and in silver, and the
amount control totals of bronze and silver (cards back in minor units at the currency's exponent).
A feed that does not reconcile fails the audit.

These models recompute from silver and gold, which the parallel run already compares; they are
exempted from parity in their dbt `meta` (`parity_exemption`, with the reason) and listed as such in
the [parity report](../assessment/parity.md). Their acceptance is the source's unit tests, ported
with their inputs and expected rows, and the run of the 18 business dates on dev: no anomaly, every
feed reconciled (36 of 36), the acknowledged outage kept out of the baselines.

The publication log is a dated table in the `observability` layer that no run builds: staging only
copies tables that carry both the business date and the pipeline run (`_built_by_slices`).

### Lineage is checked against dbt's graph

`lakemig operations` derives the edges the code declares from dbt's manifest (each model's parents,
through ephemeral models, then each staging table to its published table) and compares them with the
edges Unity Catalog recorded into the catalog over the window. A declared edge never recorded fails
the command; recorded edges the code does not declare are listed for review.

### Access is reported by role, never by person

The audit events on the catalog are counted per role and kind (read, write, permissions), with the
denials. Identities are mapped to roles: the deployer service principal, Databricks' own system
identities, and every other identity as `operator`. Permission changes are listed with the groups
they changed; an e-mail address in a change is replaced by its role. The report is committed and
holds no personal data.

### Cost is measured, both ways

The same command prices `system.billing.usage` with `system.billing.list_prices`, per product, SKU
and job, reads the subscription's actual cost per Azure service from the Cost Management query API,
and scales the estimate's DBU meters to the window. Report: `migration/operations.json`, rendered to
[operations.md](../assessment/operations.md).

### Liquid clustering on the two large tables

`silver.payment_transfer` is clustered by `business_date, originator_account_id`,
`silver.card_transaction` by `business_date, card_id`. The keys are dbt configuration; `publish`
applies a published table's clustering when its staging table's differs, then reclusters it with
`OPTIMIZE ... FULL`, so the layout ships with the code. `make benchmark` measures what the warehouse
reads for four queries (`migration/benchmark.yaml`) from the query history: files and bytes read
follow the layout only, where durations also follow the warehouse's cache and load. Each run of a
query selects a fresh literal so the result cache cannot answer it, and a measurement the history
reports as cached is refused.

Measured on prod, 18 business dates, before and after ([report](../assessment/clustering.md)):

| Query | Before: files read / MB | After: files read / MB |
|---|---:|---:|
| One originator account, every date | 4 / 27.5 | 2 / 16.2 |
| One beneficiary account, every date (not a key) | 3 / 24.3 | 4 / 37.1 |
| One card, every date | 1 / 10.3 | 1 / 10.3 |
| One business date of payments | 4 / 15.1 | 2 / 13.5 |

The keys halve the files read by date and by originator. At this size the tables are four files
and one: a card's history cannot skip within a single file, and every query runs in about 0.65 s
either way, the warehouse's own overhead. The gain is kept for the volume the layout is meant for:
a year of dates is twenty times as many rows and files, and the reads by date and by account are
the pipeline's and the investigators'. Reads by beneficiary get worse (the rows they need are spread
over every file); a second access path, if investigators need it, is a table of its own, not a third
key.

### CI durations are measured from the runs

`make ci-durations` renders the median and longest duration of each job over the last green runs
from the GitHub Actions API ([ci.md](../assessment/ci.md)).

## Alternatives considered

| Option | Why not |
|---|---|
| Lakehouse Monitoring (data profiling) on the published tables | Profiles a table, not a business date against the dates of the same kind, and has no acknowledged anomaly; it runs after publication, so it cannot hold a date |
| Lakeflow declarative pipeline expectations | Drop or fail rows, not a whole date compared with its history; a rewrite of tested dbt models |
| Keep the source's reconciliation against its ingestion log | The log was retired with the ingestion (ADR 0004); the manifest is the declaration the target receives |
| Accept wave 4 by the parity check | The step-7 comparison state is gone, and these models are a function of tables parity already compared; their own tests say more |
| Read lineage in Catalog Explorer | Shows the graph, does not say an edge is missing; not repeatable in a build |
| Name the people in the access report | Personal data in a public, committed file; the role answers the question |
| Partitioning by business date | Small files at 18 dates of a few MB each, and no help for the account and card reads |
| Z-ORDER | Rewrites every file at each OPTIMIZE and is not incremental; liquid clustering replaces it on Delta |
| Automatic liquid clustering (`CLUSTER BY AUTO`) | Picks keys from the observed workload after the fact; a migration ships its layout with the code and measures it |
| Measure durations only | They change with the warehouse's cache, start and load; bytes and files read do not |

## Consequences

- A date with a blocking volume anomaly or an unreconciled feed is not published; an expected low
  day is acknowledged in `acknowledged_anomaly` by a reviewed commit, as on the source. Bank
  holidays are not modelled.
- The weekend is Saturday and Sunday, in Spark's numbering (`dayofweek` 1 and 7). sqlglot 30.31
  translates Trino's `day_of_week` to an expression that numbers Saturday 1, which would make
  Thursday and Friday the weekend; the [translation report](../assessment/translation.md) shows
  `distribution_drift` as different for that reason, and the port does not use the translation.
- The observability layer adds two tasks to every run: one more serverless start each.
- Lineage, access and cost are read from system tables that Databricks fills with a delay of minutes
  to hours; `make operations` is run after the runs it describes, not inside them.
- The first days of prod cost a fraction of the estimate's low end, in both meters
  ([evidence](../assessment/operations.md)), for three reasons the estimate could not know: the
  initial load runs as one rebuild of every date, not 18 runs; dbt's work is billed to the SQL
  warehouse, and the job's own tasks are short Python steps; the warehouse stops after one idle
  minute, not ten, and no report reads it yet. The estimate stays the budget of the window (ADR
  0007); Power BI (step 9) is the line most likely to move.
- Clustering costs one full `OPTIMIZE` when the keys change, then predictive optimization clusters
  new files as they come. After it, `silver.payment_transfer` takes 285 MB instead of 206 and
  `silver.card_transaction` 79 MB instead of 96: rows grouped by key compress differently from rows
  in delivery order. Storage is the smallest line of the bill.
