# Cutover runbook

Moves the compliance report from the source's Trino to the target's SQL warehouse, and back if it
has to. Design and alternatives: [ADR 0010](adr/0010-cutover-and-rollback.md). Every phase below is
recorded, timed, in `migration/cutover.json` and rendered in
[assessment/cutover.md](assessment/cutover.md), which holds the executions of this repository.

## Roles and prerequisites

| Who | Does |
|---|---|
| Migration operator (member of `aml-migration`, on premises next to the source) | Runs every `make` command below |
| Report owner (Power BI workspace admin) | Changes the dataset parameter and refreshes |
| Compliance lead | Gives the go and the no-go; decides a rollback |

- The source is running and published; the operator's checkout has `.secrets/pseudonymization.key`
  (and its backup, see [runbook.md](runbook.md#pseudonymization-key)).
- `az login` as the operator, prod environment: `DATABRICKS_HOST=<workspace URL>`,
  `DATABRICKS_AUTH_TYPE=azure-cli`, `TARGET=prod`.
- `bi/` rendered for prod (`make bi TARGET=prod`) and published to the Power BI workspace with
  `ServingPlatform = Trino`; gateway data sources for the SQL warehouse and for Trino.
- Variables for the commands: `FROM`/`UNTIL` = the business dates in service, `TAG=cutover-<date>`.

## 1. Preflight (the day before, and as often as needed)

```bash
make cutover TARGET=prod PHASE=preflight FROM=2022-09-01 UNTIL=2022-09-18 TAG=cutover-prod
```

Every check must pass, except `parity`, which can only pass after the final sync. A failed
`key_custody` stops everything: the target holds pseudonyms of a key this host does not have;
restore the key from its backup, or rotate ([runbook.md](runbook.md#rotating-the-key)) as part
of the final sync.

## 2. Freeze

Announce the freeze (no source deployment, no manual change to either lakehouse), then:

```bash
make cutover TARGET=prod PHASE=freeze FROM=... UNTIL=... TAG=cutover-prod
```

The tag is the reference of everything that follows. If the source publishes again before the
switch (a late correction), the switch refuses (`source_frozen`): delete nothing, sync again from
step 3 with a new tag.

## 3. Final sync

```bash
make export FROM=$FROM UNTIL=$UNTIL BRANCH=$TAG
make deliver TARGET=prod
make ingest TARGET=prod            # or `make rekey TARGET=prod` when rotating the key
make daily TARGET=prod             # every date; one date when only one was delivered again
make parity TARGET=prod FROM=$FROM UNTIL=$UNTIL BRANCH=$TAG
```

Exports are content-addressed: a date whose content did not change since its last delivery is not
delivered again (same delivery id).

## 4. Switch

```bash
make cutover TARGET=prod PHASE=switch FROM=... UNTIL=... TAG=cutover-prod
```

On a pass, the report owner sets `ServingPlatform = Databricks` in the dataset's settings and
refreshes; the compliance lead checks the alert count of the last date on the Alerts page against
the `report_rows` detail of the switch. The cutover is done: the target is the publisher of record.

## 5. Rollback (any time within the rollback window)

Triggers: the switch fails after the parameter change (refresh error), a number differs from the
source, or the target cannot publish a business date by the reporting deadline.

```bash
make cutover TARGET=prod PHASE=rollback FROM=... UNTIL=... TAG=cutover-prod
```

On a pass, the report owner sets `ServingPlatform = Trino` and refreshes. A failed
`source_current` lists the business dates to run on the source first (its `make airflow-backfill`);
run them, then the rollback again. The target keeps running and publishing: the investigation
happens there, and the next switch is phase 4 again.

## 6. Closing the window

After the rollback window (`migration/cutover.yaml`, 14 days) with no rollback, the source's
analytical layers are decommissioned (step 10). The sanctions screening and the core banking
reconciliation stay on premises with a query engine for the report's `OnPremises` tables.
