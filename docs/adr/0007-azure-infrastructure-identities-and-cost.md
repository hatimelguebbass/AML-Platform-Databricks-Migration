# ADR 0007: Azure infrastructure, identities and cost guardrails of prod

- Status: accepted

## Context

Steps 1 to 5 ran on the dev workspace (Databricks Free Edition). Prod is an Azure Databricks
workspace that exists for a limited window (steps 6 to 9) on a subscription with a spending limit,
then is destroyed (step 10). It must reach what the bundle expects: a catalog `aml_prod`, a SQL
warehouse named `aml-prod`, the account groups of ADR 0005, a `landing.deliveries` volume, and an
identity CI can deploy with.

Facts checked on the subscription before writing the configuration:

- The subscription has a 4 vCPU regional quota with no increase: classic clusters cannot run a
  driver and a worker of a usable size.
- West Europe refuses new customers of this offer (`RequestDisallowedByAzure`); France Central
  accepts them and offers serverless compute.
- A serverless workspace requires the Premium tier: the trial tier is refused
  (`InvalidSkuForServerlessWorkspace`).
- The `azurerm` provider (5.14) has no argument for the workspace compute mode; the ARM API
  (`Microsoft.Databricks/workspaces@2026-01-01`) has `computeMode`.
- The workspace joins the region's Unity Catalog metastore when it is created.
- Personal Microsoft accounts are guests of their own tenant, and Azure Databricks refuses their
  browser sign-in. Their Azure CLI tokens work on the workspace and account APIs.

## Decision

**Serverless workspace.** Compute runs in Databricks' network and is billed per use: no VM quota,
no managed resource group, nothing running between jobs. It is created through the `azapi`
provider with `computeMode = "Serverless"` until `azurerm` supports it. Every resource sits in one
resource group in France Central.

**Terraform owns what is outside the bundle; the bundle owns what is inside the catalog.**

| Owner | Objects |
|---|---|
| `infra/terraform/bootstrap` (applied once, state in its own account) | Resource group and storage account of the Terraform state: Entra ID only, blob versioning |
| `infra/terraform/prod` | Resource group, workspace, ADLS Gen2 account (hierarchical namespace, shared keys disabled), access connector and its role on the account, storage credential, external locations `catalog` and `landing`, catalog `aml_prod` (managed location on ADLS), account groups and their workspace assignments, deployer service principal and its federated credential, SQL warehouse `aml-prod`, budget |
| Bundle (`databricks.yml`, `resources/`) | Schemas and their grants, volumes, jobs |

The catalog, storage credential and external locations are bound to the prod workspace only
(isolated) and owned by the `data-platform-admins` group, whose members are the operators. The
deployer service principal is in `aml-pipeline`, which holds `USE_CATALOG` and `CREATE_SCHEMA` on
the catalog and `CREATE_EXTERNAL_VOLUME` on the landing location: it creates, and so owns, the
schemas, tables and volumes. Catalog grants are authoritative, so a grant made by hand is removed
at the next apply.

**The landing volume is external in prod.** The deliveries stay in the `landing` container,
outside the catalog's managed location, so the files the on-premises export uploads are in the
account's own storage under a path it controls. The bundle switches `landing.deliveries` to
`EXTERNAL` for the prod target only; dev keeps the managed volume.

**Identities.**

- CI deploys prod as a service principal through OpenID Connect: GitHub issues a token for the
  `prod` environment of this repository, Entra ID exchanges it through the federated credential
  (`repo:Revoyaldev@339949978/aml-platform-databricks-migration@1458213653:environment:prod`: GitHub
  names owner and repository with their immutable ids, so a repository recreated under the same
  name cannot inherit the trust), and the Databricks CLI
  and SDK use it (`DATABRICKS_AUTH_TYPE=github-oidc-azure`). No secret exists. The jobs run as
  that principal.
- Terraform is applied by an operator with their own Azure CLI login, not by CI: it needs rights
  on the tenant (application registration) and the account (groups) that the deploying principal
  must not hold. CI checks the code: `terraform fmt`, `validate` and TFLint with the azurerm rules.
- Operators sign in to the browser with users of the tenant itself; the personal account that owns
  the subscription stays an API identity.

**Cost guardrails.**

- The subscription's spending limit stops every resource when the credit is spent; it is never
  removed without a written decision.
- A monthly budget of 40 USD alerts at 50 % and 80 % of actual cost and at 100 % of forecast.
- The SQL warehouse is serverless, 2X-Small, one cluster, and stops after one idle minute: it
  starts in seconds, so a longer idle period only bills idle time.
- No job has a trigger (ADR 0006); nothing runs unless the on-premises scheduler, CI or an
  operator starts it.
- The window ends with `terraform destroy` of prod, then of the bootstrap (step 10).

## Consequences

- The workspace resource uses a second provider (`azapi`); it moves to `azurerm` once the compute
  mode is supported there, with a `moved` block or an import.
- Serverless compute reaches the storage account over its public endpoint, with Entra ID only.
  A long-lived platform would add a network connectivity configuration with private endpoints,
  which bills per hour; for a short window that is cost without a matching risk, since the account
  accepts no keys and no anonymous access.
- An operator applies the infrastructure from a workstation: a reviewed `terraform plan` saved to
  a file, then `terraform apply` of that file (`make infra-plan`, `make infra-apply`). Running it in
  CI would require a second, highly privileged federated identity.
- The pricing tier is Premium from the first hour: the 14-day trial tier does not exist for
  serverless workspaces, so every DBU is paid from the credit.

## Update, step 10: closing the window

The window closes by destroying everything this configuration created, and the result is checked,
not assumed ([procedure](../migration-plan.md#closing-the-azure-window)).

- **Reverse order, each part by its owner.** The bundle's schemas, tables, volumes and jobs belong to
  the deployer service principal, and Unity Catalog refuses to drop a catalog that still holds
  schemas: CI destroys the bundle first (`workflow_dispatch`, `prod: destroy`). Then `terraform
  destroy` of prod from a saved plan, then of the bootstrap, whose state is first moved out of the
  account it describes (`make bootstrap-local-state`).
- **Only what the configuration created.** The operators' Databricks users existed before it and
  were adopted with `force`; a destroy would delete them from the account, including its original
  admin. They are removed from the state, not destroyed.
- **Proof.** Before each destroy, `make infra-destroy-plan` keeps the identifier of every object of
  the state; afterwards `make teardown` looks each one up where it lived (Resource Manager, Entra ID,
  the Databricks account; objects of the workspace go with it), sweeps the subscription and the
  account's metastores, records the actual cost per day, and fails if anything is left. The
  identifiers stay on the operator's host; the committed report holds counts per type.
- **CI no longer deploys prod on every push to `main`**: a merge after the teardown would fail
  against a workspace that no longer exists. Prod is deployed or its bundle destroyed on demand.
- Left outside Terraform, by hand: the `prod` GitHub environment and its variables, the operators'
  tenant user, and the subscription, which the free account disables at the end of its credit.

Result: 50 objects of the two states looked up after the destroy, none left; the subscription
holds no resource group and the account's metastore no workspace ([teardown](../assessment/teardown.md)).
The destroy stopped three times on dependencies no reference expresses (workspace bindings, soft-deleted
managed tables, role assignments), and one identity was only deactivated; each cause is fixed in the
configuration ([postmortem](../postmortems/teardown-destroy-order.md)). Cost of the 4 days of prod:
10.91 USD of Databricks usage at list price and 11.02 USD charged to the subscription, both under the
low end of the estimate scaled to the same days ([operations](../assessment/operations.md)).
