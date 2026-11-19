# ADR 0001: Re-platform the AML lakehouse in waves, with a parallel run as proof

- Status: accepted

## Context

The source is the on-premises AML platform of [AML-Compliance-Platform](https://github.com/hatimelguebbass/AML-Compliance-Platform),
pinned at the commit in `source/pin.toml`: Spark and Iceberg on SeaweedFS with a Nessie catalog,
Trino, dbt on Trino, Airflow, Kafka and Debezium for the core banking change data capture, and a
Power BI report. It produces the alerts of seven transaction monitoring rules, sanctions screening
hits and a customer 360 view.

The target is Azure Databricks. Three constraints shape how to get there:

1. **Same results.** Compliance decisions rest on the alerts. On the day the cloud platform becomes
   the system of record, the alerts of a given business date must be the ones the source produced.
2. **Personal data stays on premises.** Law 09-08 restricts transfers of personal data; names,
   national ID numbers, phones, e-mails and addresses do not leave the bank (ADR 0003).
3. **The source keeps running** until the target has proved itself, and must be able to take over
   again if the cutover fails.

## Decision

- **Re-platform.** Keep the business logic (contracts, dbt models, rule parameters, tests) and move
  it to the target's engine, storage and operations: Delta tables in Unity Catalog, dbt on
  Databricks SQL, Lakeflow Jobs deployed by bundles. SQL is translated from the Trino dialect
  automatically where possible, with a report of what needed manual changes.
- **Measure before moving.** The assessment is generated, not written by hand: `make inventory`
  collects every table, column, volume, dbt model, SQL function and access rule from the running
  source into `migration/inventory.json`; `make assessment` classifies every column and checks the
  waves against it. A source change that adds a column fails CI until the column is classified.
- **Waves** (`migration/waves.yaml`, rendered in `docs/assessment/waves.md`), each one usable on its
  own and checked before the next starts:
  1. payments and cards to silver: the bulk of the volume, and no personal data;
  2. customer reference without direct identifiers: SCD2 histories rebuilt from exported change
     logs, and the customer-to-party assignment computed on premises;
  3. AML rules, alerts and their evidence, plus the sanctions hits computed on premises;
  4. observability checks and reporting.
- Every object has exactly one fate, enforced by `lakemig assess`: **exported** (computed on
  premises, shipped without direct identifiers), **rebuilt** (recomputed in the cloud, only from
  objects of the same or earlier waves, never holding a direct identifier), **kept on premises**,
  or **retired** (replaced by a target mechanism, such as run audit tables by job run history).
- **Proof by parallel run.** Both platforms process the same business dates; a parity report
  compares row counts, null counts, distinct counts, sums and row hashes per key, column by column,
  with tolerances written down in advance. Zero difference is required on alerts (ADR 0008).
- **Reversible cutover.** The source stays fed during the transition; the rollback is executed and
  timed before the migration is declared done (step 9).

## Alternatives considered

| Option | Why not |
|---|---|
| Lift and shift (the same containers on Azure VMs) | Keeps all the operations work the move is meant to remove, at cloud prices |
| Rewrite from scratch on Databricks | Loses the tested logic and any way to show the results did not change; longest and riskiest |
| Big bang (all objects, one cutover) | One failure blocks everything, and a parity gap cannot be traced to a layer |
| Move everything, personal data included | Not lawful without the data protection authority's approval; a bank would not do it |
| Hand-written inventory | Goes stale with the first source change; nothing checks it |

## Consequences

- The source repository is referenced by commit, never copied. Moving the pin reruns the inventory,
  and the diff of `migration/inventory.json` shows what changed.
- Identity resolution and sanctions screening need names, so they stay on premises; the cloud
  receives their results keyed by technical identifiers. The cloud platform cannot re-run them.
- The core banking reconciliation reads the source database and stays on premises with it.
- Every cloud wave depends on exports from the source until the source is decommissioned: the
  export is part of the production pipeline, not a one-off copy.
