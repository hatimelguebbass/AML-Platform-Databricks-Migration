"""Go/no-go checks of the cutover and its rollback, and the record of each execution (docs/cutover.md).

Each phase is a list of named checks run against the live platforms and appended, timed, to
`migration/cutover.json`; `lakemig assess` renders it. A phase passes only if every check does;
`switch` and `rollback` change which platform the report reads only when they pass.

- preflight: the target is ready (key custody, deliveries, publications, parity, the report's
  columns, grants, no run in flight); run as often as needed.
- freeze:    tags the source lakehouse at the head of its published branch: the reference that the
  final sync exports and the parity compares, and the state a rollback returns the report to.
- switch:    preflight against the frozen reference, then the report's tables read on both sides.
- rollback:  the report's tables on the source, and the business dates the target published after
  the freeze that the source does not have (a catch-up the source must run before the switch back).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lakemig.cutover.semantic_model import ImportedTable, Plan

FORMAT_VERSION = 1
PHASES = ("preflight", "freeze", "switch", "rollback")
SERVING = {"switch": "Databricks", "rollback": "Trino"}

Rows = list[list[str | None]]
Query = Callable[[str], Rows]

# Power Query types of the model and the Databricks SQL types each one loads from.
M_TYPES: Mapping[str, tuple[str, ...]] = {
    "type text": ("string",),
    "type date": ("date",),
    "type datetime": ("timestamp", "timestamp_ntz"),
    "type logical": ("boolean",),
    "Int64.Type": ("bigint", "int", "smallint", "tinyint"),
    "Currency.Type": ("decimal",),
    "type number": ("double", "float", "decimal", "bigint", "int"),
}


def compatible(m_type: str, data_type: str) -> bool:
    return data_type.lower().split("(")[0].strip() in M_TYPES.get(m_type, ())


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class Event:
    phase: str
    started_at: str
    finished_at: str
    seconds: float
    passed: bool
    checks: tuple[Check, ...]
    # The platform the report reads after this event (switch and rollback only, when passed).
    serving: str | None = None
    facts: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Record:
    format_version: int
    catalog: str
    events: tuple[Event, ...]

    @classmethod
    def read(cls, path: Path) -> Record:
        raw = json.loads(path.read_text(encoding="utf-8"))
        events = tuple(Event(**{**e, "checks": tuple(Check(**c) for c in e["checks"])}) for e in raw["events"])
        return cls(raw["format_version"], raw["catalog"], events)

    def write(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")

    def append(self, event: Event) -> Record:
        return Record(self.format_version, self.catalog, (*self.events, event))

    def serving(self) -> str:
        changes = [e.serving for e in self.events if e.passed and e.serving]
        return changes[-1] if changes else "Trino"

    def frozen(self) -> Event | None:
        freezes = [e for e in self.events if e.phase == "freeze" and e.passed]
        return freezes[-1] if freezes else None


def timed(phase: str, steps: Iterable[Callable[[], Check]], facts: Mapping[str, Any] | None = None) -> Event:
    """Runs every step even after a failure: the record shows all that blocks the phase at once."""
    started, clock = datetime.now(UTC), time.monotonic()
    checks = []
    for step in steps:
        try:
            checks.append(step())
        except Exception as error:  # an unreachable platform is a failed check, recorded
            checks.append(Check(getattr(step, "__name__", "check"), False, f"{type(error).__name__}: {error}"))
    seconds = round(time.monotonic() - clock, 1)
    passed = all(c.passed for c in checks)
    return Event(
        phase=phase,
        started_at=started.isoformat(timespec="seconds"),
        finished_at=datetime.now(UTC).isoformat(timespec="seconds"),
        seconds=seconds,
        passed=passed,
        checks=tuple(checks),
        serving=SERVING.get(phase) if passed else None,
        facts=dict(facts or {}),
    )


# --- offline: the rendered report against the transformations ---------------------------------


def contract_problems(
    tables: Sequence[ImportedTable], plan: Plan, models: Mapping[str, Mapping[str, tuple[str, str | None]]]
) -> list[str]:
    """`models`: relation -> column -> (data type, mask function) of the published dbt models.

    Every relation read from the target must be published with each column the report imports,
    of a type it loads, and unmasked: the report's identity is not in the privileged groups, so a
    masked column would import as empty without an error.
    """
    problems = []
    for table in tables:
        serving = plan.serving(table.relation)
        if serving == "retired":
            problems.append(f"{table.name}: imports {table.relation}, retired by the cutover plan: render bi/ again")
            continue
        if serving == "on_premises":
            if table.expression != "OnPremises":
                problems.append(f"{table.name}: {table.relation} stays on premises but is read from {table.expression}")
            continue
        if table.expression != "Lakehouse":
            problems.append(f"{table.name}: {table.relation} is served by the target but read from {table.expression}")
        model = models.get(table.relation)
        if model is None:
            problems.append(f"{table.name}: imports {table.relation}, which the transformations do not publish")
            continue
        for column, m_type in table.columns.items():
            if column not in model:
                problems.append(f"{table.name}: {table.relation}.{column} is not published")
            elif not compatible(m_type, model[column][0]):
                problems.append(f"{table.name}: {table.relation}.{column} is {model[column][0]}, imported as {m_type}")
            elif model[column][1]:
                problems.append(f"{table.name}: {table.relation}.{column} is masked by {model[column][1]}")
    return problems


# --- online checks ------------------------------------------------------------------------------

ACCEPTED_KEY_SQL = "select max_by(key_id, logged_at) from {catalog}.landing.delivery_log where status = 'accepted'"
DECISIONS_SQL = (
    "select cast(business_date as string), delivery_id, max_by(status, logged_at), max(logged_at) "
    "from {catalog}.landing.delivery_log group by all order by 1, 4"
)
COLUMNS_SQL = (
    "select table_schema, table_name, column_name, lower(full_data_type) from {catalog}.information_schema.columns "
    "where table_schema in ({schemas})"
)
SCHEMA_GRANTS_SQL = (
    "select schema_name, privilege_type from {catalog}.information_schema.schema_privileges where grantee = '{group}'"
)
CATALOG_GRANTS_SQL = (
    "select privilege_type from {catalog}.information_schema.catalog_privileges where grantee = '{group}'"
)


def key_custody(query: Query, catalog: str, local_key_id: str) -> Check:
    rows = query(ACCEPTED_KEY_SQL.format(catalog=catalog))
    accepted = rows[0][0] if rows else None
    if accepted is None:
        return Check("key_custody", False, "no accepted delivery on the target")
    return Check(
        "key_custody",
        accepted == local_key_id,
        f"target pseudonyms under key {accepted}, the on-premises key is {local_key_id}"
        + ("" if accepted == local_key_id else ": new deliveries would be refused (rotate: docs/runbook.md)"),
    )


def deliveries(query: Query, catalog: str, dates: Sequence[str]) -> Check:
    """Each date's latest decided delivery is accepted: nothing waits in quarantine."""
    latest: dict[str, tuple[str, str]] = {}
    for day, delivery_id, status, _ in query(DECISIONS_SQL.format(catalog=catalog)):
        if status != "superseded":
            latest[str(day)] = (str(delivery_id), str(status))
    missing = [d for d in dates if d not in latest]
    refused = [f"{d} ({latest[d][0]})" for d in dates if d in latest and latest[d][1] != "accepted"]
    detail = f"{len(dates) - len(missing) - len(refused)}/{len(dates)} dates with an accepted delivery"
    if missing:
        detail += f"; none for {', '.join(missing)}"
    if refused:
        detail += f"; latest refused: {', '.join(refused)}"
    return Check("deliveries", not missing and not refused, detail)


def publications(published: Mapping[str, str], dates: Sequence[str]) -> Check:
    missing = [d for d in dates if d not in published]
    return Check(
        "publications",
        not missing,
        f"{len(dates) - len(missing)}/{len(dates)} dates published"
        + (f"; missing {', '.join(missing)}" if missing else ""),
    )


def parity(report: Any, dates: Sequence[str], published: Mapping[str, str], reference_hash: str | None) -> Check:
    """The parity report compares the dates, at the frozen reference, against today's publications."""
    problems = []
    if not report.accepted:
        problems.append("tables differ: " + ", ".join(t.relation for t in report.tables if not t.equal))
    if (report.business_dates[0], report.business_dates[1]) != (dates[0], dates[-1]):
        problems.append(f"compares {report.business_dates[0]}..{report.business_dates[1]}, not {dates[0]}..{dates[-1]}")
    measured_hash = report.lakehouse_ref.rpartition("@")[2]
    if reference_hash is not None and measured_hash != reference_hash:
        problems.append(
            f"measured at source commit {measured_hash[:12]}, the frozen reference is {reference_hash[:12]}"
        )
    republished = sorted(d for d in dates if report.publications.get(d) != published.get(d))
    if republished:
        problems.append(f"republished since the comparison: {', '.join(republished)}")
    rows = sum(s.target_rows for t in report.tables for s in t.slices)
    detail = "; ".join(problems) or f"{len(report.tables)} tables equal, {rows} rows"
    return Check("parity", not problems, detail)


def report_columns(query: Query, catalog: str, tables: Sequence[ImportedTable], plan: Plan) -> Check:
    served = [t for t in tables if plan.serving(t.relation) == "target"]
    schemas = sorted({t.relation.split(".")[0] for t in served})
    actual: dict[str, dict[str, str]] = {}
    for schema, name, column, data_type in query(
        COLUMNS_SQL.format(catalog=catalog, schemas=", ".join(f"'{s}'" for s in schemas))
    ):
        actual.setdefault(f"{schema}.{name}", {})[str(column)] = str(data_type)
    problems = []
    for table in served:
        columns = actual.get(table.relation, {})
        for column, m_type in table.columns.items():
            if column not in columns:
                problems.append(f"{table.relation}.{column} missing")
            elif not compatible(m_type, columns[column]):
                problems.append(f"{table.relation}.{column} is {columns[column]}, imported as {m_type}")
    count = sum(len(t.columns) for t in served)
    return Check(
        "report_columns", not problems, "; ".join(problems) or f"{count} columns of {len(served)} tables, types load"
    )


def reader_grants(query: Query, catalog: str, tables: Sequence[ImportedTable], plan: Plan, group: str) -> Check:
    schemas = sorted({t.relation.split(".")[0] for t in tables if plan.serving(t.relation) == "target"})
    held: dict[str, set[str]] = {}
    for schema, privilege in query(SCHEMA_GRANTS_SQL.format(catalog=catalog, group=group)):
        held.setdefault(str(schema), set()).add(str(privilege).replace(" ", "_"))
    catalog_privileges = {
        str(p).replace(" ", "_") for (p,) in query(CATALOG_GRANTS_SQL.format(catalog=catalog, group=group))
    }
    problems = [] if "USE_CATALOG" in catalog_privileges else [f"no USE CATALOG on {catalog}"]
    for schema in schemas:
        lacking = {"USE_SCHEMA", "SELECT"} - held.get(schema, set())
        if lacking:
            problems.append(f"{schema}: lacks {', '.join(sorted(lacking))}")
    return Check("reader_grants", not problems, "; ".join(problems) or f"{group} reads {', '.join(schemas)}")


def no_active_runs(active: Sequence[str]) -> Check:
    return Check("no_active_runs", not active, ", ".join(active) or "no job run in flight")


def source_frozen(tag: str, tag_hash: str | None, head_hash: str, branch: str) -> Check:
    if tag_hash is None:
        return Check("source_frozen", False, f"no tag {tag} on the source lakehouse: run the freeze first")
    return Check(
        "source_frozen",
        tag_hash == head_hash,
        f"{tag} at {tag_hash[:12]}, {branch} at {head_hash[:12]}"
        + ("" if tag_hash == head_hash else ": the source published after the freeze, sync again"),
    )


def row_counts(target: Mapping[str, int], source: Mapping[str, int]) -> Check:
    """The report's tables hold as many rows on each side: the numbers it shows do not move."""
    differ = [
        f"{r}: target {target.get(r)}, source {source.get(r)}" for r in sorted(target) if target[r] != source.get(r)
    ]
    total = sum(target.values())
    return Check("report_rows", not differ, "; ".join(differ) or f"{len(target)} tables, {total} rows on each side")


def catch_up(target_dates: Iterable[str], source_dates: Iterable[str], frozen_until: str) -> Check:
    """Dates the target published after the freeze that the source lacks: a rollback must run them first."""
    missing = sorted(set(d for d in target_dates if d > frozen_until) - set(source_dates))
    return Check(
        "source_current",
        not missing,
        f"source must run {', '.join(missing)} before the switch back"
        if missing
        else "no date to catch up on the source",
    )


def readable(name: str, counts: Mapping[str, int]) -> Check:
    empty = sorted(r for r, n in counts.items() if n == 0)
    return Check(name, not empty, f"empty: {', '.join(empty)}" if empty else f"{len(counts)} tables read")


# --- rendering ----------------------------------------------------------------------------------


def cutover_md(record: Record | None, plan: Plan, tables: Sequence[ImportedTable]) -> str:
    lines = [
        "# Cutover",
        "",
        "Generated by `lakemig assess` from `migration/cutover.yaml`, the rendered report in `bi/` and the",
        "executions recorded in `migration/cutover.json` (`make cutover PHASE=...`). Runbook: `docs/cutover.md`.",
        "",
        "## What the report reads after the cutover",
        "",
        "| Report table | Relation | Columns | Read from |",
        "|---|---|---:|---|",
    ]
    places = {"target": "target catalog (`Lakehouse`)", "on_premises": "source Trino, on premises (`OnPremises`)"}
    for table in tables:
        lines.append(
            f"| {table.name} | `{table.relation}` | {len(table.columns)} | {places[plan.serving(table.relation)]} |"
        )
    for relation, reason in sorted(plan.retired.items()):
        lines.append(f"| (removed) | `{relation}` | | retired: {reason} |")
    lines += ["", f"Rollback window: {plan.rollback_window_days} days.", ""]
    if record is None or not record.events:
        return "\n".join([*lines, "## Executions", "", "None recorded yet.", ""])
    lines += [f"## Executions on `{record.catalog}`", "", f"The report reads **{record.serving()}**.", ""]
    freeze = record.frozen()
    switches = [e for e in record.events if e.phase == "switch" and e.passed]
    if freeze and switches:
        window = (_at(switches[-1].finished_at) - _at(freeze.started_at)).total_seconds()
        lines += [f"Cutover window, freeze to switch: **{_duration(window)}**.", ""]
    rollbacks = [e for e in record.events if e.phase == "rollback" and e.passed]
    if rollbacks:
        lines += [f"Rollback, decision to report back on the source: **{_duration(rollbacks[-1].seconds)}**.", ""]
    lines += ["| Phase | Duration | Result | Checks |", "|---|---:|---|---|"]
    for event in record.events:
        failed = [c.name for c in event.checks if not c.passed]
        outcome = "passed" if event.passed else "**failed**: " + ", ".join(failed)
        lines.append(f"| {event.phase} | {_duration(event.seconds)} | {outcome} | {len(event.checks)} |")
    for number, event in enumerate(record.events, 1):
        lines += ["", f"### {number}. {event.phase}", ""]
        for name, value in event.facts.items():
            lines.append(f"- {name}: `{value}`")
        if event.facts:
            lines.append("")
        lines += ["| Check | Result | Detail |", "|---|---|---|"]
        lines += [f"| {c.name} | {'pass' if c.passed else '**fail**'} | {_cell(c.detail)} |" for c in event.checks]
    return "\n".join(lines) + "\n"


def _cell(text: str) -> str:
    # A check that raised carries the engine's message, over several lines and with SQL in it.
    return " ".join(text.split()).replace("|", "\\|")


def _at(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _duration(seconds: float) -> str:
    minutes, rest = divmod(round(seconds), 60)
    return f"{minutes} min {rest:02d} s" if minutes else f"{rest} s"
