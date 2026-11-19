# Architecture

The source platform keeps everything that needs a direct identifier; the cloud platform receives
exports without them and rebuilds the rest. Solid arrows exist today; dashed ones are built by the
step named on them.

```mermaid
flowchart LR
    subgraph onprem["On premises: aml-compliance-platform (pinned commit)"]
        direction TB
        corebanking[("Core banking<br/>PostgreSQL")]
        feeds["Payments and card<br/>files"]
        cdc["Debezium, Kafka"]
        lake[("Iceberg on SeaweedFS<br/>Nessie, Trino")]
        dbt_src["dbt on Trino<br/>silver, gold"]
        identity["Customer 360 matching<br/>sanctions screening<br/>(need names)"]
        export["Export (lakemig export)<br/>policy, pseudonyms,<br/>PII gate, manifest"]
        key[["Pseudonymization key<br/>never leaves"]]
        parity["Parity (lakemig parity)<br/>fingerprints, differing<br/>buckets row by row"]
        airflow["Airflow"]
        corebanking --> cdc --> lake
        feeds --> lake
        lake <--> dbt_src
        lake --> identity --> lake
        airflow -.-> dbt_src
        airflow -- "after export: run aml_daily<br/>for the business date" --> jobs
        lake -- "one Nessie commit" --> export
        key --- export
        lake -- "published tables,<br/>pseudonymized like the export" --> parity
        key --- parity
    end

    subgraph cloud["Azure Databricks: catalog aml_dev / aml_prod (Unity Catalog)"]
        direction TB
        landing[("landing<br/>volume deliveries")]
        bronze[("bronze<br/>Delta")]
        silver[("silver<br/>histories, payments, cards,<br/>parties (snapshot)")]
        gold[("gold<br/>alerts, evidence, rules,<br/>sanctions hits (snapshot)")]
        governance[["governance<br/>mask function"]]
        obs[("observability")]
        migration[("migration<br/>platform checks, parity")]
        jobs["Lakeflow job aml_daily (bundle)<br/>ingest, audit, build in staging,<br/>audit, publish"]
        log[("landing.delivery_log<br/>accepted, rejected,<br/>superseded")]
        landing -- "whole deliveries: manifest, contract,<br/>checksums, control totals" --> bronze
        landing --- log
        staging[("silver_staging, gold_staging,<br/>reference_staging,<br/>observability_staging")]
        bronze -- "dbt on the SQL warehouse" --> staging
        staging -- "publication after the audits" --> silver --> gold
        governance -. "column masks" .- silver
        staging -- "checks of the date:<br/>volumes, drift, reconciliation" --> obs
        jobs --> bronze
        jobs --> staging
    end

    export -- "Files API over HTTPS, manifest last,<br/>no direct identifiers" --> landing
    powerbi["Power BI report (bi/)<br/>parameter ServingPlatform"]
    gold -- "Lakehouse: SQL warehouse<br/>since the cutover" --> powerbi
    obs --> powerbi
    lake -. "Lakehouse: Trino,<br/>rollback only" .-> powerbi
    lake -- "OnPremises: data gateway,<br/>sanctions hits, core banking<br/>reconciliation" --> powerbi
    groups["Account groups<br/>aml-compliance, aml-analysts,<br/>aml-reporting, aml-pipeline,<br/>aml-migration"] -. "schema grants" .-> cloud
    parity -- "SQL warehouse: profiles, bucket digests,<br/>rows of differing buckets only" --> silver
    parity --> gold
```

## Deployment

```mermaid
flowchart LR
    repo["GitHub repository"] --> ci["CI checks: lint, types, unit tests,<br/>bundle schema, assessment,<br/>governance, dbt parse"]
    repo --> infra["CI infra: terraform fmt,<br/>validate, TFLint"]
    ci --> devdeploy["groups, bundle deploy -t dev<br/>platform check, transfer check,<br/>daily job of one date"]
    devdeploy --> free["Databricks Free Edition<br/>(dev)"]
    devdeploy -- "push to main, or on demand" --> proddeploy["bundle deploy -t prod<br/>platform check, transfer check<br/>(OpenID Connect, no secret)"]
    infra --> proddeploy
    proddeploy --> azure
    operator["Operator: terraform plan,<br/>reviewed apply"] --> azure
    subgraph azure["Azure subscription (France Central, prod window)"]
        direction TB
        ws["Azure Databricks workspace<br/>Premium, serverless"]
        adls[("ADLS Gen2<br/>catalog, landing")]
        connector["Access connector<br/>managed identity"]
        wh["SQL warehouse aml-prod<br/>serverless, 1 min auto stop"]
        budget["Budget alert"]
        connector -- "Storage Blob Data Contributor" --> adls
        ws --- wh
        ws -- "storage credential,<br/>external locations" --> connector
    end
```

| Concern | dev | prod |
|---|---|---|
| Workspace | Databricks Free Edition | Azure Databricks Premium, serverless, France Central |
| Storage | Workspace default storage | ADLS Gen2 behind Unity Catalog external locations; `landing.deliveries` is an external volume |
| Catalog | `aml_dev`, created with SQL (`make catalog`) | `aml_prod`, Terraform, bound to the prod workspace only |
| Groups | `make groups` | Terraform, with workspace assignments; operators in `data-platform-admins` |
| CI identity | Personal access token in the `dev` GitHub environment | Service principal through OpenID Connect from the `prod` environment, no stored secret |
| Deployed by | Every pull request and every push to `main` | Push to `main` after dev, or on demand |
| Infrastructure | — | `infra/terraform/prod`, applied by an operator ([ADR 0007](adr/0007-azure-infrastructure-identities-and-cost.md)) |

## The boundary

What crosses, column by column: [`docs/assessment/boundary.md`](assessment/boundary.md).

```mermaid
sequenceDiagram
    participant S as Source lakehouse (Nessie main)
    participant E as lakemig export (on premises)
    participant V as landing.deliveries volume
    E->>S: pin the head commit of main
    E->>S: read the allowed columns of each object, at that commit
    E->>S: read the customers' full names (identity table)
    E->>E: pseudonymize, reduce, order rows
    E->>E: PII gate on every string value
    alt a match without exemption
        E->>E: stop, write a masked report, nothing written
    else clean
        E->>E: write Parquet files, then _manifest.json
    end
    E->>V: upload the data files, check each size
    E->>V: upload _manifest.json last, never overwritten
```

## Bronze ingestion

```mermaid
sequenceDiagram
    participant V as landing.deliveries volume
    participant J as ingest_bronze job
    participant L as landing.delivery_log
    participant B as bronze tables
    J->>L: decided deliveries, key id of the accepted ones
    J->>V: list sealed deliveries, latest export of each date
    J->>V: manifest place, key id, landing contract, size and SHA-256 of each file
    J->>V: read each table with the contract schema, recompute the control totals
    alt any difference
        J->>L: rejected, with every reason (files stay in the volume)
    else all agree
        J->>B: replace the slice of the date in every table
        J->>L: accepted
    end
```

## Transformations and access

```mermaid
flowchart LR
    subgraph bronze_s["bronze (pipeline only)"]
        b_cdc["corebanking_* change logs<br/>change_batch"]
        b_tx["payment_transfer<br/>card_transaction"]
        b_snap["party_member<br/>sanctions_hit"]
    end
    subgraph silver_s["silver (compliance, analysts, reporting)"]
        hist["customer, account, card<br/>histories (type 2)"]
        tx["payment_transfer, card_transaction<br/>fx_rate"]
        party["party_member, party"]
    end
    subgraph gold_s["gold (compliance, reporting)"]
        rules["7 rules (inlined)"]
        alerts["aml_alert_transfer<br/>aml_alert, aml_rule"]
        hits["sanctions_hit"]
    end
    b_cdc --> hist
    b_tx --> tx
    hist --> tx
    b_snap --> party
    b_snap --> hits
    hist --> party
    tx --> rules --> alerts
    hist --> alerts
    party --> alerts
```

The daily run of one business date, the source DAG `aml_daily` task for task ([ADR 0006](adr/0006-orchestration-and-write-audit-publish.md)):

```mermaid
flowchart LR
    ingest["ingest<br/>(job ingest_bronze)"] --> ab["audit_bronze<br/>slices = accepted delivery"]
    ab --> open["open_staging<br/>reset unaudited slices"]
    open --> bs["build_silver<br/>dbt seed + run"] --> as["audit_silver<br/>dbt test"]
    as --> bg["build_gold<br/>dbt run"] --> ag["audit_gold<br/>dbt test"]
    ag --> bo["build_observability<br/>dbt run"] --> ao["audit_observability<br/>dbt test"]
    ao --> pub["publish<br/>REPLACE WHERE / INSERT OVERWRITE,<br/>clustering, observability.publication"]
```

Readers only ever see `silver`, `gold`, `reference` and `observability`; dbt writes only their
`_staging` copies, which no group reads. A failed audit leaves the published layers as they were:
a blocking volume anomaly or a feed that does not reconcile holds the date's publication
([ADR 0009](adr/0009-reliability-security-and-cost.md)).

Pseudonym columns read as null outside `aml-compliance` and `aml-pipeline`; every column relating to a
person is tagged with its classification ([governance.md](assessment/governance.md)). What sqlglot
would have translated on its own: [translation.md](assessment/translation.md).

Decisions: [ADR 0001](adr/0001-replatform-in-waves.md) (strategy),
[ADR 0002](adr/0002-azure-databricks-and-delta.md) (platform, format, environments),
[ADR 0003](adr/0003-hybrid-boundary.md) (boundary, gate, transport),
[ADR 0004](adr/0004-bronze-ingestion.md) (bronze ingestion),
[ADR 0005](adr/0005-transformations-and-unity-catalog-security.md) (transformations, access),
[ADR 0006](adr/0006-orchestration-and-write-audit-publish.md) (orchestration, write-audit-publish),
[ADR 0007](adr/0007-azure-infrastructure-identities-and-cost.md) (Azure infrastructure, identities, cost),
[ADR 0008](adr/0008-migration-acceptance-and-parity.md) (acceptance, parity),
[ADR 0009](adr/0009-reliability-security-and-cost.md) (checks, lineage, access, cost, clustering).

## Cutover

The report the compliance team reads is the source's Power BI project, rendered into `bi/` with one
change of substance: its `Lakehouse` expression reads the SQL warehouse or Trino depending on a
dataset parameter, `ServingPlatform`. The cutover sets it to `Databricks`; a rollback sets it back
to `Trino` while the source keeps running for the 14-day rollback window. The sanctions hits (the
listed names never cross) and the core banking reconciliation stay on premises, read through the
data gateway. Each phase's go/no-go checks are code (`make cutover`), recorded and timed in
[assessment/cutover.md](assessment/cutover.md). Decision: [ADR 0010](adr/0010-cutover-and-rollback.md);
procedure: [cutover.md](cutover.md); operations, key rotation included: [runbook.md](runbook.md).
