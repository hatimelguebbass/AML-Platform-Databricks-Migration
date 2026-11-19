"""Exports one business date: read at one source commit, apply the policy, pass the gate, write."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
from pyiceberg.expressions import AlwaysTrue, BooleanExpression, EqualTo, In

from lakemig.export import delivery, gate
from lakemig.export.policy import ExportPlan, SelectionKind, TablePlan, Treatment
from lakemig.export.pseudonym import ALGORITHM, Key, pseudonymize
from lakemig.export.source import Source
from lakemig.inventory.model import Inventory

log = logging.getLogger(__name__)

CHANGE_BATCH = "audit.change_batch"


class SchemaDriftError(RuntimeError):
    """The source table no longer has the columns the inventory and the policy were reviewed against."""


class UndatedCaptureError(RuntimeError):
    pass


class GateBlockedError(RuntimeError):
    def __init__(self, business_date: date, report: gate.GateReport) -> None:
        self.report = report
        lines = [
            f"  {f.relation}.{f.column}: {f.detector} in {f.rows} rows, e.g. {', '.join(f.examples)}"
            for f in report.findings
        ]
        super().__init__(f"PII gate blocked the delivery of {business_date}:\n" + "\n".join(lines))


@dataclass(frozen=True)
class Context:
    source_repository: str
    source_commit: str
    policy_digest: str


def policy_digest(paths: Iterable[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def export_date(
    business_date: date,
    plan: ExportPlan,
    inventory: Inventory,
    source: Source,
    key: Key,
    out: Path,
    context: Context,
) -> delivery.Manifest:
    if business_date < plan.first_business_date:
        raise ValueError(f"{business_date} is before the first business date {plan.first_business_date}")
    names = _name_index(plan, source)
    runs = capture_runs(business_date, plan.first_business_date, source)
    report = gate.GateReport()
    tables: list[tuple[TablePlan, pa.Table]] = []
    for table_plan in plan.tables:
        _check_schema(table_plan, inventory, source)
        raw = source.read(
            table_plan.relation,
            [c.source for c in table_plan.crossing()],
            _row_filter(table_plan, business_date, runs),
        )
        crossed = cross(table_plan, raw, key)
        gate.scan(table_plan.relation, crossed, names, plan.exemptions, report)
        tables.append((table_plan, crossed))
        log.info("%s %s: %d rows", business_date, table_plan.relation, crossed.num_rows)
    if not report.passed:
        raise GateBlockedError(business_date, report)
    header = delivery.Header(
        source={
            "repository": context.source_repository,
            "commit": context.source_commit,
            "lakehouse_ref": source.ref,
        },
        policy_digest=context.policy_digest,
        pseudonymization={"algorithm": ALGORITHM, "key_id": key.key_id},
        gate={
            "detectors": list(gate.DETECTOR_NAMES),
            "known_names": len(names),
            "rows_scanned": report.rows_scanned,
            "columns_scanned": report.columns_scanned,
            "findings": 0,
            "exempted": [
                {**asdict(f), "examples": list(f.examples), "reason": _reason(plan, f)} for f in report.exempted
            ],
        },
    )
    return delivery.write(out, business_date, tables, header)


def cross(plan: TablePlan, raw: pa.Table, key: Key) -> pa.Table:
    """The table as it crosses: treatments applied, plain Arrow types, rows in a fixed order."""
    columns: dict[str, pa.Array | pa.ChunkedArray] = {}
    for rule in plan.crossing():
        if rule.output is None:
            raise ValueError(f"{plan.relation}.{rule.source}: crosses without an output name")
        values = raw.column(rule.source)
        if rule.treatment is Treatment.PSEUDONYMIZE:
            values = pseudonymize(values, key, rule.namespace or "")
        elif rule.treatment is Treatment.YEAR:
            values = pc.year(values).cast(pa.int32())
        columns[rule.output] = _plain(values)
    table = pa.table(columns)
    return table.sort_by([(c, "ascending") for c in plan.order_by])


def _plain(values: pa.Array | pa.ChunkedArray) -> pa.Array | pa.ChunkedArray:
    # The Iceberg reader returns large_string; Spark and Databricks map both to STRING.
    if pa.types.is_large_string(values.type):
        return values.cast(pa.string())
    return values


def _row_filter(plan: TablePlan, business_date: date, runs: Mapping[str | None, list[str]]) -> BooleanExpression:
    selection = plan.selection
    if selection.kind is SelectionKind.BUSINESS_DATE and selection.argument is not None:
        return _equal(selection.argument, business_date.isoformat())
    if selection.kind is SelectionKind.CAPTURE_RUNS:
        run_ids = runs.get(selection.argument, [])
        column = "run_id" if selection.argument is None else "_ingest_run_id"
        # An empty IN is not a valid Iceberg predicate; no run id is empty.
        return _in(column, run_ids) if run_ids else _equal(column, "")
    return AlwaysTrue()


def capture_runs(business_date: date, first_business_date: date, source: Source) -> dict[str | None, list[str]]:
    """Change-capture runs delivered with `business_date`, by target table (None: every target).

    A run belongs to the business date the daily pipeline gave it. Runs without one (the initial
    snapshot, captures made by hand before the daily runs began) and runs dated earlier go with
    the first business date. An undated run made after the daily runs began would be in no
    delivery: the export stops rather than lose its change events.
    """
    batches = source.read(CHANGE_BATCH, ["run_id", "target_table", "business_date", "processed_at"]).to_pylist()
    daily = [b["processed_at"] for b in batches if b["business_date"] is not None]
    late = sorted(
        {b["run_id"] for b in batches if b["business_date"] is None and daily and b["processed_at"] > min(daily)}
    )
    if late:
        raise UndatedCaptureError(
            f"capture runs {', '.join(late)} have no business date and ran after the daily runs began: "
            "rerun them through the daily pipeline, which dates them, before exporting"
        )

    def delivered(batch_date: date | None) -> bool:
        if business_date == first_business_date:
            return batch_date is None or batch_date <= business_date
        return batch_date == business_date

    runs: dict[str | None, list[str]] = {}
    for batch in batches:
        if delivered(batch["business_date"]):
            for target in (batch["target_table"], None):
                runs.setdefault(target, []).append(batch["run_id"])
    return {target: sorted(set(ids)) for target, ids in runs.items()}


# pyiceberg's expressions are pydantic models whose declared fields mypy takes for the constructor.
def _equal(column: str, value: str) -> BooleanExpression:
    return EqualTo(column, value)  # type: ignore[call-arg, arg-type]


def _in(column: str, values: list[str]) -> BooleanExpression:
    return In(column, values)  # type: ignore[call-arg, arg-type]


def _check_schema(plan: TablePlan, inventory: Inventory, source: Source) -> None:
    expected = inventory.by_relation()[plan.relation].column_names()
    live = source.columns(plan.relation)
    added = sorted(set(live) - set(expected))
    removed = sorted(set(expected) - set(live))
    if added or removed:
        raise SchemaDriftError(
            f"{plan.relation} at {source.ref}: columns added {added or '[]'}, removed {removed or '[]'} since the "
            "inventory; collect it again and classify the new columns before exporting"
        )


def _name_index(plan: ExportPlan, source: Source) -> gate.NameIndex:
    groups = plan.known_names.names
    columns = list(dict.fromkeys(c for group in groups for c in group))
    index = gate.NameIndex.from_table(source.read(plan.known_names.relation, columns), groups)
    if not len(index):
        raise RuntimeError(f"{plan.known_names.relation} gave no names: the gate would not detect any")
    return index


def _reason(plan: ExportPlan, finding: gate.Finding) -> str:
    return next(
        e.reason
        for e in plan.exemptions
        if (e.relation, e.column, e.detector) == (finding.relation, finding.column, finding.detector)
    )


def blocked_report(error: GateBlockedError) -> str:
    return json.dumps([asdict(f) for f in error.report.findings], indent=2) + "\n"
