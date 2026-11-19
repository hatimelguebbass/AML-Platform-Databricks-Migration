"""Export policy (`migration/export.yaml`): what crosses the boundary, column by column.

The policy is checked against the inventory, the classification and the waves, so that a change
to any of them that would let an unreviewed column cross fails CI rather than an export:
- every exported object of the waves has an entry, and nothing else does;
- a `direct` column never crosses and is never listed;
- every `indirect` column states its treatment: there is no default;
- a `none` column crosses as it is unless the policy drops it.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from lakemig.assessment.classification import Classification, Level
from lakemig.assessment.waves import Fate, Plan
from lakemig.inventory.model import Inventory


class Treatment(StrEnum):
    KEEP = "keep"
    DROP = "drop"
    # Keyed HMAC-SHA256 whose key stays on premises; equal values of one namespace give equal pseudonyms.
    PSEUDONYMIZE = "pseudonymize"
    # A date reduced to its year, under a new column name.
    YEAR = "year"


class SelectionKind(StrEnum):
    # Rows whose business date column holds the delivery date.
    BUSINESS_DATE = "business_date"
    # Rows written by the change-capture runs delivered with the date (see `capture_runs` in run.py):
    # the change events of one target table, or the run records of audit.change_batch itself.
    CAPTURE_RUNS = "capture_runs"
    # The whole table, as of the source commit of the delivery.
    SNAPSHOT = "snapshot"


@dataclass(frozen=True)
class Selection:
    kind: SelectionKind
    # BUSINESS_DATE: the date column; CAPTURE_RUNS: the target table of the runs, None for every target.
    argument: str | None = None


@dataclass(frozen=True)
class ColumnRule:
    source: str
    level: Level
    treatment: Treatment
    output: str | None
    namespace: str | None = None


@dataclass(frozen=True)
class TablePlan:
    relation: str
    name: str
    selection: Selection
    order_by: tuple[str, ...]
    columns: tuple[ColumnRule, ...]

    def crossing(self) -> tuple[ColumnRule, ...]:
        return tuple(c for c in self.columns if c.treatment is not Treatment.DROP)

    def output_columns(self) -> tuple[str, ...]:
        return tuple(c.output for c in self.crossing() if c.output is not None)


@dataclass(frozen=True)
class Exemption:
    relation: str
    column: str
    detector: str
    reason: str


@dataclass(frozen=True)
class NameSource:
    """On-premises relation whose names the gate looks for; each group of columns is one name."""

    relation: str
    names: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class ExportPlan:
    # The first delivery also carries the change captures made before the daily runs began.
    first_business_date: date
    tables: tuple[TablePlan, ...]
    exemptions: tuple[Exemption, ...]
    known_names: NameSource

    def table(self, relation: str) -> TablePlan:
        return next(t for t in self.tables if t.relation == relation)


@dataclass(frozen=True)
class Resolution:
    plan: ExportPlan
    problems: tuple[str, ...]


class PolicyError(ValueError):
    pass


def read(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise PolicyError(f"{path}: expected a mapping")
    return raw


def resolve(
    raw: Mapping[str, Any],
    inventory: Inventory,
    classification: Classification,
    waves: Plan,
    detectors: Collection[str],
) -> Resolution:
    problems: list[str] = []
    objects = inventory.by_relation()
    exported = {r for w in waves.waves for r, fate in w.objects.items() if fate is Fate.EXPORT}
    entries: Mapping[str, Any] = raw.get("tables", {})
    problems += [f"{r}: exported by the waves, absent from the export policy" for r in sorted(exported - set(entries))]
    problems += [f"{r}: in the export policy, not exported by the waves" for r in sorted(set(entries) - exported)]

    tables = []
    for relation, entry in entries.items():
        if relation not in exported or relation not in objects:
            continue
        table, table_problems = _table(relation, entry, objects[relation].column_names(), classification.of(relation))
        tables.append(table)
        problems += table_problems
    names = [t.name for t in tables]
    problems += [f"delivery table name {n} used twice" for n in sorted({n for n in names if names.count(n) > 1})]

    by_relation = {t.relation: t for t in tables}
    gate: Mapping[str, Any] = raw.get("gate", {})
    exemptions = tuple(
        Exemption(e["table"], e["column"], e["detector"], e["reason"]) for e in gate.get("exemptions", ())
    )
    for e in exemptions:
        exempted = by_relation.get(e.relation)
        if exempted is None or e.column not in exempted.output_columns():
            problems.append(f"exemption {e.relation}.{e.column}: not a column of the delivery")
        if e.detector not in detectors:
            problems.append(f"exemption {e.relation}.{e.column}: unknown detector {e.detector}")

    names_raw = gate.get("known_names", {})
    known_names = NameSource(names_raw.get("table", ""), tuple(tuple(g) for g in names_raw.get("names", ())))
    problems += _name_source_problems(known_names, inventory, classification, waves)
    first = raw.get("first_business_date")
    if not isinstance(first, date):
        problems.append(f"first_business_date must be a date (YYYY-MM-DD), got {first!r}")
        first = date.min
    return Resolution(ExportPlan(first, tuple(tables), exemptions, known_names), tuple(problems))


def _table(
    relation: str, entry: Mapping[str, Any], columns: tuple[str, ...], levels: Mapping[str, Level]
) -> tuple[TablePlan, list[str]]:
    problems: list[str] = []
    listed: Mapping[str, Any] = entry.get("columns") or {}
    problems += [f"{relation}.{c}: in the export policy but not in the inventory" for c in listed if c not in columns]

    rules = []
    for column in columns:
        level = levels.get(column)
        if level is None:
            problems.append(f"{relation}.{column}: not classified")
            continue
        spec = listed.get(column)
        if level is Level.DIRECT:
            if spec is not None:
                problems.append(f"{relation}.{column}: direct identifier, never crosses: remove it from the policy")
            rules.append(ColumnRule(column, level, Treatment.DROP, None))
            continue
        if spec is None:
            if level is Level.INDIRECT:
                problems.append(f"{relation}.{column}: indirect identifier without a treatment")
                rules.append(ColumnRule(column, level, Treatment.DROP, None))
            else:
                rules.append(ColumnRule(column, level, Treatment.KEEP, column))
            continue
        rule, rule_problems = _column(relation, column, level, spec)
        rules.append(rule)
        problems += rule_problems

    table = TablePlan(
        relation=relation,
        name=entry.get("name", relation.split(".")[1]),
        selection=_selection(entry.get("selection")),
        order_by=tuple(entry.get("order_by", ())),
        columns=tuple(rules),
    )
    outputs = table.output_columns()
    twice = sorted({c for c in outputs if outputs.count(c) > 1})
    problems += [f"{relation}: output column {c} produced twice" for c in twice]
    if not table.order_by:
        problems.append(f"{relation}: order_by is required, it makes the delivery files reproducible")
    problems += [f"{relation}: order_by column {c} does not cross" for c in table.order_by if c not in outputs]
    selection = table.selection
    if selection.kind is SelectionKind.BUSINESS_DATE and selection.argument not in outputs:
        problems.append(f"{relation}: business date column {selection.argument} does not cross")
    if selection.kind is SelectionKind.CAPTURE_RUNS:
        run_column = "run_id" if selection.argument is None else "_ingest_run_id"
        if run_column not in outputs:
            problems.append(f"{relation}: capture runs are selected by {run_column}, which does not cross")
    return table, problems


def _column(relation: str, column: str, level: Level, spec: Any) -> tuple[ColumnRule, list[str]]:
    if isinstance(spec, str):
        spec = {"treatment": spec}
    try:
        treatment = Treatment(spec.get("treatment"))
    except ValueError:
        return ColumnRule(column, level, Treatment.DROP, None), [f"{relation}.{column}: unknown treatment {spec}"]
    if treatment is Treatment.DROP:
        return ColumnRule(column, level, treatment, None), []
    if treatment is Treatment.KEEP:
        return ColumnRule(column, level, treatment, column), []
    if treatment is Treatment.PSEUDONYMIZE:
        namespace = spec.get("namespace")
        problems = [] if namespace else [f"{relation}.{column}: pseudonymize needs a namespace"]
        return ColumnRule(column, level, treatment, column, namespace), problems
    output = spec.get("as")
    problems = [] if output else [f"{relation}.{column}: year needs the output column name (as)"]
    return ColumnRule(column, level, treatment, output), problems


def _selection(raw: Any) -> Selection:
    if raw in (SelectionKind.SNAPSHOT.value, SelectionKind.CAPTURE_RUNS.value):
        return Selection(SelectionKind(raw))
    if not isinstance(raw, Mapping) or len(raw) != 1:
        raise PolicyError(f"selection must be a kind or a one-key mapping of kind to argument, got {raw!r}")
    ((kind, argument),) = raw.items()
    return Selection(SelectionKind(kind), str(argument))


def _name_source_problems(
    source: NameSource, inventory: Inventory, classification: Classification, waves: Plan
) -> list[str]:
    if source.relation not in waves.on_premises:
        return [f"gate.known_names: {source.relation or '(missing)'} must be a relation kept on premises"]
    obj = inventory.by_relation().get(source.relation)
    if obj is None:
        return [f"gate.known_names: {source.relation} not in the inventory"]
    levels = classification.of(source.relation)
    return [
        f"gate.known_names: {source.relation}.{c} is not a direct identifier column"
        for group in source.names
        for c in group
        if levels.get(c) is not Level.DIRECT
    ] or ([] if source.names else ["gate.known_names: no name columns"])
