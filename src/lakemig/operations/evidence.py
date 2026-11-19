"""Operating evidence of the cloud platform, read from the Unity Catalog system tables (ADR 0009).

`lakemig operations` collects what the platform did over the Azure window into
`migration/operations.json`; `lakemig assess` renders it with the cost estimate of step 1 into
`docs/assessment/operations.md`:

- cost: billed usage at list price (system.billing), per product and per job, against the estimate,
  and what Azure Cost Management charged the subscription;
- runs: every job's runs, outcomes and durations (system.lakeflow);
- lineage: the table lineage Unity Catalog recorded, against the lineage the code declares (the dbt
  manifest, built in the staging schemas, then each staging table published to its layer);
- access: who did what on the catalog (system.access.audit), and every change of permissions.

Identities are reported by role, never by name: the report is committed.
"""

from __future__ import annotations

import json
import statistics
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from lakemig.cost.estimate import SCENARIOS, Assumptions, Range
from lakemig.cost.prices import UnitPrice

FORMAT_VERSION = 1
STAGING_SUFFIX = "_staging"
# Schemas the transformations build in staging and publish (ADR 0006).
PUBLISHED_LAYERS = ("reference", "silver", "gold", "observability")

Query = Callable[[str], Sequence[Sequence[Any]]]


# --- Queries. `{catalog}`, `{workspace}` and `{since}` are identifiers and a date the CLI validated.

COST_SQL = """
SELECT u.billing_origin_product, u.sku_name, u.usage_unit, sum(u.usage_quantity),
       sum(u.usage_quantity * p.pricing.default), min(u.usage_date), max(u.usage_date)
FROM system.billing.usage u
JOIN system.billing.list_prices p
  ON p.sku_name = u.sku_name AND p.cloud = u.cloud AND p.usage_unit = u.usage_unit
 AND u.usage_end_time > p.price_start_time AND (p.price_end_time IS NULL OR u.usage_end_time <= p.price_end_time)
WHERE u.workspace_id = '{workspace}' AND u.usage_date >= DATE'{since}'
GROUP BY ALL ORDER BY 5 DESC
"""

JOB_COST_SQL = """
SELECT coalesce(j.name, u.usage_metadata.job_id), sum(u.usage_quantity), sum(u.usage_quantity * p.pricing.default)
FROM system.billing.usage u
JOIN system.billing.list_prices p
  ON p.sku_name = u.sku_name AND p.cloud = u.cloud AND p.usage_unit = u.usage_unit
 AND u.usage_end_time > p.price_start_time AND (p.price_end_time IS NULL OR u.usage_end_time <= p.price_end_time)
LEFT JOIN (SELECT job_id, max_by(name, change_time) AS name FROM system.lakeflow.jobs GROUP BY job_id) j
  ON j.job_id = u.usage_metadata.job_id
WHERE u.workspace_id = '{workspace}' AND u.usage_date >= DATE'{since}' AND u.usage_metadata.job_id IS NOT NULL
GROUP BY ALL ORDER BY 3 DESC
"""

RUNS_SQL = """
SELECT j.name, t.run_id, max_by(t.result_state, t.period_end_time),
       coalesce(first(t.job_parameters['business_date']['value'], true), ''),
       unix_timestamp(max(t.period_end_time)) - unix_timestamp(min(t.period_start_time))
FROM system.lakeflow.job_run_timeline t
JOIN (SELECT job_id, max_by(name, change_time) AS name FROM system.lakeflow.jobs GROUP BY job_id) j USING (job_id)
WHERE t.workspace_id = '{workspace}' AND t.period_start_time >= TIMESTAMP'{since}'
GROUP BY ALL
"""

LINEAGE_SQL = """
SELECT DISTINCT source_table_full_name, target_table_full_name
FROM system.access.table_lineage
WHERE workspace_id = '{workspace}' AND target_table_catalog = '{catalog}'
  AND source_table_full_name IS NOT NULL AND event_date >= DATE'{since}'
"""

# Unity Catalog names the securable in different request parameters depending on the action.
AUDIT_SQL = """
WITH events AS (
  SELECT event_time, user_identity.email AS actor, action_name, response.status_code AS status,
         coalesce(request_params['full_name_arg'], request_params['securable_full_name'],
                  request_params['name'], request_params['catalog_name']) AS securable,
         request_params['changes'] AS changes
  FROM system.access.audit
  WHERE workspace_id = '{workspace}' AND service_name = 'unityCatalog' AND event_date >= DATE'{since}'
)
SELECT event_time, actor, action_name, status, securable, changes
FROM events
WHERE securable = '{catalog}' OR securable LIKE '{catalog}.%'
"""


# --- Report.


@dataclass(frozen=True)
class Usage:
    product: str
    sku: str
    unit: str
    quantity: float
    list_cost: float
    first_day: str
    last_day: str


@dataclass(frozen=True)
class JobCost:
    job: str
    dbus: float
    list_cost: float


@dataclass(frozen=True)
class ServiceCost:
    service: str
    cost: float
    currency: str


@dataclass(frozen=True)
class JobRuns:
    job: str
    scope: str  # "one date", "every date" or "" for jobs without a business date
    runs: int
    succeeded: int
    failed: int
    median_seconds: int
    max_seconds: int


@dataclass(frozen=True)
class Lineage:
    expected: int
    observed: int
    missing: tuple[str, ...]
    undeclared: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.missing


@dataclass(frozen=True)
class AccessCount:
    actor: str
    kind: str
    events: int
    denied: int


@dataclass(frozen=True)
class PermissionChange:
    at: str
    actor: str
    securable: str
    changes: str


@dataclass(frozen=True)
class Report:
    format_version: int
    measured_at: str
    catalog: str
    window: tuple[str, str]
    usage: tuple[Usage, ...]
    jobs_cost: tuple[JobCost, ...]
    azure: tuple[ServiceCost, ...]
    runs: tuple[JobRuns, ...]
    lineage: Lineage
    access: tuple[AccessCount, ...]
    permission_changes: tuple[PermissionChange, ...] = field(default_factory=tuple)

    @property
    def days(self) -> int:
        first, last = (date.fromisoformat(d) for d in self.window)
        return (last - first).days + 1

    def write(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")

    @classmethod
    def read(cls, path: Path) -> Report:
        raw = json.loads(path.read_text(encoding="utf-8"))
        lineage = raw["lineage"]
        return cls(
            format_version=raw["format_version"],
            measured_at=raw["measured_at"],
            catalog=raw["catalog"],
            window=(raw["window"][0], raw["window"][1]),
            usage=tuple(Usage(**u) for u in raw["usage"]),
            jobs_cost=tuple(JobCost(**j) for j in raw["jobs_cost"]),
            azure=tuple(ServiceCost(**s) for s in raw["azure"]),
            runs=tuple(JobRuns(**r) for r in raw["runs"]),
            lineage=Lineage(
                lineage["expected"], lineage["observed"], tuple(lineage["missing"]), tuple(lineage["undeclared"])
            ),
            access=tuple(AccessCount(**a) for a in raw["access"]),
            permission_changes=tuple(PermissionChange(**p) for p in raw["permission_changes"]),
        )


# --- Collection, from rows of the queries above.


def usage(rows: Iterable[Sequence[Any]]) -> tuple[Usage, ...]:
    return tuple(
        Usage(str(r[0]), str(r[1]), str(r[2]), float(r[3]), round(float(r[4]), 6), str(r[5]), str(r[6])) for r in rows
    )


def jobs_cost(rows: Iterable[Sequence[Any]]) -> tuple[JobCost, ...]:
    return tuple(JobCost(str(r[0]), round(float(r[1]), 6), round(float(r[2]), 6)) for r in rows)


def runs(rows: Iterable[Sequence[Any]]) -> tuple[JobRuns, ...]:
    """Runs per job and scope: one business date and every date are different workloads."""
    groups: dict[tuple[str, str], list[tuple[str, int]]] = {}
    for job, _run_id, state, business_date, seconds in rows:
        scope = "" if not business_date else "every date" if business_date == "all" else "one date"
        groups.setdefault((str(job), scope), []).append((str(state), int(seconds or 0)))
    result = []
    for (job, scope), outcomes in sorted(groups.items()):
        succeeded = [s for state, s in outcomes if state == "SUCCEEDED"]
        result.append(
            JobRuns(
                job=job,
                scope=scope,
                runs=len(outcomes),
                succeeded=len(succeeded),
                failed=sum(1 for state, _ in outcomes if state in ("FAILED", "ERROR", "TIMED_OUT")),
                # Durations of successful runs only: a failed run stops early and says nothing of the workload.
                median_seconds=int(statistics.median(succeeded)) if succeeded else 0,
                max_seconds=max(succeeded, default=0),
            )
        )
    return tuple(result)


def declared_lineage(manifest: Mapping[str, Any], catalog: str) -> set[tuple[str, str]]:
    """Table-to-table edges the code declares: dbt's dependencies between the staging tables it
    builds (through ephemeral models, which are inlined), then each staging table to its published copy."""
    nodes = {**manifest["nodes"], **manifest["sources"]}

    def relation(node: Mapping[str, Any]) -> str:
        return f"{catalog}.{node['schema']}.{node.get('identifier') or node.get('alias') or node['name']}"

    def tables_read(unique_id: str) -> set[str]:
        node = nodes[unique_id]
        if node["resource_type"] == "model" and node["config"]["materialized"] == "ephemeral":
            return {t for parent in node["depends_on"].get("nodes", []) for t in tables_read(parent)}
        return {relation(node)}

    edges: set[tuple[str, str]] = set()
    for node in manifest["nodes"].values():
        if node["resource_type"] not in ("model", "seed") or node["config"]["materialized"] == "ephemeral":
            continue
        target = relation(node)
        for parent in node["depends_on"].get("nodes", []):
            edges |= {(source, target) for source in tables_read(parent)}
        schema = node["schema"]
        if schema.endswith(STAGING_SUFFIX) and schema.removesuffix(STAGING_SUFFIX) in PUBLISHED_LAYERS:
            edges.add((target, f"{catalog}.{schema.removesuffix(STAGING_SUFFIX)}.{node.get('alias') or node['name']}"))
    return edges


def lineage(declared: set[tuple[str, str]], observed: Iterable[Sequence[Any]]) -> Lineage:
    seen = {(str(s), str(t)) for s, t in observed}
    return Lineage(
        expected=len(declared),
        observed=len(seen),
        missing=tuple(sorted(f"{s} -> {t}" for s, t in declared - seen)),
        undeclared=tuple(sorted(f"{s} -> {t}" for s, t in seen - declared)),
    )


_READS = {"getTable", "getSchema", "getFunction", "listTables", "listSchemas", "tableExists", "getPermissions"}
_GRANTS = {"updatePermissions", "UpdateWorkspaceBindings"}


def actor(identity: str | None, deployer: str) -> str:
    """The role of an identity: names of people never reach the committed report."""
    if identity == deployer:
        return "deployer service principal (aml-pipeline)"
    if identity and "@" in identity:
        return "operator"
    return "Databricks"


def action_kind(action: str) -> str:
    if action in _GRANTS:
        return "permissions"
    if action in _READS:
        return "read"
    return "write"


def access(
    rows: Iterable[Sequence[Any]], deployer: str
) -> tuple[tuple[AccessCount, ...], tuple[PermissionChange, ...]]:
    counts: dict[tuple[str, str], list[int]] = {}
    changes = []
    for at, identity, action, status, securable, change in rows:
        who = actor(identity, deployer)
        kind = action_kind(str(action))
        tally = counts.setdefault((who, kind), [0, 0])
        tally[0] += 1
        tally[1] += int(str(status) == "403")
        if kind == "permissions" and str(status) == "200":
            changes.append(PermissionChange(str(at), who, str(securable), _redact(change)))
    return (
        tuple(AccessCount(a, k, n, d) for (a, k), (n, d) in sorted(counts.items())),
        tuple(sorted(changes, key=lambda c: c.at)),
    )


def _redact(changes: Any) -> str:
    """Grants as `principal: +privileges -privileges`; a principal that is a person becomes `operator`."""
    try:
        parsed = json.loads(changes) if isinstance(changes, str) else changes
    except ValueError:
        return ""
    out = []
    for change in parsed or []:
        principal = str(change.get("principal", ""))
        principal = "operator" if "@" in principal else principal
        added = ",".join(change.get("add", []))
        removed = ",".join(change.get("remove", []))
        out.append(
            f"{principal}: " + " ".join(p for p in (f"+{added}" * bool(added), f"-{removed}" * bool(removed)) if p)
        )
    return "; ".join(out)


# --- Rendering.


@dataclass(frozen=True)
class EstimateLine:
    meter: str
    unit: str
    measured: float
    estimated: Range


def against_estimate(
    report: Report, assumptions: Assumptions, prices: Iterable[UnitPrice], region: str
) -> list[EstimateLine]:
    """Billed DBUs of the window so far against the estimate scaled to the same number of days."""
    by_key = {p.key: p for p in prices if p.region == region}
    measured = {
        "jobs_serverless_dbu": sum(u.quantity for u in report.usage if "JOBS_SERVERLESS" in u.sku),
        "sql_serverless_dbu": sum(u.quantity for u in report.usage if "SERVERLESS_SQL" in u.sku),
    }
    lines = []
    for key, quantity in measured.items():
        estimated = [0.0, 0.0, 0.0]
        for line in assumptions.usage:
            if line.meter == key:
                for i, s in enumerate(SCENARIOS):
                    estimated[i] += line.once.get(s) + line.per_day.get(s) * report.days
        price = by_key.get(key)
        lines.append(EstimateLine(key, price.unit if price else "", quantity, Range(*estimated)))
    return lines


def operations_md(report: Report, comparison: Sequence[EstimateLine], region: str) -> str:
    list_total = sum(u.list_cost for u in report.usage)
    out = [
        "# Operating evidence",
        "",
        "Generated by `make operations` from the system tables into `migration/operations.json` (ADR 0009); "
        "do not edit.",
        "",
        f"Catalog `{report.catalog}`, a window of {report.days} days.",
        "",
        "## Cost",
        "",
        f"Billed usage at list price: **{list_total:.2f} USD** "
        "(system.billing.usage priced with system.billing.list_prices, before credits).",
        "",
        "| Product | SKU | Quantity | Unit | List cost (USD) |",
        "|---|---|---:|---|---:|",
        *[f"| {u.product} | `{u.sku}` | {u.quantity:.3f} | {u.unit} | {u.list_cost:.2f} |" for u in report.usage],
        "",
        "By job:",
        "",
        "| Job | DBU | List cost (USD) |",
        "|---|---:|---:|",
        *[f"| `{j.job}` | {j.dbus:.3f} | {j.list_cost:.2f} |" for j in report.jobs_cost],
        "",
    ]
    if report.azure:
        azure_total = sum(s.cost for s in report.azure)
        out += [
            f"Charged to the subscription by Azure Cost Management over the same days: **{azure_total:.2f} "
            f"{report.azure[0].currency}** (actual cost, before the free account's credit).",
            "",
            "| Azure service | Cost |",
            "|---|---:|",
            *[f"| {s.service} | {s.cost:.2f} |" for s in report.azure],
            "",
        ]
    out += [
        f"Against the estimate of step 1 ({region}, [cost-estimate.md](cost-estimate.md)), scaled to "
        f"{report.days} days of the window:",
        "",
        "| Meter | Measured | Estimated low | Estimated expected | Estimated high |",
        "|---|---:|---:|---:|---:|",
        *[
            f"| `{c.meter}` | {c.measured:.2f} | {c.estimated.low:.2f} | {c.estimated.expected:.2f} | "
            f"{c.estimated.high:.2f} |"
            for c in comparison
        ],
        "",
        "## Job runs",
        "",
        "| Job | Business date | Runs | Succeeded | Failed | Median (s) | Longest (s) |",
        "|---|---|---:|---:|---:|---:|---:|",
        *[
            f"| `{r.job}` | {r.scope or 'n/a'} | {r.runs} | {r.succeeded} | {r.failed} | {r.median_seconds} | "
            f"{r.max_seconds} |"
            for r in report.runs
        ],
        "",
        "## Lineage",
        "",
        f"Unity Catalog recorded {report.lineage.observed} table-to-table edges into `{report.catalog}`; the code "
        f"declares {report.lineage.expected} (dbt's dependencies in the staging schemas, then each staging table "
        f"to its published layer). "
        + (
            "**Every declared edge was recorded.**"
            if report.lineage.complete
            else f"**{len(report.lineage.missing)} declared edges were not recorded:**"
        ),
        "",
        *[f"- `{m}`" for m in report.lineage.missing],
    ]
    if report.lineage.undeclared:
        out += [
            "",
            "Recorded but not declared by the code (reads outside the transformations):",
            "",
            *[f"- `{u}`" for u in report.lineage.undeclared],
        ]
    out += [
        "",
        "## Access",
        "",
        f"Unity Catalog audit events on `{report.catalog}` and its objects, by role:",
        "",
        "| Role | Kind | Events | Denied |",
        "|---|---|---:|---:|",
        *[f"| {a.actor} | {a.kind} | {a.events:,} | {a.denied:,} |" for a in report.access],
        "",
        "Permission changes, oldest first:",
        "",
        "| Role | Securable | Change |",
        "|---|---|---|",
        *[f"| {p.actor} | `{p.securable}` | {p.changes} |" for p in report.permission_changes],
    ]
    return "\n".join(out) + "\n"


def azure_costs(subscription_id: str, first: date, last: date, token: str, client: Any) -> tuple[ServiceCost, ...]:
    """Actual cost of the subscription per Azure service over the window (Cost Management query API)."""
    response = client.post(
        f"https://management.azure.com/subscriptions/{subscription_id}/providers/Microsoft.CostManagement/query",
        params={"api-version": "2023-11-01"},
        headers={"Authorization": f"Bearer {token}"},
        json={
            "type": "ActualCost",
            "timeframe": "Custom",
            "timePeriod": {"from": f"{first.isoformat()}T00:00:00Z", "to": f"{last.isoformat()}T23:59:59Z"},
            "dataset": {
                "granularity": "None",
                "aggregation": {"cost": {"name": "Cost", "function": "Sum"}},
                "grouping": [{"type": "Dimension", "name": "ServiceName"}],
            },
        },
    )
    response.raise_for_status()
    properties = response.json()["properties"]
    names = [c["name"] for c in properties["columns"]]
    rows = [dict(zip(names, row, strict=True)) for row in properties["rows"]]
    return tuple(
        ServiceCost(str(r["ServiceName"]), round(float(r["Cost"]), 6), str(r["Currency"]))
        for r in sorted(rows, key=lambda r: -float(r["Cost"]))
    )
