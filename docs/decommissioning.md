# Decommissioning the source's analytical layers

What the source platform stops doing once the target is the publisher of record, in which order,
and what each step needs before it starts. The objects and their fates come from
[waves](assessment/waves.md); the figures are those of the pinned commit's inventory.

**Status: planned, not executed.** This repository's Azure window ended within the 14-day rollback
window that follows the cutover (`migration/cutover.yaml`), when prod was destroyed
([ADR 0007](adr/0007-azure-infrastructure-identities-and-cost.md), [teardown](assessment/teardown.md)).
The source is the publisher of record again, and the report returns to it through its rollback setting
(`ServingPlatform = Trino`, [cutover](cutover.md#5-rollback-any-time-within-the-rollback-window)).
Nothing below may start while that is the case.

## What stops, what stays

| Fate (waves.yaml) | Objects | Rows | Data | On the source after decommissioning |
|---|---:|---:|---:|---|
| Rebuilt in the cloud | 24 | 7,965,979 | 224.6 MB | No longer built; frozen read-only, kept for the retention period, then dropped |
| Retired | 6 | 311 | 0.3 MB | Dropped with the Airflow tasks that write them |
| Exported | 8 | 7,820,180 | 177.3 MB | Unchanged: they are what the daily export reads |
| Kept on premises | 11 | 1,032,497 | 7.4 MB | Unchanged: core banking, identities, customer matching, sanctions screening |

Components:

| Component | After decommissioning |
|---|---|
| Airflow `aml_daily` | Ingestion of feeds, change data capture, customer matching, sanctions screening, export and delivery, then the start of the target's `aml_daily`. The tasks `build_silver`, `audit_silver`, `build_gold`, `audit_gold` and `publish` of the rebuilt models are removed |
| dbt on Trino | Only the models of the objects kept or exported: `silver.party_member`, `silver.customer_match`, the sanctions models, `gold.sanctions_hit`, `observability.corebanking_reconciliation` |
| Trino | Stays: it serves the report's `OnPremises` tables (sanctions hits with their listed names, the core banking reconciliation) and the compliance archive |
| Nessie, SeaweedFS | Stay, with the branch `etl` used by the remaining builds only |
| Power BI report | `ServingPlatform` removed: every moved table reads the target, `OnPremises` keeps its two tables |

## Preconditions

All of them, checked and recorded before the first step:

1. **The rollback window has elapsed without a rollback**: the cutover record's last passed event is
   a switch at least `rollback_window_days` old ([cutover record](assessment/cutover.md)).
2. **Shadow parity held every day of the window**: `make parity` on each business date published
   since the freeze, zero difference on the four gold tables, other differences explained.
3. **The target has published every business date of the window by the reporting deadline**
   (`make publications`), with no repair left open.
4. **The pseudonymization key is held twice on premises** and the custody check of the cutover
   passes ([runbook](runbook.md#pseudonymization-key)): after decommissioning, losing it can no longer
   be recovered by switching back.
5. **The compliance function has approved the retention** of the frozen tables: how long, who reads
   them, and how an examiner's request is served from them.

## Steps

Each step is a reviewed change to the source repository and can be undone on its own until step 5.

1. **Stop building.** Remove the build, audit and publish tasks of the rebuilt models from the DAG
   and the models from the dbt selectors; the source's own data tests of the kept models stay. The
   daily export and the start of the target's job are unchanged.
2. **Freeze.** Tag the published branch (`decommissioned-<date>`) and deny writes to the rebuilt
   tables in Trino's access rules. Reads keep working at the tag: an alert of any past date can
   still be explained on premises.
3. **Narrow access.** Readers of the rebuilt tables move to the target's groups (ADR 0005); on the
   source only the compliance archive role keeps `SELECT` on the frozen tables.
4. **Re-render the report** without `ServingPlatform` (`migration/cutover.yaml`, `make bi`), and
   publish it. From here a rollback is a rebuild of the source's layers, not a setting.
5. **Drop the retired objects** (`audit.ingestion_run`, `audit.ingestion_file`,
   `audit.publish_check`, `audit.dbt_node_run`, `observability.layer_reconciliation`,
   `observability.pipeline_run`) and their writers.
6. **At the end of the retention period**, drop the frozen tables and expire their snapshots; the
   storage they held is reclaimed then, not before.

## Rolling back after step 4

The source's code for the removed tasks stays in its history at the decommissioning tag. Restoring
it, then running the source's backfill from the freeze date (`make airflow-backfill` in the source
repository), rebuilds the layers; the report is rendered with `ServingPlatform` again. This takes
hours rather than seconds, which is why steps 1 to 3 wait for the preconditions and step 4 waits for
step 3.
