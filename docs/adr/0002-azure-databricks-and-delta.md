# ADR 0002: Azure Databricks with Unity Catalog and Delta tables; Free Edition for dev

- Status: accepted

## Context

ADR 0001 re-platforms the AML lakehouse to the cloud. The target must:

- run Spark for the ingestion and SQL for the dbt models, without servers to operate;
- govern access per group, down to columns and rows: the source enforces this with Trino access
  rules (`docs/assessment/inventory.md`, access rules);
- deploy jobs from the repository, reviewed in pull requests, identically to each environment;
- cost nothing while idle: the platform runs one batch a day.

The source tables are Iceberg. The cloud environment is available for a short window only
(steps 6 to 9); development before it needs a free workspace.

## Decision

**Platform: Azure Databricks, Premium, with Unity Catalog and serverless compute.**

- Serverless jobs for Python and Spark tasks, a serverless SQL warehouse for dbt, Power BI and the
  parity queries. Nothing runs, or bills, between runs.
- Unity Catalog holds one catalog per environment (`aml_dev`, `aml_prod`) with the schemas
  `landing`, `bronze`, `silver`, `gold`, `observability` and `migration`. Schemas and the landing
  volume are bundle resources (`resources/schemas.yml`). The catalog is not: on Free Edition, the
  catalogs API refuses to create a catalog on the workspace's default storage and only SQL does
  (`make catalog`), and in `prod` Terraform creates it on its ADLS external location. Either way, a
  bundle destroy never drops a catalog.
- Jobs are deployed with Databricks Asset Bundles (renamed Declarative Automation Bundles in recent
  documentation), CLI pinned in `databricks.yml` and installed in CI after a SHA-256 check.
  Terraform manages only what exists outside the workspace (ADR 0007).

**Table format: Delta Lake** for every managed table.

- dbt model contracts become Delta `NOT NULL` and `CHECK` constraints that the engine enforces on
  write; Unity Catalog managed Iceberg tables do not support constraints or generated columns.
- Delta is what Lakeflow, dbt-databricks, liquid clustering and the change data feed are built
  around; no workload of this platform needs another engine to write the tables.
- Should an Iceberg reader appear, a Delta table can be exposed to Iceberg clients without a copy
  (Iceberg reads, formerly UniForm). Moving the data back to managed Iceberg tables later is a
  table rewrite, not a redesign.

**Environments.**

- `dev` is Databricks Free Edition: serverless only, one 2X-Small SQL warehouse, workspace default
  storage, no account console. CI deploys every pull request to it and runs the platform check
  job, which records the runtime it ran on in `aml_dev.migration.platform_check`.
- `prod` is Azure Databricks in the Azure window, on ADLS Gen2 through a Unity Catalog external
  location, destroyed at the end (ADR 0007).
- The bundle is the same for both; targets change only variables and the deployment mode.

**Where dbt runs.** As a dbt task of a Lakeflow job, its SQL executed by the environment's SQL
warehouse: the same path in `dev` and `prod`, scheduled and retried by the job like the other
tasks. CI only parses and compiles the project. The platform check job installs `dbt-databricks`
from PyPI into its serverless environment on every run, which proves the package index is
reachable from the dev workspace.

## Alternatives considered

| Option | Why not |
|---|---|
| Microsoft Fabric | Billed per capacity, which bills while running whether or not jobs run; its lakehouse is Delta too, so the format question is the same. Unity Catalog column masks, row filters and bundle deployments fit the access rules and the review flow this migration needs, and Databricks is what the targeted data engineering roles ask for |
| Azure Synapse Analytics | Microsoft directs new analytics investment to Fabric and has been retiring Synapse components; not a target to start a migration on |
| Databricks on classic clusters | Cluster sizing and start-up time to manage, and a 4-vCPU regional quota on the subscriptions available would not fit a multi-node cluster; serverless avoids both |
| Unity Catalog managed Iceberg tables | No constraints or generated columns, compression fixed, predictive optimization required; nothing in this platform needs Iceberg writers |
| Keep the tables in the source format, read through a foreign catalog | Foreign Iceberg tables are read-only in Databricks, so the cloud could never take over |
| One catalog with per-environment schema prefixes | Grants, masks and the bundle would need the prefix everywhere; one catalog per environment keeps names identical |

## Consequences

- dbt SQL moves from the Trino dialect to Databricks SQL (step 4): `docs/assessment/inventory.md`
  lists every function the models call and how the translator renders it.
- Iceberg-specific mechanisms of the source need equivalents: the Nessie branch behind
  write-audit-publish has none in Unity Catalog and is rebuilt with a staging schema (ADR 0006).
- Free Edition has no custom storage location: everything lands in managed volumes and tables of
  the workspace's default storage. Paths are never hard-coded; the code addresses Unity Catalog
  names only, so `prod` swaps in ADLS without code changes.
- Free Edition has no account console, hence no service principal: CI authenticates to `dev` with
  a personal access token kept in a GitHub environment secret. `prod` uses OpenID Connect
  federation and no stored secret (ADR 0007).
