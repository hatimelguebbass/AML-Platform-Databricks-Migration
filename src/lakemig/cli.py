"""`lakemig`: assessment of the source platform and the on-premises side of the transfer.

lakemig inventory --source DIR --manifest FILE    collect migration/inventory.json from the live source
lakemig prices                                    snapshot the Azure retail prices the estimate uses
lakemig assess [--check]                          check the migration files, render docs/assessment/
lakemig key                                       generate the pseudonymization key, once, on premises
lakemig export --from DATE [--until DATE]         export business dates from the live source
lakemig deliver --volume PATH                     upload the complete exports to the landing volume
lakemig contract                                  freeze the landing contract from the complete exports
lakemig translate --source-sql DIR --catalog C    measure sqlglot's translation of the ported models
lakemig parity --catalog C --from D [--until D]   compare the live source with the target on business dates
lakemig benchmark --catalog C --label L          measure what the benchmark queries read on the current layout
lakemig operations --catalog C --since D         collect cost, runs, lineage and access from the system tables
lakemig bi --source-model DIR --host H ...        render the report of the cutover into bi/ from the source's
lakemig cutover PHASE --catalog C --from D ...    run and record one phase of the cutover (docs/cutover.md)
lakemig teardown inventory --root R < STATE       keep the identifiers a Terraform state manages, before destroying it
lakemig teardown verify --subscription S ...      look them up after the destroy, sweep, record the window's cost
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger("lakemig")

MIGRATION = Path("migration")
INVENTORY = MIGRATION / "inventory.json"
CLASSIFICATION = MIGRATION / "classification.yaml"
WAVES = MIGRATION / "waves.yaml"
EXPORT_POLICY = MIGRATION / "export.yaml"
EXPORTS = Path("exports")
KEY = Path(os.environ.get("LAKEMIG_PSEUDONYMIZATION_KEY", ".secrets/pseudonymization.key"))
COST_ASSUMPTIONS = MIGRATION / "cost-assumptions.yaml"
PRICES = MIGRATION / "azure-prices.json"
ASSESSMENT_DOCS = Path("docs/assessment")
TRANSFORM = Path("transform")
TRANSLATION = MIGRATION / "translation.json"
HAND_CHANGES = MIGRATION / "translation.yaml"
PARITY_SPEC = MIGRATION / "parity.yaml"
PARITY_REPORT = MIGRATION / "parity.json"
BENCHMARK_SPEC = MIGRATION / "benchmark.yaml"
BENCHMARK_REPORT = MIGRATION / "benchmark.json"
OPERATIONS_REPORT = MIGRATION / "operations.json"
CUTOVER_PLAN = MIGRATION / "cutover.yaml"
CUTOVER_RECORD = MIGRATION / "cutover.json"
TEARDOWN_REPORT = MIGRATION / "teardown.json"
# Identifiers of the destroyed objects: kept on the operator's host, never committed.
TEARDOWN_INVENTORY = Path(".teardown/inventory.json")
BI = Path("bi")


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("pyiceberg").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(prog="lakemig")
    commands = parser.add_subparsers(dest="command", required=True)

    inventory = commands.add_parser("inventory", help="collect the inventory of the running source platform")
    inventory.add_argument("--source", type=Path, required=True, help="checkout of the pinned source commit")
    inventory.add_argument("--manifest", type=Path, required=True, help="manifest.json of `dbt compile`")
    inventory.add_argument("--trino-host", default="localhost")
    inventory.add_argument("--trino-port", type=int, default=8080)
    inventory.add_argument("--trino-user", default="trino")

    commands.add_parser("prices", help="snapshot the Azure retail prices of the cost assumptions")

    assess = commands.add_parser("assess", help="check classification and waves, render docs/assessment/")
    assess.add_argument("--check", action="store_true", help="fail if the rendered documents are not committed")

    key = commands.add_parser("key", help=f"generate the pseudonymization key ({KEY}); never overwrites one")
    key.add_argument("--id", action="store_true", help="print the id of the existing key instead")

    export = commands.add_parser("export", help="export business dates of the live source to the exports directory")
    export.add_argument("--from", dest="first", type=date.fromisoformat, required=True)
    export.add_argument("--until", dest="last", type=date.fromisoformat, help="last business date (default: --from)")
    export.add_argument("--out", type=Path, default=EXPORTS)
    export.add_argument("--nessie", default="http://localhost:19120", help="Nessie server of the source")
    export.add_argument("--branch", default="main", help="published branch of the source lakehouse")
    export.add_argument("--s3-endpoint", default="http://localhost:8333")
    export.add_argument("--source-env", type=Path, required=True, help=".env of the source, for its storage keys")

    deliver = commands.add_parser("deliver", help="upload complete exports to the landing volume (idempotent)")
    deliver.add_argument("--exports", type=Path, default=EXPORTS)
    deliver.add_argument("--volume", required=True, help="/Volumes/<catalog>/landing/deliveries")

    contract = commands.add_parser("contract", help="freeze the landing contract from the complete exports")
    contract.add_argument("--exports", type=Path, default=EXPORTS)

    translate = commands.add_parser("translate", help="measure sqlglot's translation of the ported models")
    translate.add_argument("--source-sql", type=Path, required=True, help="compiled models of the source project")
    translate.add_argument("--catalog", required=True, help="catalog the port was built in")
    translate.add_argument("--warehouse-id", help="SQL warehouse (default: the first one listed)")

    parity = commands.add_parser("parity", help="compare the live source with the target on business dates")
    parity.add_argument("--catalog", required=True, help="catalog of the target")
    parity.add_argument("--from", dest="first", type=date.fromisoformat, required=True)
    parity.add_argument("--until", dest="last", type=date.fromisoformat, help="last business date (default: --from)")
    parity.add_argument("--warehouse-id", help="SQL warehouse (default: the first one listed)")
    parity.add_argument("--nessie", default="http://localhost:19120", help="Nessie server of the source")
    parity.add_argument("--branch", default="main", help="published branch of the source lakehouse")
    parity.add_argument("--s3-endpoint", default="http://localhost:8333")
    parity.add_argument("--source-env", type=Path, required=True, help=".env of the source, for its storage keys")

    benchmark = commands.add_parser("benchmark", help="measure what the benchmark queries read on the current layout")
    benchmark.add_argument("--catalog", required=True, help="catalog of the target")
    benchmark.add_argument("--label", required=True, help="name of the layout measured, e.g. unclustered")
    benchmark.add_argument("--warehouse-id", help="SQL warehouse (default: the first one listed)")

    operations = commands.add_parser("operations", help="collect cost, runs, lineage and access from system tables")
    operations.add_argument("--catalog", required=True, help="catalog of the target")
    operations.add_argument("--since", type=date.fromisoformat, required=True, help="first day of the window")
    operations.add_argument("--manifest", type=Path, required=True, help="manifest.json of `dbt parse`")
    operations.add_argument("--deployer", required=True, help="application id of the deployer service principal")
    operations.add_argument("--subscription", help="Azure subscription to read Cost Management for (az login)")
    operations.add_argument("--warehouse-id", help="SQL warehouse (default: the first one listed)")

    bi = commands.add_parser("bi", help="render the report of the cutover from the source's Power BI project")
    bi.add_argument("--source-model", type=Path, required=True, help="bi/ of the pinned source checkout")
    bi.add_argument("--host", required=True, help="server hostname of the target's SQL warehouse")
    bi.add_argument("--http-path", required=True, help="HTTP path of the target's SQL warehouse")
    bi.add_argument("--catalog", required=True, help="catalog of the target")

    cutover = commands.add_parser("cutover", help="run and record one phase of the cutover")
    cutover.add_argument("phase", choices=("preflight", "freeze", "switch", "rollback"))
    cutover.add_argument("--catalog", required=True, help="catalog of the target")
    cutover.add_argument("--from", dest="first", type=date.fromisoformat, required=True)
    cutover.add_argument("--until", dest="last", type=date.fromisoformat, required=True)
    cutover.add_argument("--tag", required=True, help="Nessie tag of the frozen source, e.g. cutover-prod")
    cutover.add_argument("--nessie", default="http://localhost:19120", help="Nessie server of the source")
    cutover.add_argument("--branch", default="main", help="published branch of the source lakehouse")
    cutover.add_argument("--trino-host", default="localhost")
    cutover.add_argument("--trino-port", type=int, default=8080)
    cutover.add_argument("--trino-user", default="trino")
    cutover.add_argument("--reader-group", default="aml-reporting", help="account group the report reads as")
    cutover.add_argument("--warehouse-id", help="SQL warehouse (default: the first one listed)")

    teardown = commands.add_parser("teardown", help="prove that the Azure window left nothing behind")
    teardown.add_argument("action", choices=("inventory", "verify"))
    teardown.add_argument("--root", help="inventory: Terraform root of the `terraform show -json` on stdin")
    teardown.add_argument("--subscription", help="verify: Azure subscription of the window")
    teardown.add_argument("--account", help="verify: Databricks account id")
    teardown.add_argument("--since", type=date.fromisoformat, help="verify: first day of the window")

    args = parser.parse_args(argv)
    if args.command == "teardown":
        return _teardown(args)
    if args.command == "cutover":
        return _cutover(args)
    if args.command == "bi":
        return _bi(args)
    if args.command == "operations":
        return _operations(args)
    if args.command == "benchmark":
        return _benchmark(args)
    if args.command == "parity":
        return _parity(args)
    if args.command == "translate":
        return _translate(args)
    if args.command == "contract":
        return _contract(args)
    if args.command == "inventory":
        return _inventory(args)
    if args.command == "prices":
        return _prices()
    if args.command == "key":
        return _key(show_id=args.id)
    if args.command == "export":
        return _export(args)
    if args.command == "deliver":
        return _deliver(args)
    return _assess(check_only=args.check)


def _inventory(args: argparse.Namespace) -> int:
    import trino

    from lakemig.inventory.collect import collect
    from lakemig.inventory.dbt_project import load_manifest
    from lakemig.source import SourcePin

    connection = trino.dbapi.connect(host=args.trino_host, port=args.trino_port, user=args.trino_user)

    def query(sql: str) -> list[Any]:
        cursor = connection.cursor()
        cursor.execute(sql)
        return list(cursor.fetchall())

    result = collect(SourcePin.read(), args.source, load_manifest(args.manifest), query)
    result.write(INVENTORY)
    log.info("%d objects written to %s", len(result.objects), INVENTORY)
    return 0


def _prices() -> int:
    import httpx

    from lakemig.cost import prices
    from lakemig.cost.estimate import Assumptions

    assumptions = Assumptions.read(COST_ASSUMPTIONS)
    with httpx.Client(timeout=60) as client:
        fetched = prices.fetch(assumptions.meters, assumptions.regions, client)
    prices.Snapshot(retrieved_on=date.today().isoformat(), prices=tuple(fetched)).write(PRICES)
    log.info("%d prices written to %s", len(fetched), PRICES)
    return 0


def _key(*, show_id: bool) -> int:
    from lakemig.export.pseudonym import Key

    if show_id:
        print(Key.load(KEY).key_id)
        return 0
    key = Key.generate(KEY)
    log.info("key %s written to %s: back it up on premises, it never goes to the cloud", key.key_id, KEY)
    return 0


def _export_plan() -> tuple[Any, Any, list[str]]:
    from lakemig.assessment.classification import Policy, classify
    from lakemig.assessment.waves import Plan
    from lakemig.export import gate, policy
    from lakemig.inventory.model import Inventory

    inventory = Inventory.read(INVENTORY)
    classification = classify(inventory.objects, Policy.read(CLASSIFICATION))
    resolution = policy.resolve(
        policy.read(EXPORT_POLICY), inventory, classification, Plan.read(WAVES), gate.DETECTOR_NAMES
    )
    return resolution.plan, inventory, list(resolution.problems)


def _export(args: argparse.Namespace) -> int:
    import httpx

    from lakemig.export import run
    from lakemig.export.pseudonym import Key
    from lakemig.export.source import IcebergSource, S3Settings
    from lakemig.source import SourcePin

    plan, inventory, problems = _export_plan()
    if problems:
        for problem in problems:
            log.error(problem)
        return 1
    settings = _dotenv(args.source_env)
    s3 = S3Settings(args.s3_endpoint, settings["S3_REGION"], settings["S3_ACCESS_KEY"], settings["S3_SECRET_KEY"])
    with httpx.Client(timeout=30) as client:
        source = IcebergSource.nessie(args.nessie, args.branch, s3, client)
    pin = SourcePin.read()
    context = run.Context(pin.repository, pin.commit, run.policy_digest((CLASSIFICATION, EXPORT_POLICY)))
    key = Key.load(KEY)
    log.info("source %s, key %s", source.ref, key.key_id)
    day, last = args.first, args.last or args.first
    while day <= last:
        try:
            manifest = run.export_date(day, plan, inventory, source, key, args.out, context)
        except run.GateBlockedError as blocked:
            report = args.out / "_blocked" / f"{day.isoformat()}.json"
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(run.blocked_report(blocked), encoding="utf-8")
            log.error("%s\nmasked report: %s", blocked, report)
            return 2
        except (run.SchemaDriftError, run.UndatedCaptureError) as stopped:
            log.error("%s: %s", day, stopped)
            return 1
        log.info("%s: delivery %s, %d rows", day, manifest.delivery_id, sum(t.rows for t in manifest.tables))
        day += timedelta(days=1)
    return 0


def _deliver(args: argparse.Namespace) -> int:
    from databricks.sdk import WorkspaceClient

    from lakemig.export.delivery import complete
    from lakemig.transfer.landing import VolumeLanding, deliver

    landing = VolumeLanding(WorkspaceClient())
    deliveries = complete(args.exports)
    for local in deliveries:
        outcome = deliver(local, landing, args.volume)
        log.info("%s: %s", local.name, outcome.value)
    log.info("%d deliveries checked against %s", len(deliveries), args.volume)
    return 0


def _contract(args: argparse.Namespace) -> int:
    from lakemig.export.delivery import complete
    from lakemig.ingestion.contract import CONTRACT, Contract
    from lakemig.manifest import MANIFEST, Manifest

    deliveries = complete(args.exports)
    manifests = [Manifest.from_json((d / MANIFEST).read_text(encoding="utf-8")) for d in deliveries]
    Contract.from_manifests(manifests).write()
    log.info("landing contract of %d deliveries written to %s", len(manifests), CONTRACT)
    return 0


def _ported_models() -> list[Any]:
    """Models of the transform project rebuilt from a source model, in dependency order of the waves."""
    from lakemig.assessment.governance import read_models
    from lakemig.assessment.waves import Fate, Plan
    from lakemig.inventory.model import Inventory

    objects = Inventory.read(INVENTORY).by_relation()
    rebuilt = [
        relation
        for wave in Plan.read(WAVES).waves
        for relation, fate in wave.objects.items()
        if fate is Fate.REBUILD and objects[relation].materialization not in (None, "ephemeral")
    ]
    models = {m.relation: m for m in read_models(TRANSFORM)}
    return [models[r] for r in rebuilt if r in models]


def _translate(args: argparse.Namespace) -> int:
    from importlib.metadata import version

    from databricks.sdk import WorkspaceClient

    from lakemig.assessment.governance import PSEUDONYM_MASK
    from lakemig.source import SourcePin
    from lakemig.translation.report import FORMAT_VERSION, Report, measure
    from lakemig.translation.warehouse import engine_version, runner

    client = WorkspaceClient()
    warehouse_id = args.warehouse_id or next(iter(client.warehouses.list())).id
    if not warehouse_id:
        log.error("no SQL warehouse in the workspace")
        return 1
    execute = runner(client, warehouse_id, args.catalog)
    # A reader outside the privileged groups sees masked pseudonyms as null in the port's tables only.
    rows, _ = execute(f"select {args.catalog}.{PSEUDONYM_MASK}('probe')")
    if not rows or rows[0][0] != "probe":
        log.error("this identity reads masked values; measure as a member of aml-pipeline or aml-compliance")
        return 1
    results = []
    for model in _ported_models():
        compiled = next(args.source_sql.rglob(f"{model.name}.sql"), None)
        if compiled is None:
            log.error("%s: no compiled source model under %s", model.relation, args.source_sql)
            return 1
        result = measure(
            model.relation,
            compiled.read_text(encoding="utf-8"),
            f"{args.catalog}.{model.relation}",
            [c.name for c in model.columns],
            execute,
        )
        log.info("%s: %s", model.relation, result.outcome.value)
        results.append(result)
    Report(
        format_version=FORMAT_VERSION,
        sqlglot_version=version("sqlglot"),
        source_commit=SourcePin.read().commit,
        engine_version=engine_version(execute),
        measured_on=date.today().isoformat(),
        models=tuple(results),
    ).write(TRANSLATION)
    log.info("%d models measured, written to %s", len(results), TRANSLATION)
    return 0


def _parity(args: argparse.Namespace) -> int:
    from datetime import UTC, datetime

    import httpx
    from databricks.sdk import WorkspaceClient

    from lakemig.assessment.governance import PSEUDONYM_MASK
    from lakemig.export.pseudonym import Key
    from lakemig.export.source import IcebergSource, S3Settings
    from lakemig.parity import run
    from lakemig.parity.report import FORMAT_VERSION, Report
    from lakemig.parity.spec import Spec
    from lakemig.source import SourcePin
    from lakemig.translation.warehouse import engine_version, runner, strict

    spec = Spec.read(PARITY_SPEC)
    first, last = args.first, args.last or args.first
    dates = [(first + timedelta(days=n)).isoformat() for n in range((last - first).days + 1)]
    client = WorkspaceClient()
    warehouse_id = args.warehouse_id or next(iter(client.warehouses.list())).id
    if not warehouse_id:
        log.error("no SQL warehouse in the workspace")
        return 1
    execute = runner(client, warehouse_id, args.catalog)
    query = strict(execute)
    # Masked pseudonyms read as null outside the privileged groups: every keyed row would differ.
    if query(f"select {args.catalog}.{PSEUDONYM_MASK}('probe')") != [["probe"]]:
        log.error(
            "this identity reads masked values; compare as a member of aml-pipeline, aml-compliance or aml-migration"
        )
        return 1
    settings = _dotenv(args.source_env)
    s3 = S3Settings(args.s3_endpoint, settings["S3_REGION"], settings["S3_ACCESS_KEY"], settings["S3_SECRET_KEY"])
    with httpx.Client(timeout=30) as http:
        source = IcebergSource.nessie(args.nessie, args.branch, s3, http)
    key = Key.load(KEY)
    log.info("source %s, key %s, target %s", source.ref, key.key_id, args.catalog)
    try:
        published = run.publications(query, args.catalog, dates)
        tables = run.run(spec, source, query, key, args.catalog, dates)
    except run.ParitySetupError as failure:
        log.error("%s", failure)
        return 1
    pin = SourcePin.read()
    report = Report(
        format_version=FORMAT_VERSION,
        measured_at=datetime.now(UTC).isoformat(timespec="seconds"),
        spec_digest=spec.digest,
        source_repository=pin.repository,
        source_commit=pin.commit,
        lakehouse_ref=source.ref,
        target_catalog=args.catalog,
        engine_version=engine_version(execute),
        business_dates=(dates[0], dates[-1]),
        publications=published,
        tables=tables,
    )
    report.write(PARITY_REPORT)
    differing = [t.relation for t in tables if not t.equal]
    if differing:
        log.error("the target differs from the source in %s; see %s", ", ".join(differing), PARITY_REPORT)
        return 1
    log.info("%d tables equal on %d business dates, written to %s", len(tables), len(dates), PARITY_REPORT)
    return 0


def _benchmark(args: argparse.Namespace) -> int:
    from datetime import UTC, datetime

    from databricks.sdk import WorkspaceClient

    from lakemig.operations import benchmark
    from lakemig.translation.warehouse import engine_version, runner

    spec = benchmark.Spec.read(BENCHMARK_SPEC)
    client = WorkspaceClient()
    warehouse_id = args.warehouse_id or next(iter(client.warehouses.list())).id
    if not warehouse_id:
        log.error("no SQL warehouse in the workspace")
        return 1
    layouts = tuple(
        benchmark.layout(benchmark.describe_detail(client, warehouse_id, f"{args.catalog}.{t}"), t) for t in spec.tables
    )
    measurements = benchmark.measure(
        spec,
        args.catalog,
        benchmark.statement_runner(client, warehouse_id, args.catalog),
        benchmark.history_metrics(client, warehouse_id),
    )
    run = benchmark.Run(
        label=args.label,
        measured_at=datetime.now(UTC).isoformat(timespec="seconds"),
        catalog=args.catalog,
        engine_version=engine_version(runner(client, warehouse_id, args.catalog)),
        layouts=layouts,
        measurements=tuple(measurements),
    )
    benchmark.Report.read(BENCHMARK_REPORT).with_run(run).write(BENCHMARK_REPORT)
    for m in measurements:
        log.info(
            "%s: %d files read, %d pruned, %d bytes, %d ms",
            m.query,
            m.files_read,
            m.files_pruned,
            m.bytes_read,
            m.duration_ms,
        )
    return 0


def _operations(args: argparse.Namespace) -> int:
    import json
    import re
    import subprocess
    from datetime import UTC, datetime

    import httpx
    from databricks.sdk import WorkspaceClient

    from lakemig.operations import evidence
    from lakemig.translation.warehouse import runner, strict

    if not re.fullmatch(r"[a-z_][a-z0-9_]*", args.catalog):
        log.error("not a catalog name: %s", args.catalog)
        return 1
    client = WorkspaceClient()
    warehouse_id = args.warehouse_id or next(iter(client.warehouses.list())).id
    if not warehouse_id:
        log.error("no SQL warehouse in the workspace")
        return 1
    query = strict(runner(client, warehouse_id, args.catalog))
    names = {"catalog": args.catalog, "workspace": str(client.get_workspace_id()), "since": args.since.isoformat()}
    usage = evidence.usage(query(evidence.COST_SQL.format(**names)))
    until = datetime.now(UTC).date()
    azure: tuple[evidence.ServiceCost, ...] = ()
    if args.subscription:
        # The operator's Azure CLI login, as for Terraform: no credential of its own.
        token = subprocess.run(
            ["az", "account", "get-access-token", "--resource", "https://management.azure.com", "--output", "json"],  # noqa: S607
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        with httpx.Client(timeout=60) as http:
            azure = evidence.azure_costs(args.subscription, args.since, until, json.loads(token)["accessToken"], http)
    declared = evidence.declared_lineage(json.loads(args.manifest.read_text(encoding="utf-8")), args.catalog)
    counts, changes = evidence.access(query(evidence.AUDIT_SQL.format(**names)), args.deployer)
    report = evidence.Report(
        format_version=evidence.FORMAT_VERSION,
        measured_at=datetime.now(UTC).isoformat(timespec="seconds"),
        catalog=args.catalog,
        window=(args.since.isoformat(), until.isoformat()),
        usage=usage,
        jobs_cost=evidence.jobs_cost(query(evidence.JOB_COST_SQL.format(**names))),
        azure=azure,
        runs=evidence.runs(query(evidence.RUNS_SQL.format(**names))),
        lineage=evidence.lineage(declared, query(evidence.LINEAGE_SQL.format(**names))),
        access=counts,
        permission_changes=changes,
    )
    report.write(OPERATIONS_REPORT)
    log.info(
        "%.2f USD at list price over %d days; lineage %s; written to %s",
        sum(u.list_cost for u in usage),
        report.days,
        "complete" if report.lineage.complete else f"{len(report.lineage.missing)} edges missing",
        OPERATIONS_REPORT,
    )
    return 0 if report.lineage.complete else 1


def _teardown(args: argparse.Namespace) -> int:
    import json
    import subprocess
    from datetime import UTC, datetime

    import httpx

    from lakemig.operations import teardown

    if args.action == "inventory":
        if not args.root:
            log.error("--root is required")
            return 2
        fresh = teardown.inventory(args.root, json.load(sys.stdin))
        kept = teardown.merge(teardown.read_inventory(TEARDOWN_INVENTORY), args.root, fresh)
        teardown.write_inventory(TEARDOWN_INVENTORY, kept)
        log.info("%d objects of %s kept in %s", len(fresh), args.root, TEARDOWN_INVENTORY)
        return 0

    if not (args.subscription and args.account and args.since):
        log.error("--subscription, --account and --since are required")
        return 2
    items = teardown.read_inventory(TEARDOWN_INVENTORY)
    if not items:
        log.error("%s is empty: run `make infra-destroy-plan` before destroying", TEARDOWN_INVENTORY)
        return 1

    def token(resource: str) -> str:
        # The operator's Azure CLI login, as for Terraform: no credential of its own.
        out = subprocess.run(  # noqa: S603 - resource is one of three constants
            ["az", "account", "get-access-token", "--resource", resource, "--output", "json"],  # noqa: S607
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        return str(json.loads(out)["accessToken"])

    tokens = teardown.Tokens(
        arm=token("https://management.azure.com"),
        graph=token("https://graph.microsoft.com"),
        databricks=token(teardown.DATABRICKS_RESOURCE),
    )
    until = datetime.now(UTC).date()
    with httpx.Client(timeout=60) as http:
        lookups = teardown.Lookups(http, tokens, args.account)
        currency, days = lookups.daily_cost(args.subscription, args.since, until)
        report = teardown.Report(
            format_version=teardown.FORMAT_VERSION,
            measured_at=datetime.now(UTC).isoformat(timespec="seconds"),
            window=(args.since.isoformat(), until.isoformat()),
            managed=teardown.verify(items, lookups),
            resource_groups=tuple(lookups.resource_groups(args.subscription)),
            resources=tuple(lookups.resources(args.subscription)),
            metastores=tuple(lookups.metastores()),
            currency=currency,
            daily_cost=days,
        )
    report.write(TEARDOWN_REPORT)
    log.info(
        "%s; %.2f %s charged over the window; written to %s",
        "nothing left" if report.complete else f"{report.remaining} managed objects left",
        report.total_cost,
        report.currency,
        TEARDOWN_REPORT,
    )
    return _assess(check_only=False) or (0 if report.complete else 1)


def _bi(args: argparse.Namespace) -> int:
    from lakemig.cutover import semantic_model

    plan = semantic_model.Plan.read(CUTOVER_PLAN)
    target = semantic_model.Target(args.host, args.http_path, args.catalog)
    removed = semantic_model.render(args.source_model, BI, plan, target)
    log.info("rendered %s from %s; visuals removed: %s", BI, args.source_model, ", ".join(removed) or "none")
    return 0


def _cutover(args: argparse.Namespace) -> int:
    import httpx
    import trino
    from databricks.sdk import WorkspaceClient

    from lakemig.cutover import runbook, semantic_model
    from lakemig.export.pseudonym import Key
    from lakemig.parity import run as parity_run
    from lakemig.parity.report import Report as ParityReport
    from lakemig.translation.warehouse import runner, strict

    catalog = args.catalog
    plan = semantic_model.Plan.read(CUTOVER_PLAN)
    tables = semantic_model.imported_tables(BI / semantic_model.MODEL)
    dates = [(args.first + timedelta(days=n)).isoformat() for n in range((args.last - args.first).days + 1)]
    record = (
        runbook.Record.read(CUTOVER_RECORD)
        if CUTOVER_RECORD.exists()
        else runbook.Record(runbook.FORMAT_VERSION, catalog, ())
    )
    client = WorkspaceClient()
    warehouse_id = args.warehouse_id or next(iter(client.warehouses.list())).id
    if not warehouse_id:
        log.error("no SQL warehouse in the workspace")
        return 1
    query = strict(runner(client, warehouse_id, catalog))
    http = httpx.Client(timeout=30)
    connection = trino.dbapi.connect(host=args.trino_host, port=args.trino_port, user=args.trino_user)

    def source_query(statement: str) -> list[list[Any]]:
        cursor = connection.cursor()
        cursor.execute(statement)
        return [list(r) for r in cursor.fetchall()]

    def reference(name: str) -> str | None:
        response = http.get(f"{args.nessie}/api/v2/trees/{name}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return str(response.json()["reference"]["hash"])

    def preflight(reference_hash: str | None) -> list[Any]:
        published: dict[str, str] = {}

        def key_custody() -> runbook.Check:
            return runbook.key_custody(query, catalog, Key.load(KEY).key_id)

        def deliveries() -> runbook.Check:
            return runbook.deliveries(query, catalog, dates)

        def publications() -> runbook.Check:
            published.update(parity_run.publications(query, catalog, dates))
            return runbook.publications(published, dates)

        def parity() -> runbook.Check:
            if not PARITY_REPORT.exists():
                return runbook.Check("parity", False, f"no {PARITY_REPORT}: make parity")
            return runbook.parity(ParityReport.read(PARITY_REPORT), dates, published, reference_hash)

        def report_columns() -> runbook.Check:
            return runbook.report_columns(query, catalog, tables, plan)

        def reader_grants() -> runbook.Check:
            return runbook.reader_grants(query, catalog, tables, plan, args.reader_group)

        def no_active_runs() -> runbook.Check:
            active = [f"{r.run_name} ({r.run_id})" for r in client.jobs.list_runs(active_only=True)]
            return runbook.no_active_runs(active)

        return [key_custody, deliveries, publications, parity, report_columns, reader_grants, no_active_runs]

    # Relations are `schema.table` words parsed from the model (semantic_model._NAVIGATION).
    def counts(side: str, relations: list[str]) -> dict[str, int]:
        if side == "target":
            return {r: int(str(query(f"select count(*) from {catalog}.{r}")[0][0])) for r in relations}  # noqa: S608
        return {r: int(source_query(f"select count(*) from lakehouse.{r}")[0][0]) for r in relations}  # noqa: S608

    served = [t.relation for t in tables if plan.serving(t.relation) == "target"]
    on_premises = [t.relation for t in tables if plan.serving(t.relation) == "on_premises"]
    facts: dict[str, Any] = {"tag": args.tag, "business_dates": f"{dates[0]}..{dates[-1]}"}
    steps: list[Any]
    if args.phase == "preflight":
        steps = preflight(reference(args.tag))
    elif args.phase == "freeze":
        head = reference(args.branch)
        tagged = reference(args.tag)

        def freeze() -> runbook.Check:
            if tagged is None:
                response = http.post(
                    f"{args.nessie}/api/v2/trees",
                    params={"name": args.tag, "type": "TAG"},
                    json={"type": "BRANCH", "name": args.branch, "hash": head},
                )
                response.raise_for_status()
                return runbook.Check("freeze", True, f"{args.tag} created at {args.branch}@{head}")
            return runbook.Check("freeze", tagged == head, f"{args.tag} already at {tagged}, {args.branch} at {head}")

        facts["reference"] = f"{args.branch}@{head}"
        steps = [freeze]
    elif args.phase == "switch":
        tagged = reference(args.tag)

        def source_frozen() -> runbook.Check:
            return runbook.source_frozen(args.tag, tagged, reference(args.branch) or "", args.branch)

        def report_rows() -> runbook.Check:
            return runbook.row_counts(counts("target", served), counts("source", served))

        def on_premises_tables() -> runbook.Check:
            return runbook.readable("on_premises_tables", counts("source", on_premises))

        steps = [source_frozen, *preflight(tagged), report_rows, on_premises_tables]
    else:
        frozen = record.frozen()
        if frozen is None:
            log.error("no freeze recorded in %s: nothing to roll back", CUTOVER_RECORD)
            return 1

        def source_tables() -> runbook.Check:
            return runbook.readable("source_tables", counts("source", served + on_premises))

        def report_rows() -> runbook.Check:
            return runbook.row_counts(counts("target", served), counts("source", served))

        def source_current() -> runbook.Check:
            target_dates = parity_run.publications(query, catalog, dates).keys()
            source_dates = {
                str(d)
                for (d,) in source_query(
                    "select distinct cast(business_date as varchar) from lakehouse.observability.volume_check"
                )
            }
            return runbook.catch_up(target_dates, source_dates, dates[-1])

        steps = [source_tables, report_rows, source_current]

    event = runbook.timed(args.phase, steps, facts)
    record = record.append(event)
    record.write(CUTOVER_RECORD)
    for check in event.checks:
        (log.info if check.passed else log.error)("%-20s %s", check.name, check.detail)
    log.info(
        "%s %s in %.1f s; the report reads %s",
        args.phase,
        "passed" if event.passed else "FAILED",
        event.seconds,
        record.serving(),
    )
    return _assess(check_only=False) or (0 if event.passed else 1)


def _operations_md(assumptions: Any, snapshot: Any) -> str:
    from lakemig.operations import evidence

    if not OPERATIONS_REPORT.exists():
        return "# Operating evidence\n\nNot measured yet: `make operations`.\n"
    measured = evidence.Report.read(OPERATIONS_REPORT)
    # The region the platform was deployed to (ADR 0007), which the estimate also priced.
    region = "francecentral"
    return evidence.operations_md(
        measured, evidence.against_estimate(measured, assumptions, snapshot.prices, region), region
    )


def _teardown_md() -> str:
    from lakemig.operations import teardown

    if not TEARDOWN_REPORT.exists():
        return "# Teardown of the Azure window\n\nNot measured yet: `make teardown`.\n"
    return teardown.teardown_md(teardown.Report.read(TEARDOWN_REPORT))


def _contract_problems(export_plan: Any) -> list[str]:
    """The landing contract must carry exactly the columns the export policy lets cross."""
    from lakemig.ingestion.contract import CONTRACT, Contract

    contract = Contract.read()
    problems = contract.problems()
    allowed = {t.name: t.output_columns() for t in export_plan.tables}
    for name in sorted(set(allowed) | set(contract.tables)):
        frozen = tuple(c.name for c in contract.tables.get(name, ()))
        if frozen != allowed.get(name, ()):
            problems.append(
                f"{CONTRACT.name}: {name} has columns {frozen}, the export policy lets {allowed.get(name, ())} cross;"
                " export again and run `lakemig contract`"
            )
    return problems


def _dotenv(path: Path) -> dict[str, str]:
    pairs = (line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines() if "=" in line)
    return {k.strip(): v.strip().strip("'\"") for k, v in pairs if not k.lstrip().startswith("#")}


def _assess(*, check_only: bool) -> int:
    from lakemig.assessment import report
    from lakemig.assessment.classification import Policy, classify
    from lakemig.assessment.governance import check as governance_check
    from lakemig.assessment.governance import classify_models, policy_md, read_models
    from lakemig.assessment.waves import Plan, check
    from lakemig.cost import prices
    from lakemig.cost.estimate import Assumptions, estimate
    from lakemig.cutover import runbook as cutover_runbook
    from lakemig.cutover import semantic_model
    from lakemig.export import gate
    from lakemig.export.policy import Treatment
    from lakemig.inventory.model import Inventory
    from lakemig.operations import benchmark
    from lakemig.parity.report import Report as ParityReport
    from lakemig.parity.report import parity_md
    from lakemig.parity.spec import Spec as ParitySpec
    from lakemig.source import SourcePin
    from lakemig.translation.report import HandChanges, translation_md
    from lakemig.translation.report import Report as TranslationReport

    inventory = Inventory.read(INVENTORY)
    problems = []
    pin = SourcePin.read()
    if inventory.source_commit != pin.commit:
        problems.append(f"{INVENTORY} was collected at {inventory.source_commit}, the pin is {pin.commit}")
    classification = classify(inventory.objects, Policy.read(CLASSIFICATION))
    problems += classification.problems()
    plan = Plan.read(WAVES)
    problems += check(plan, inventory, classification)
    export_plan, _, export_problems = _export_plan()
    problems += export_problems
    problems += _contract_problems(export_plan)
    models = read_models(TRANSFORM)
    model_classes = classify_models(models, Policy.read(CLASSIFICATION))
    pseudonymized = {
        rule.output
        for table in export_plan.tables
        for rule in table.crossing()
        if rule.treatment is Treatment.PSEUDONYMIZE
    }
    problems += governance_check(models, model_classes, pseudonymized)
    hand_changes = HandChanges.read(HAND_CHANGES)
    ported = [m.relation for m in _ported_models()]
    problems += hand_changes.problems(ported)
    translation = TranslationReport.read(TRANSLATION)
    measured = [m.relation for m in translation.models]
    if measured != ported:
        problems.append(f"{TRANSLATION} measured {measured}, the ported models are {ported}: run `make translation`")
    # Every table the transformations publish is compared with the source, and nothing else, unless
    # its model states why not.
    parity_spec = ParitySpec.read(PARITY_SPEC)
    published = sorted(m.relation for m in models if not m.parity_exemption)
    exempted = {m.relation: m.parity_exemption for m in models if m.parity_exemption}
    specified = sorted(t.relation for t in parity_spec.tables)
    if specified != published:
        problems.append(f"{PARITY_SPEC} compares {specified}, the transformations publish {published}")
    parity = ParityReport.read(PARITY_REPORT) if PARITY_REPORT.exists() else None
    if parity is not None:
        problems += parity.problems(parity_spec)
    # The cutover's report reads only what the transformations publish, unmasked.
    cutover_plan = semantic_model.Plan.read(CUTOVER_PLAN)
    report_tables = semantic_model.imported_tables(BI / semantic_model.MODEL)
    published_columns = {m.relation: {c.name: (c.data_type, c.mask) for c in m.columns} for m in models}
    problems += [
        f"{BI}: {p}" for p in cutover_runbook.contract_problems(report_tables, cutover_plan, published_columns)
    ]
    if problems:
        for problem in problems:
            log.error(problem)
        return 1

    assumptions = Assumptions.read(COST_ASSUMPTIONS)
    snapshot = prices.Snapshot.read(PRICES)
    migrated = report.migrated_bytes(plan, inventory)
    documents = {
        "inventory.md": report.inventory_md(inventory),
        "classification.md": report.classification_md(inventory, classification),
        "waves.md": report.waves_md(plan, inventory, classification),
        "boundary.md": report.boundary_md(
            export_plan,
            [(d.name, d.description) for d in gate.DETECTORS]
            + [
                (
                    gate.PERSON_NAME,
                    "A full name of a customer, as whole words of a value,"
                    " after normalization (case, accents, punctuation)",
                )
            ],
        ),
        "governance.md": policy_md(models, model_classes),
        "translation.md": translation_md(translation, hand_changes, inventory.source_repository),
        "parity.md": parity_md(parity, parity_spec, exempted)
        if parity
        else "# Parity of the parallel run\n\nNot measured yet: `make parity`.\n",
        "clustering.md": benchmark.benchmark_md(
            benchmark.Report.read(BENCHMARK_REPORT), benchmark.Spec.read(BENCHMARK_SPEC)
        ),
        "operations.md": _operations_md(assumptions, snapshot),
        "teardown.md": _teardown_md(),
        "cutover.md": cutover_runbook.cutover_md(
            cutover_runbook.Record.read(CUTOVER_RECORD) if CUTOVER_RECORD.exists() else None,
            cutover_plan,
            report_tables,
        ),
        "cost-estimate.md": report.cost_md(
            estimate(assumptions, snapshot.prices, migrated), assumptions, migrated, snapshot.retrieved_on
        ),
    }
    stale = []
    for name, content in documents.items():
        path = ASSESSMENT_DOCS / name
        if check_only:
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                stale.append(str(path))
        else:
            path.write_text(content, encoding="utf-8")
    if stale:
        log.error("not up to date, run `make assessment` and commit: %s", ", ".join(stale))
        return 1
    log.info(
        "classification and waves consistent with the inventory; %d documents %s",
        len(documents),
        "up to date" if check_only else "rendered",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
