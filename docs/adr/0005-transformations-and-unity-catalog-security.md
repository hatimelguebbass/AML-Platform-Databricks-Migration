# ADR 0005: Transformations ported by hand to dbt-databricks, sqlglot as the measure; security in Unity Catalog

- Status: accepted

## Context

The source builds silver and gold with a dbt project on Trino (dbt-core 1.11.17, dbt-trino): change
logs to type 2 histories, payments and cards resolved to the core banking, a rate table, seven AML
rules inlined into two alert tables, and a rule catalogue rendered from the rules' parameters. Its
access control is Trino's file-based rules: `compliance` reads everything, `analyst` reads all but
gold and `restricted`, `bi` all but `restricted`, and only `dbt` and `trino` write.

In the cloud the inputs differ in three ways (ADR 0003): direct identifiers never arrive, account
numbers and card tokens arrive as keyed pseudonyms, and the party resolution and sanctions screening
arrive as finished results, because they need the names. Waves 1 to 3 rebuild ten materialized
models and eight ephemeral ones (`docs/assessment/waves.md`); wave 4, observability, comes with
step 8. The parallel run (step 7) requires zero difference on alerts, so the port must keep the
source's semantics exactly, including rounding and dates.

## Decision

**Port by hand, keep the source's structure, and measure what an automatic translation would have
given.** Each model keeps its name, columns, tests and unit tests; the SQL changes only where
Databricks SQL needs it. Every hand change is listed with its reason and kind (boundary, semantics,
types, syntax, engine) in `migration/translation.yaml`, which CI requires to cover every ported
model. `make translation` renders the pinned source project offline, transpiles each compiled model
with sqlglot (Trino to Databricks), runs it on the warehouse against the same inputs, and compares
its rows with the port's table (`docs/assessment/translation.md`).

**One engine, no `adapter.dispatch`.** The plan foresaw dispatch macros where the SQL differs. The
project runs on one engine: a Trino branch would be code nobody runs or tests. Engine-specific
expressions are instead isolated in named macros (`utc_date`, `lineage_columns`,
`normalize_bank_id`), so a future change of engine is a change of macros, and the source keeps the
Trino version.

**Semantics kept explicitly where the engines differ**, each found by reading both engines' rules
and checked by the measure:

- dates of instants are taken in UTC (`convert_timezone`), never with a cast that follows the
  session time zone;
- decimal division and sums are cast back to the source's scale and precision (`decimal(38, 2)`),
  so thresholds compare the same values;
- arrays aggregated in order are sorted explicitly: Databricks SQL has no `ORDER BY` inside
  `array_agg`.

**Dated tables are replaced with Delta `REPLACE WHERE`** (dbt `replace_where`): one atomic commit
per business date instead of the source's delete then append, without partitions (clustering is
measured in step 8). Contracts stay enforced. The project uses dbt-core 1.11.17, the source's
version, with dbt-databricks 1.13.0; CI parses it offline and the `transform` job builds it on the
SQL warehouse (ADR 0002).

**Results resolved on premises are snapshot models.** `silver.party_member` and
`gold.sanctions_hit` select the latest delivered snapshot up to the business date, so a rerun of a
past date sees what that date saw. The source models that compute them do not migrate.

**Security model in Unity Catalog**, mapped from the source's rules:

| Source user | Account group | Reads |
|---|---|---|
| `compliance` | `aml-compliance` | silver, gold, reference; pseudonyms unmasked |
| `analyst` | `aml-analysts` | silver, reference; pseudonyms masked |
| `bi` | `aml-reporting` | silver, gold, reference; pseudonyms masked |
| `dbt`, `trino` | `aml-pipeline` | owns and builds the tables; pseudonyms unmasked |

- Grants are per schema (`USE_SCHEMA`, `SELECT`) in the bundle, plus `USE_CATALOG`; no group reads
  `landing` or `bronze`. Access follows the layer, as on the source, so a new table inherits it.
- Columns holding pseudonyms (`account_number`, `originator_account`, `beneficiary_account`,
  `card_token`) carry the column mask `governance.mask_pseudonym`, null outside `aml-compliance` and
  `aml-pipeline`. A pseudonym is stable and links a person's rows across tables and deliveries;
  everyone else joins on `account_id`, which means nothing outside the bank.
- Every column classified `indirect` carries the tag `classification=indirect`. CI checks both
  against `migration/classification.yaml` and the export policy: a new column fails until its
  YAML declares the tag and mask the policy requires (`docs/assessment/governance.md`).
- The mask function is created by the dbt run itself (`on-run-start`), before any table that uses
  it, and tables are created with their full column definition (dbt-databricks materialization v2),
  so a masked column is never readable unmasked, even between two statements.
- No row filter: the source had no row-level rule; the rows it hid (`restricted`) never leave the
  premises.

The groups are account groups. On Free Edition, which has no account console, `make groups` creates
them through the workspace's account SCIM endpoint; in prod Terraform does (step 6).

## Alternatives considered

| Option | Why not |
|---|---|
| sqlglot output as the port | Measured: of the ten models, sqlglot's translation fails on two (the boundary renamed a column; it hoists the inlined rules' nested CTEs to the top level, where their names collide) and silently drops the ordering of `array_agg`, which matched only by execution order. It translates compiled SQL, not the Jinja the project is maintained in |
| `adapter.dispatch` for both engines | See "One engine": untested code paths, and the source keeps its own project |
| Lakeflow declarative pipelines or materialized views | A rewrite of a tested project rather than a port, which the parity run would have to prove from scratch; Free Edition allows one active pipeline per type |
| Grants in each model's config | Per-table grants drift and a forgotten one is a silent hole; the layer is the access boundary on the source too |
| Workspace-local groups | Unity Catalog grants only to account principals; tried on Free Edition, refused (`PRINCIPAL_DOES_NOT_EXIST`) |
| Masks as policies attached to tags, defined outside the project | The mask on each column is reviewed in the model's YAML and checked by CI; a policy defined elsewhere would not be |
| Row filter on politically exposed persons | Would change what analysts see compared with the source: a business decision, not a migration one |

## Consequences

- The whole history (18 dates) builds and passes its 78 data tests and 4 unit tests in about a
  minute on the 2X-Small warehouse; `gold.aml_alert` holds 19,395 alerts, as on the source.
- A group membership change takes several minutes to reach `is_account_group_member`; a pipeline
  identity added to `aml-pipeline` just before a build reads masked values, and the tests that
  resolve accounts fail rather than publish nulls. `make translation` refuses to run as an identity
  that reads masked values.
- The measure needs the source rendered, not running: dbt compiles the pinned source offline with
  an in-process DuckDB adapter, against the target catalog's names.
- Wave 4 (observability) is ported in step 8 under the same rules; `translation.yaml` and the
  governance check extend to it automatically.

## Update (ADR 0006)

The `transform` job is replaced by the daily job `aml_daily`: dbt builds each layer in its staging
schema (`generate_schema_name` appends `_staging`), audits run as separate tasks, and the
publication copies the audited rows into the schemas this ADR grants. The grants, masks and tags
above apply to the published tables; staging schemas have no grants.
