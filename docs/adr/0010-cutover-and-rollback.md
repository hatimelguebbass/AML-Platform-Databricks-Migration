# ADR 0010: Cutover of the report by a dataset parameter, rollback by switching it back

- Status: accepted

## Context

Step 7 proved the target computes what the source computes; step 8 that it can be run. What is
left is the moment the compliance team stops reading the source: the cutover. Its consumer is one
Power BI report (`bi/` of the source), which imports ten relations through Trino. Four facts shape
how it moves:

- **The report reads through one expression.** Every table of the source's semantic model starts
  from `Lakehouse{[Name = <schema>, Kind = "Schema"]}[Data]{[Name = <table>, Kind = "Table"]}`, and
  the Azure Databricks connector (`Databricks.Catalogs`) navigates a catalog the same way. The
  tables, measures and visuals do not depend on the engine behind that expression.
- **Not everything the report reads moves.** `observability.corebanking_reconciliation` compares
  the lakehouse with the core banking database, which stays on premises (waves.yaml).
  `gold.sanctions_hit` crosses without `listed_name`, a direct identifier (ADR 0003), and the
  report shows it. `observability.pipeline_run` is the source's Airflow run history, retired.
- **The target's pseudonyms depend on a key that lives on premises only.** A rebuilt on-premises
  host without that key cannot deliver again: the ingestion refuses another key (ADR 0004), by
  design, and nothing could rotate one.
- **The source keeps running.** Its daily pipeline is not switched off by the cutover; it is
  decommissioned after it (step 10).

## Decision

1. **The report is rendered, not forked.** `lakemig bi` copies the pinned source's Power BI project
   into `bi/` and changes only what the cutover needs: `Lakehouse` reads `Databricks.Catalogs` or
   Trino depending on a list parameter, `ServingPlatform`; a second expression, `OnPremises`,
   always reads Trino through the on-premises data gateway; retired relations leave the model with
   their relationships and the visuals that read them. `migration/cutover.yaml` places each
   relation that does not move, with its reason. `lakemig assess` (CI) fails if the rendered report
   imports a column the transformations do not publish, of a type it cannot load, or masked: the
   report's identity is in no privileged group, and a masked column would import as empty without
   an error. That check found `sanctions_hit.listed_name`, which placed the sanctions page on
   premises.
2. **Cutover and rollback are a parameter change on the published dataset**, followed by a refresh.
   Nothing is redeployed and the report keeps its identity (`.platform` logical id), its
   permissions and its bookmarks. Both data sources stay configured in the gateway for the
   rollback window.
3. **The source keeps running during a 14-day rollback window.** A rollback inside it needs no
   catch-up; the `rollback` phase still lists any business date the target published that the
   source lacks, and the switch back waits for the source to run it.
4. **Go/no-go is code.** `lakemig cutover` runs each phase as named checks against both platforms
   and appends the timed result to `migration/cutover.json` (rendered as
   `docs/assessment/cutover.md`); a phase passes only if every check passes:
   - `preflight`: key custody, every date's latest delivery accepted, every date published,
     parity accepted at the frozen reference and against the current publications, every report
     column present with a loadable type, the reader group granted, no job run in flight;
   - `freeze`: a Nessie tag at the head of the source's published branch, the reference that the
     final sync exports and the parity compares;
   - `switch`: the source still at the tag, the preflight checks, the report's tables holding as
     many rows on both sides, the on-premises tables readable;
   - `rollback`: every table the report reads readable on the source, the same row counts, no
     business date to catch up.
5. **Key rotation is a reviewed operation of the ingestion** (`--rekey`, `make rekey`): every
   accepted date is exported again under the new key, and the run writes nothing unless all of
   them pass, so bronze is never under two keys. The key is backed up off the export host
   (docs/runbook.md) and `preflight` checks custody before any cutover.

## Consequences

- The timed part of a rollback is what the platform can prove: the source readable, consistent and
  current. The parameter change and refresh in the Power BI service are manual and outside the
  measured time; `docs/cutover.md` lists them with who does them.
- A data source chosen by an `if` needs credentials for both connectors in the service; Power BI
  refreshes it like any parameterized source. The report was rendered and checked as files: it has
  not been opened in Power BI Desktop as part of this repository's CI.
- The sanctions page and the core banking reconciliation keep depending on the source's Trino after
  the cutover. Decommissioning the source (step 10) must keep a query engine for them on premises.
- `observability.pipeline_run` disappears from the report. Run history is in the Lakeflow UI and
  `system.lakeflow`; what was published, when and by which run, is in `observability.publication`.

## Alternatives considered

- **Swap the connector in a new report version** (no parameter). A rollback is then a redeploy of
  the previous version: slower, and it needs the deployment path to work during the incident that
  caused the rollback.
- **A semantic layer in front of both engines** (views or Lakehouse Federation reading Trino from
  Databricks). It would hide the switch from the report but add a component on the critical path
  for one report, and federation would route on-premises data through the cloud.
- **Stop the source at the cutover.** It removes double running costs a few days earlier but turns
  any rollback into a catch-up of every missed date, during the incident.
- **DirectQuery.** The source report imports; the cutover changes where it reads, not how. Moving
  to DirectQuery would change refresh, caching and cost at the same time as the engine.
