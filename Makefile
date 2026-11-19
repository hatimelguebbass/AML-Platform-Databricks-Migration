SHELL := /usr/bin/env bash
.DEFAULT_GOAL := help

TARGET ?= dev
SOURCE_DIR ?= .source/aml-compliance-platform
SOURCE_REPOSITORY = $(shell sed -n 's/^repository = "\(.*\)"/\1/p' source/pin.toml)
SOURCE_COMMIT = $(shell sed -n 's/^commit = "\(.*\)"/\1/p' source/pin.toml)
# dbt and adapter of the transformations; the daily job's dbt environment pins the same versions.
DBT := uvx --quiet --from dbt-core==1.11.17 --with dbt-databricks==1.13.0 dbt
# dbt and adapter versions of the source platform (its infra/dbt/requirements.in), used only to compile it.
SOURCE_DBT := uvx --quiet --from dbt-core==1.11.17 --with dbt-trino==1.11.0 dbt
DBT_TARGET := $(abspath .source/dbt-target)
# The source project rendered offline for the translation report, with the change batches read where they land.
RENDER_DBT := uvx --quiet --from dbt-core==1.11.17 --with dbt-duckdb==1.11.1 dbt
SOURCE_SQL := $(abspath .source/dbt-render)

.PHONY: help install lint format typecheck test-unit test source inventory assessment assessment-check prices \
	catalog validate deploy platform-check key export deliver transfer-check \
	contract ingest rekey delivery-log parity bi cutover benchmark operations ci-durations query groups sql dbt-parse dbt-selectors daily repair publications source-sql translation \
	infra-check infra-init infra-plan infra-apply destroy infra-destroy-plan infra-destroy bootstrap-local-state teardown

help: ## List targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-18s %s\n", $$1, $$2}'

install: ## Install the package, assessment tooling and dev tools
	uv sync --all-extras

lint: ## Lint and check formatting
	uv run ruff check .
	uv run ruff format --check .

format: ## Format the code
	uv run ruff check --fix .
	uv run ruff format .

typecheck: ## Static type check
	uv run mypy

test-unit: ## Tests that need neither the source platform nor a workspace
	uv run pytest -m "not integration"

test: lint typecheck test-unit assessment-check ## Everything CI runs before deploying

source: ## Check out the pinned commit of the source platform into SOURCE_DIR
	@test -d $(SOURCE_DIR)/.git || git init -q $(SOURCE_DIR)
	git -C $(SOURCE_DIR) fetch -q --depth 1 $(SOURCE_REPOSITORY) $(SOURCE_COMMIT)
	git -C $(SOURCE_DIR) checkout -q --detach $(SOURCE_COMMIT)

# The source platform must be running with its pipeline published (in SOURCE_DIR: make up up-cdc data cdc pipeline).
inventory: ## Collect migration/inventory.json from the running source platform
	TRINO_HOST=localhost TRINO_PORT=8080 $(SOURCE_DBT) compile --quiet --project-dir $(SOURCE_DIR)/transform \
	  --profiles-dir $(SOURCE_DIR)/transform --target-path $(DBT_TARGET)
	uv run lakemig inventory --source $(SOURCE_DIR) --manifest $(DBT_TARGET)/manifest.json

prices: ## Snapshot the Azure retail prices of migration/cost-assumptions.yaml
	uv run lakemig prices

assessment: ## Check classification and waves against the inventory, render docs/assessment/
	uv run lakemig assess

assessment-check: ## Fail if docs/assessment/ is not what the migration files render
	uv run lakemig assess --check

# Free Edition creates catalogs on its default storage only through SQL, not the catalogs API.
CATALOG = aml_$(TARGET)
catalog: ## Create the catalog of TARGET through the SQL warehouse where absent (dev; prod's is Terraform's)
	@warehouse=$$(databricks warehouses list --output json | jq -r 'first(.[].id)'); \
	result=$$(databricks api post /api/2.0/sql/statements --json \
	  "{\"warehouse_id\": \"$$warehouse\", \"statement\": \"CREATE CATALOG IF NOT EXISTS $(CATALOG)\", \"wait_timeout\": \"50s\"}"); \
	echo "$$result" | jq -r '"CREATE CATALOG IF NOT EXISTS $(CATALOG): " + .status.state + " " + (.status.error.message // "")'; \
	echo "$$result" | jq -e '.status.state == "SUCCEEDED"' >/dev/null

# Free Edition has no account console: account groups go through the workspace's account SCIM proxy.
# In prod, Terraform owns the groups and their members (step 6).
GROUPS := aml-compliance aml-analysts aml-reporting aml-pipeline aml-migration
READER_GROUPS := aml-compliance aml-analysts aml-reporting aml-migration
SCIM_GROUPS := /api/2.0/account/scim/v2/Groups
groups: ## Create the account groups where absent, put the deploying identity in aml-pipeline, grant the catalog (dev)
	@for group in $(GROUPS); do \
	  found=$$(databricks api get "$(SCIM_GROUPS)?filter=displayName%20eq%20%22$$group%22" | jq -r '.Resources[0].id // empty'); \
	  if [ -z "$$found" ]; then \
	    databricks api post $(SCIM_GROUPS) --json "$$(jq -n --arg g "$$group" '{displayName: $$g}')" >/dev/null; \
	    echo "created $$group"; \
	  fi; \
	done
	@pipeline=$$(databricks api get "$(SCIM_GROUPS)?filter=displayName%20eq%20%22aml-pipeline%22" | jq -r '.Resources[0].id'); \
	me=$$(databricks current-user me --output json | jq -r .id); \
	databricks api patch $(SCIM_GROUPS)/$$pipeline --json "$$(jq -n --arg m "$$me" \
	  '{schemas: ["urn:ietf:params:scim:api:messages:2.0:PatchOp"], Operations: [{op: "add", path: "members", value: [{value: $$m}]}]}')" >/dev/null
	@for group in $(READER_GROUPS); do \
	  $(MAKE) --no-print-directory sql STATEMENT="GRANT USE CATALOG ON CATALOG $(CATALOG) TO \`$$group\`" || exit 1; \
	done

sql: ## Run STATEMENT on the SQL warehouse of TARGET
	@warehouse=$$(databricks warehouses list --output json | jq -r 'first(.[].id)'); \
	result=$$(databricks api post /api/2.0/sql/statements --json \
	  "$$(jq -n --arg w "$$warehouse" --arg s "$$STATEMENT" '{warehouse_id: $$w, statement: $$s, wait_timeout: "50s"}')"); \
	echo "$$result" | jq -r --arg s "$$STATEMENT" '"\($$s): " + .status.state + " " + (.status.error.message // "")'; \
	echo "$$result" | jq -e '.status.state == "SUCCEEDED"' >/dev/null

validate: ## Validate the bundle for TARGET (needs DATABRICKS_HOST and DATABRICKS_TOKEN)
	databricks bundle validate --strict --target $(TARGET)

deploy: ## Deploy the bundle to TARGET (DEPLOY_ARGS=--auto-approve only after reviewing a destructive plan)
	databricks bundle deploy --target $(TARGET) $(DEPLOY_ARGS)

platform-check: ## Run the platform check job on TARGET
	databricks bundle run --target $(TARGET) platform_check

# On premises, next to the running source platform (ADR 0003). The key never leaves this host.
key: ## Generate the pseudonymization key, once (.secrets/pseudonymization.key; LAKEMIG_PSEUDONYMIZATION_KEY overrides)
	uv run lakemig key

UNTIL ?= $(FROM)
# Published branch of the source lakehouse, or the tag of a cutover freeze.
BRANCH ?= main
export: ## Export business dates FROM..UNTIL of the running source to exports/ (PII gate included)
	@test -n "$(FROM)" || { echo "usage: make export FROM=YYYY-MM-DD [UNTIL=YYYY-MM-DD]"; exit 2; }
	uv run lakemig export --from $(FROM) --until $(UNTIL) --branch $(BRANCH) --source-env $(SOURCE_DIR)/.env

deliver: ## Upload every complete delivery in exports/ to the landing volume of TARGET (idempotent)
	uv run lakemig deliver --volume /Volumes/$(CATALOG)/landing/deliveries

transfer-check: ## Transfer integration tests against the Files API of TARGET
	LAKEMIG_CATALOG=$(CATALOG) uv run pytest -m integration tests/integration

contract: ## Freeze the landing contract from the complete deliveries in exports/ (a reviewed change)
	uv run lakemig contract

ingest: ## Ingest the sealed deliveries of TARGET's landing volume into bronze (RETRY=<delivery ids> to decide again)
	@$(call run_job,ingest_bronze,{$(if $(RETRY),\"retry\": \"$(RETRY)\")})

# Jobs run by name, as `daily`: prod's bundle state is the deployer's, not the operator's.
# A rotation of the on-premises key (docs/runbook.md): every accepted date exported again under the
# new key and delivered first.
rekey: ## Re-ingest every accepted date of TARGET under the current on-premises key, all or nothing
	@key=$$(uv run lakemig key --id) && echo "rotating $(CATALOG) to key $$key" && \
	$(call run_job,ingest_bronze,{\"rekey\": \"$$key\"})

dbt-parse: ## Parse the dbt project offline (no workspace): Jinja, refs, configs, tests
	DBT_PROFILES_DIR=tests/dbt $(DBT) parse --project-dir transform --target-path $(abspath .dbt/target)

# The daily job's audits select by these: a test left out of every selector would never run.
DBT_LS = DBT_PROFILES_DIR=tests/dbt $(DBT) ls --quiet --project-dir transform --target-path $(abspath .dbt/target) \
  --resource-type test unit_test --output name
dbt-selectors: ## Fail unless the selectors of the daily job's audits run every test exactly once
	@diff <($(DBT_LS) | sort) <(for s in bronze_audit silver gold observability; do $(DBT_LS) --selector $$s; done | sort) \
	  && echo "every test runs in exactly one audit"

# By job name rather than `bundle run`: the bundle's deployment state lives under the deployer's
# root path, which an operator other than the deployer (prod's service principal) cannot resolve.
run_job = set -o pipefail; job=$$(databricks jobs list --name $(1) --output json | jq -r 'first(.[].job_id) // empty'); \
  test -n "$$job" || { echo "no job $(1) on $(TARGET): make deploy first"; exit 1; }; \
  databricks jobs run-now --timeout 60m --json "{\"job_id\": $$job, \"job_parameters\": $(2)}" --output json \
  | jq -r '"run \(.run_id): \(.state.result_state) in \(.run_duration / 1000 | floor) s, \(.run_page_url)"'

daily: ## Run the daily job on TARGET for DATE, or each date FROM..UNTIL in order; neither rebuilds every date
	@if [ -n "$(DATE)" ]; then dates="$(DATE)"; \
	elif [ -n "$(FROM)" ]; then dates=$$(python3 -c 'import datetime as d, sys; a, b = map(d.date.fromisoformat, sys.argv[1:]); \
	  print(*(a + d.timedelta(n) for n in range((b - a).days + 1)))' $(FROM) $(UNTIL)); \
	else dates=all; fi; \
	for day in $$dates; do \
	  echo "== $$day"; $(call run_job,aml_daily,{\"business_date\": \"$$day\"}) || exit 1; \
	done

# A repair without job parameters runs with their defaults: this one passes the run's own.
FROM_TASK ?= open_staging
repair: ## Repair daily run RUN from FROM_TASK (default open_staging) and every task after it, same parameters
	@test -n "$(RUN)" || { echo "usage: make repair RUN=<run id> [FROM_TASK=open_staging]"; exit 2; }
	@run=$$(databricks jobs get-run $(RUN) --include-history --output json); \
	databricks jobs repair-run --timeout 60m --json "$$(echo "$$run" | jq --arg task $(FROM_TASK) '{ \
	  run_id: .run_id, rerun_tasks: [$$task], rerun_dependent_tasks: true, \
	  latest_repair_id: ([.repair_history[]? | select(.type == "REPAIR") | .id] | last), \
	  job_parameters: (.job_parameters | map({(.name): .value}) | add) \
	} | with_entries(select(.value != null))')"

PUBLICATIONS_SQL = SELECT coalesce(cast(business_date AS STRING), 'every date') AS business_date, run_id, \
  published_at, size(tables) AS tables, aggregate(tables, 0L, (n, t) -> n + t.rows) AS rows \
  FROM $(CATALOG).observability.publication ORDER BY published_at DESC LIMIT 20
publications: ## The latest publications of TARGET's transformed layers
	@$(MAKE) --no-print-directory query QUERY="$(PUBLICATIONS_SQL)"

source-sql: source ## Render the pinned source project's SQL offline, against TARGET's catalog
	@mkdir -p $(SOURCE_SQL)
	LAKEMIG_RENDER_DATABASE=$(SOURCE_SQL)/$(CATALOG).duckdb $(RENDER_DBT) compile --quiet \
	  --project-dir $(SOURCE_DIR)/transform --profiles-dir source/render --target-path $(SOURCE_SQL)/target \
	  --vars '{audit_schema: bronze}'

translation: source-sql ## Measure sqlglot's translation of the ported models against TARGET's tables (after make daily)
	uv run lakemig translate --source-sql $(SOURCE_SQL)/target/compiled --catalog $(CATALOG)

# On premises, next to the running source: only fingerprints and the rows of differing buckets come
# back from the target (ADR 0008). Needs the export key and an identity in aml-migration.
parity: ## Compare the running source with TARGET's published layers on FROM..UNTIL, render docs/assessment/parity.md
	@test -n "$(FROM)" || { echo "usage: make parity FROM=YYYY-MM-DD [UNTIL=YYYY-MM-DD]"; exit 2; }
	uv run lakemig parity --catalog $(CATALOG) --from $(FROM) --until $(UNTIL) --branch $(BRANCH) --source-env $(SOURCE_DIR)/.env; \
	  status=$$?; uv run lakemig assess && exit $$status

# Run once per layout: before a clustering change (LABEL=unclustered), and after it was published.
benchmark: ## Measure what migration/benchmark.yaml's queries read on TARGET's current layout, render docs/assessment/clustering.md
	@test -n "$(LABEL)" || { echo "usage: make benchmark LABEL=<layout name>"; exit 2; }
	uv run lakemig benchmark --catalog $(CATALOG) --label $(LABEL) && uv run lakemig assess

# WINDOW_START: the day prod was created, which opened the Azure window (ADR 0007).
operations: dbt-parse ## Collect cost, job runs, lineage and access of TARGET from the system tables, render docs/assessment/operations.md
	@test -n "$(DEPLOYER)" -a -n "$(WINDOW_START)" || \
	  { echo "usage: make operations DEPLOYER=<deployer application id> WINDOW_START=<YYYY-MM-DD> [SUBSCRIPTION=<id>]"; exit 2; }
	uv run lakemig operations --catalog $(CATALOG) --since $(WINDOW_START) --manifest .dbt/target/manifest.json \
	  --deployer $(DEPLOYER) $(if $(SUBSCRIPTION),--subscription $(SUBSCRIPTION)); \
	  status=$$?; uv run lakemig assess && exit $$status

# Durations of each CI job over the last successful runs (needs the GitHub CLI, signed in).
CI_RUNS ?= 10
CI_REPOSITORY ?= $(shell git remote get-url origin | sed -E 's#(git@github.com:|https://github.com/)##; s#\.git$$##')
ci-durations: ## Render docs/assessment/ci.md: median and longest duration of each CI job over the last CI_RUNS green runs
	@gh api "repos/$(CI_REPOSITORY)/actions/workflows/ci.yml/runs?status=success&per_page=$(CI_RUNS)" --jq '.workflow_runs[].id' \
	| while read -r run; do \
	    gh api "repos/$(CI_REPOSITORY)/actions/runs/$$run/jobs" \
	      --jq '.jobs[] | select(.conclusion == "success") | [.name, ((.completed_at | fromdate) - (.started_at | fromdate))] | @tsv'; \
	  done \
	| jq -R -s -r --arg runs $(CI_RUNS) -f .github/ci-durations.jq > docs/assessment/ci.md
	@cat docs/assessment/ci.md

DELIVERY_LOG_SQL = SELECT business_date, delivery_id, max_by(status, logged_at) AS status, \
  max_by(rows, logged_at) AS rows, max_by(array_join(reasons, ' | '), logged_at) AS reasons \
  FROM $(CATALOG).landing.delivery_log GROUP BY ALL ORDER BY business_date, delivery_id
delivery-log: ## Latest decision on each delivery of TARGET, through the SQL warehouse
	@$(MAKE) --no-print-directory query QUERY="$(DELIVERY_LOG_SQL)"

query: ## Print the rows of QUERY, run on TARGET's SQL warehouse
	@warehouse=$$(databricks warehouses list --output json | jq -r 'first(.[].id)'); \
	databricks api post /api/2.0/sql/statements --json \
	  "$$(jq -n --arg w "$$warehouse" --arg s "$$QUERY" '{warehouse_id: $$w, statement: $$s, wait_timeout: "50s"}')" \
	  | jq -r 'if .status.state == "SUCCEEDED" then (.manifest.schema.columns | map(.name) | @tsv), (.result.data_array // [] | .[] | @tsv) else "\(.status.state) \(.status.error.message // "")" end'

# The cutover of the compliance report (docs/cutover.md, ADR 0010).
bi: source ## Render bi/, the report of the cutover, from the pinned source's Power BI project and TARGET's warehouse
	@warehouse=$$(databricks warehouses list --output json | jq -r 'first(.[].id)'); \
	odbc=$$(databricks warehouses get $$warehouse --output json | jq -c .odbc_params); \
	uv run lakemig bi --source-model $(SOURCE_DIR)/bi --catalog $(CATALOG) \
	  --host $$(echo "$$odbc" | jq -r .hostname) --http-path $$(echo "$$odbc" | jq -r .path)

# On premises, next to the running source, as an identity of aml-migration. TAG names the freeze.
cutover: ## Run and record cutover PHASE (preflight|freeze|switch|rollback) of TARGET on FROM..UNTIL, frozen as TAG
	@test -n "$(PHASE)" -a -n "$(FROM)" -a -n "$(TAG)" || \
	  { echo "usage: make cutover PHASE=preflight|freeze|switch|rollback FROM=YYYY-MM-DD UNTIL=YYYY-MM-DD TAG=cutover-..."; exit 2; }
	uv run lakemig cutover $(PHASE) --catalog $(CATALOG) --from $(FROM) --until $(UNTIL) --tag $(TAG)

# Azure infrastructure of prod (ADR 0007). bootstrap holds the remote state and is applied once; the
# operator plans and applies prod with their own Azure CLI login, CI only checks the code.
TERRAFORM_ROOTS := infra/terraform/bootstrap infra/terraform/prod
TFLINT := TFLINT_CONFIG_FILE=$(abspath infra/terraform/.tflint.hcl) tflint
infra-check: ## Format, validate and lint the Terraform code (no Azure access)
	terraform fmt -check -recursive infra/terraform
	@for root in $(TERRAFORM_ROOTS); do \
	  terraform -chdir=$$root init -input=false -backend=false >/dev/null && terraform -chdir=$$root validate && \
	  $(TFLINT) --chdir=$$root || exit 1; \
	done

PROD_TF := terraform -chdir=infra/terraform/prod
infra-init: ## Initialize prod against its remote state (az login first)
	$(PROD_TF) init -input=false -backend-config=backend.hcl

infra-plan: ## Plan prod into infra/terraform/prod/prod.tfplan (variables: terraform.tfvars or TF_VAR_*)
	$(PROD_TF) plan -input=false -out=prod.tfplan

infra-apply: ## Apply the reviewed plan of make infra-plan
	$(PROD_TF) apply -input=false prod.tfplan

# Closing the Azure window (ADR 0007, docs/migration-plan.md#closing-the-azure-window): the bundle's
# objects first, by the identity that owns them (CI, `prod: destroy`), then each Terraform root, prod
# before the bootstrap that holds its state, then the proof that nothing is left.
destroy: ## Destroy what the bundle deployed to TARGET: schemas with their tables and volumes, jobs (prod only)
	@test "$(TARGET)" = prod || { echo "only prod's window closes; dev stays"; exit 2; }
	databricks bundle destroy --target $(TARGET) --auto-approve

ROOT ?= prod
ROOT_TF = terraform -chdir=infra/terraform/$(ROOT)
# Workspace bindings and role assignments go with the securable or the scope they attach to, and
# destroyed first they break the rest: an unbound isolated catalog is out of the workspace's reach,
# and deleting a role assignment needs User Access Administrator, not the operator's Contributor.
# They are kept in the inventory, so the teardown still proves they are gone.
infra-destroy-plan: ## Keep the identifiers ROOT's state manages in .teardown/, then plan its destroy
	$(ROOT_TF) show -json | uv run lakemig teardown inventory --root $(ROOT)
	$(ROOT_TF) state list | grep -E '^(databricks_workspace_binding|azurerm_role_assignment)\.' | \
	  xargs -r -d '\n' $(ROOT_TF) state rm
	$(ROOT_TF) plan -destroy -input=false -out=destroy.tfplan

infra-destroy: ## Apply the reviewed destroy plan of ROOT
	$(ROOT_TF) apply -input=false destroy.tfplan

bootstrap-local-state: ## Move the bootstrap's state out of the account it describes, before destroying it
	printf 'terraform {\n  backend "local" {}\n}\n' > infra/terraform/bootstrap/backend_override.tf
	terraform -chdir=infra/terraform/bootstrap init -input=false -migrate-state -force-copy

teardown: ## Prove nothing of the Azure window is left, record its cost per day (after every infra-destroy)
	@test -n "$(SUBSCRIPTION)" -a -n "$(ACCOUNT)" -a -n "$(WINDOW_START)" || \
	  { echo "usage: make teardown SUBSCRIPTION=<id> ACCOUNT=<Databricks account id> WINDOW_START=<YYYY-MM-DD>"; exit 2; }
	uv run lakemig teardown verify --subscription $(SUBSCRIPTION) --account $(ACCOUNT) --since $(WINDOW_START)
