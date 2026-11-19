# Migration plan

Strategy and the reasons for it: [ADR 0001](adr/0001-replatform-in-waves.md). What exists in the
source and where each object goes is generated from the source itself:

| Document | Content | Source of truth |
|---|---|---|
| [assessment/inventory.md](assessment/inventory.md) | Tables, columns, volumes, dbt models and tests, SQL functions, access rules | `migration/inventory.json` (`make inventory`) |
| [assessment/classification.md](assessment/classification.md) | Columns that relate to a person, direct or indirect | `migration/classification.yaml` |
| [assessment/waves.md](assessment/waves.md) | Fate of every object, wave by wave | `migration/waves.yaml` |
| [assessment/cost-estimate.md](assessment/cost-estimate.md) | Cost of the Azure window, before it starts | `migration/cost-assumptions.yaml`, `migration/azure-prices.json` |

CI fails when a generated document differs from what the committed files render, when a column
has no classification, or when an object has no fate or a rebuilt object reads something that
does not migrate.

## Steps

| Step | Delivers | Exit criterion |
|---|---|---|
| 1. Assessment and foundation | Inventory, classification, waves, cost estimate; bundle with catalog, schemas, landing volume and platform check job deployed to dev | Platform check job green on dev from CI |
| 2. Hybrid boundary | On-premises export of the allowed columns with a manifest; PII gate that blocks a delivery; idempotent upload to the landing volume | A delivery with a non-allowed column is refused before upload |
| 3. Bronze ingestion | Whole deliveries into Delta bronze after manifest, contract, checksum and control-total checks; delivery log; lineage columns | Every landed delivery accepted with its totals reconciled; a tampered one rejected |
| 4. Silver and gold conversion | dbt project on Databricks SQL, translation report, Unity Catalog grants, masks and tags | dbt build and tests pass on dev; as many alerts as on the source |
| 5. Orchestration | Lakeflow Jobs per business date, write-audit-publish on a staging schema | One business date end to end on dev |
| 6. Azure infrastructure | Terraform: state, serverless workspace, ADLS Gen2, external locations, catalog, groups, OpenID Connect identity, SQL warehouse, budget alert; prod deployment from CI | Platform check and transfer check green on prod from CI |
| 7. Parallel run and parity | Same business dates on both platforms; column-level parity report (`make parity`, [ADR 0008](adr/0008-migration-acceptance-and-parity.md), [report](assessment/parity.md)) | Zero difference on alerts; other differences explained |
| 8. Reliability, security and cost | Source checks replayed; lineage and audit; measured cost against this estimate; liquid clustering measured (`make operations`, `make benchmark`, [ADR 0009](adr/0009-reliability-security-and-cost.md), [evidence](assessment/operations.md)) | Measured cost within the estimate's range, or explained |
| 9. Cutover and rollback | Report rendered for the target; go/no-go checks; executed cutover, executed and timed rollback; key rotation; incident drill (`make cutover`, [ADR 0010](adr/0010-cutover-and-rollback.md), [runbook](cutover.md), [record](assessment/cutover.md), [postmortems](postmortems/)) | Rollback timed; postmortem written |
| 10. Wrap-up | Final documentation; bundle and `terraform destroy` with a verified result (`make teardown`, [teardown](assessment/teardown.md), [closing the window](#closing-the-azure-window)); [source decommissioning plan](decommissioning.md) | No Azure resource left |

## Orchestration mapping

The source runs one Airflow DAG, `aml_daily`, per business date. Its tasks map to the target as
follows (built in step 5, job `aml_daily`, [ADR 0006](adr/0006-orchestration-and-write-audit-publish.md)):

| Source task | Target |
|---|---|
| `wait_for_deliveries` | On premises: the export waits for the same deliveries, delivers them, then starts `aml_daily` with the business date |
| `open_staging` (reset the Nessie `etl` branch) | Task `open_staging`: every dated staging slice whose run differs from the published one is reset to it |
| `ingest_payments`, `ingest_cards` | Export of the bronze partition of the date, then task `ingest` (job `ingest_bronze`) |
| `ingest_corebanking` | Stays on premises (change data capture); the change log is exported without direct identifiers |
| `ingest_sanctions` | Stays on premises (screening needs the listed names) |
| `audit_bronze` | Task `audit_bronze`: the date's slices come from one accepted delivery, with the manifest's rows |
| `build_silver`, `audit_silver`, `build_gold`, `audit_gold` | dbt tasks of the same names, in the staging schemas |
| `publish` (merge `etl` into `main`) | Task `publish`: each table's slice (or whole table) copied in one commit, then `observability.publication` |

## Exporting and delivering

Runs on premises, on the host that reaches the source's Nessie and storage, with the pinned source
running and its pipeline published:

```bash
make key                                        # once: .secrets/pseudonymization.key, mode 600
make export FROM=2022-09-01 UNTIL=2022-09-18    # one delivery per date in exports/
make deliver TARGET=dev                         # needs DATABRICKS_HOST and DATABRICKS_TOKEN
```

| Exit code of `lakemig export` | Meaning | What to do |
|---|---|---|
| 0 | Every date delivered, or already delivered with the same content | Nothing |
| 1 | Policy out of date, a source table's columns changed, or an undated capture run after the daily runs began | Read the message: collect the inventory again and classify, or rerun the capture through the daily pipeline |
| 2 | PII gate blocked a date; nothing written for it | Read `exports/_blocked/<date>.json` (masked). Fix the source, or add a reviewed exemption to `migration/export.yaml` if the match is not personal data |

Back up the key on premises: losing it breaks the joins of new deliveries with the ones already
landed, and a new key means reloading every date. `make deliver` can be rerun at any time; it
skips complete deliveries and finishes interrupted ones.

## Ingesting into bronze

In the workspace, after `make deliver`:

```bash
make ingest TARGET=dev                    # job ingest_bronze; fails if it rejected a delivery
make delivery-log TARGET=dev              # latest decision and reasons for each delivery
make ingest TARGET=dev RETRY=<id>[,<id>]  # decide again, e.g. after a reviewed contract change
```

| Status in `landing.delivery_log` | Meaning | What to do |
|---|---|---|
| `accepted` | The date's slice of every bronze table holds this delivery | Nothing |
| `superseded` | A later export of the same date exists | Nothing |
| `rejected` | A check failed; nothing of it reached bronze; the files stay in the volume | Read the reasons. Transfer damage (size, SHA-256): the same export gives the same id and `make deliver` skips a sealed delivery, so remove the delivery's directory from the volume (the log keeps the evidence), `make deliver`, then `make ingest RETRY=<id>`. Contract or key: a reviewed change on premises first (ADR 0004) |

The landing contract is frozen from real deliveries with `make contract` after a reviewed change of
the export policy; CI checks it against the policy.

## Transforming and publishing

After `make deliver`, on the dev workspace:

```bash
make groups TARGET=dev                    # once per workspace: account groups, aml-pipeline membership, catalog grants
make daily DATE=2022-09-18 TARGET=dev     # job aml_daily for one business date
make daily FROM=2022-09-01 UNTIL=2022-09-18 TARGET=dev   # each date in order, stopping at the first failure
make daily TARGET=dev                     # business_date=all: rebuild every date (after a model change)
make publications TARGET=dev              # what was published, by which run
make translation TARGET=dev               # measure sqlglot's translation against the port (writes migration/translation.json)
```

When a run fails, the published layers are untouched. Read the failed task's output (an audit names
the failing test), fix the cause, then either start a new run for the date or repair the failed one
from `open_staging`:

```bash
make repair RUN=<run id>                  # reruns open_staging and every task after it, with the run's parameters
```

Repair through the UI or API only with the run's `business_date`: a repair without job parameters
runs with their defaults, and the empty default fails the first audit on purpose. A repair from the
failed audit alone is refused at publication (`StaleStagingError`), as the tables before it were
built by an earlier run.

| Publication error | Meaning | What to do |
|---|---|---|
| `SchemaChangedError` | A model's columns changed since its table was published | Deploy the change, then `make daily` without a date: the rebuild recreates the changed tables (readers miss them for seconds) |
| `GovernanceDriftError` | A published column carries another mask than its model declares | Review the mask change; set it on the published table by hand once approved |
| `StaleStagingError` | Rows to publish were built by another run | Repair from `open_staging`, or start a new run |

A new member of `aml-pipeline` or `aml-compliance` reads unmasked values only after a few minutes; until
then a build fails its account resolution tests instead of publishing nulls. A change to a model's
columns needs its classification tag and, for pseudonyms, its mask in the model's YAML: `make
assessment` says which. A ported model needs its hand changes in `migration/translation.yaml`.

| Group | Reads | Pseudonyms |
|---|---|---|
| `aml-compliance` | silver, gold, reference | clear |
| `aml-analysts` | silver, reference | masked |
| `aml-reporting` | silver, gold, reference | masked |
| `aml-pipeline` | everything it builds | clear |

## Azure infrastructure (prod)

Prod is applied by an operator with their own Azure CLI login; CI only checks the code (`make
infra-check`) and deploys the bundle ([ADR 0007](adr/0007-azure-infrastructure-identities-and-cost.md)).
The operator is a user of the subscription's tenant: Azure Databricks refuses browser sign-in to
personal Microsoft accounts, whose Azure CLI tokens still work on its APIs. The first sign-in to
<https://accounts.azuredatabricks.net> must be made by a Global Administrator of the tenant.

```bash
az login --use-device-code
# Once per subscription: the state account. Its own state starts local, then moves into it.
cd infra/terraform/bootstrap
# First time only, with the backend block of main.tf commented out:
terraform init && terraform apply -var subscription_id=<id>
# Then with the block restored (backend.hcl from the outputs):
terraform init -migrate-state -backend-config=backend.hcl
cd -
# Every change: variables in infra/terraform/prod/terraform.tfvars (ignored; see terraform.tfvars.example)
make infra-init
make infra-plan        # review the saved plan
make infra-apply
terraform -chdir=infra/terraform/prod output
```

The outputs go to the variables of the `prod` GitHub environment, read by the CI job `prod`:

| Variable | Value |
|---|---|
| `DATABRICKS_HOST` | output `workspace_url` |
| `ARM_CLIENT_ID` | output `deployer_client_id` |
| `ARM_TENANT_ID` | tenant of the subscription |
| `LANDING_LOCATION` | output `landing_location` |
| `ALERT_RECIPIENT` | mailbox of the daily job's failure alerts |

None is a secret: the job authenticates through OpenID Connect. It runs on demand
(`workflow_dispatch`, input `prod: deploy` or `prod: destroy`); during steps 6 to 9 it also ran on
every push to `main` after dev. Once the window is closed, a deployment needs `make infra-apply` first.

A lost bootstrap state is rebuilt with `terraform import` of its four resources; a corrupted prod
state is restored from a previous blob version of `tfstate/prod.tfstate`.

## Closing the Azure window

Prod is destroyed in the reverse order of its creation, each part by the identity that owns it, and
the result is checked rather than assumed ([ADR 0007](adr/0007-azure-infrastructure-identities-and-cost.md)):

```bash
# 1. Last measurement while the system tables are reachable
make operations DEPLOYER=<deployer application id> SUBSCRIPTION=<id> WINDOW_START=<first day of prod> TARGET=prod

# 2. The bundle's schemas, tables, volumes and jobs, as the deployer that owns them
gh workflow run ci.yml --ref <branch> -f prod=destroy

# 3. The operators predate the configuration (adopted with force): released, not deleted
terraform -chdir=infra/terraform/prod state rm 'databricks_user.operators'
make infra-destroy-plan ROOT=prod       # keeps every identifier in .teardown/, releases bindings and role assignments, plans
make infra-destroy ROOT=prod

# 4. The state account last, once its state is local
make bootstrap-local-state
make infra-destroy-plan ROOT=bootstrap
make infra-destroy ROOT=bootstrap

# 5. Proof, and the window's cost per day
make teardown SUBSCRIPTION=<id> ACCOUNT=<Databricks account id> WINDOW_START=<first day of prod>
```

`make teardown` looks up every object of `.teardown/inventory.json` where it lived (Resource
Manager, Entra ID, the Databricks account), lists what is left in the subscription and the
metastores that still serve a workspace, and fails if anything remains
([teardown](assessment/teardown.md)). If a destroy stops part way, fix the cause and run
`make infra-destroy-plan` again: the inventory keeps what is already gone. What stopped this one,
and why bindings and role assignments are released rather than destroyed:
[postmortem](postmortems/teardown-destroy-order.md). Cost Management posts the last day late: running it again a day
later only updates the cost.

Outside Terraform, by hand: the `prod` GitHub environment and its variables, the tenant user of the
operators, and the subscription itself, which the free account disables at the end of its credit.

## Reproducing the assessment

The inventory is collected from the running source platform at the pinned commit:

```bash
make source                                  # checks out the pinned commit into .source/
cd .source/aml-compliance-platform
make up up-cdc data cdc pipeline             # see that repository's README
cd -
make inventory                               # compiles its dbt project, queries Trino
make assessment                              # checks, renders docs/assessment/
```

The full source needs about 10 GB of memory. The inventory reads table metadata and counts only.
