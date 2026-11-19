"""What the parallel run compares, table by table, and what it accepts (`migration/parity.yaml`).

Every column of both sides is accounted for: compared, written by each platform about its own run
(lineage), or kept on premises with a reason. A column that appears on either side without being
listed fails the run instead of being left out of the comparison in silence.
"""

from __future__ import annotations

import hashlib
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class SpecError(ValueError):
    pass


@dataclass(frozen=True)
class TableSpec:
    relation: str
    key: tuple[str, ...]
    # Column the tables are published by; each value is compared on its own.
    slice: str | None = None
    # Source column -> namespace of the export's pseudonyms: the source's clear value is pseudonymized
    # with the export key before it is compared with the target's.
    pseudonymize: Mapping[str, str] = field(default_factory=dict)
    # Absolute tolerance of a double column; any other column must be equal.
    tolerance: Mapping[str, float] = field(default_factory=dict)
    # Source columns that never cross (ADR 0003), with the reason.
    on_premises: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Spec:
    lineage: frozenset[str]
    zero_difference: frozenset[str]
    tables: tuple[TableSpec, ...]
    digest: str

    @classmethod
    def read(cls, path: Path) -> Spec:
        text = path.read_text(encoding="utf-8")
        return cls.parse(yaml.safe_load(text), hashlib.sha256(text.encode()).hexdigest()[:16])

    @classmethod
    def parse(cls, raw: Mapping[str, Any], digest: str = "") -> Spec:
        tables = tuple(
            TableSpec(
                relation=relation,
                key=tuple(entry["key"]),
                slice=entry.get("slice"),
                pseudonymize=dict(entry.get("pseudonymize") or {}),
                tolerance={c: float(t) for c, t in (entry.get("tolerance") or {}).items()},
                on_premises=dict(entry.get("on_premises") or {}),
            )
            for relation, entry in raw["tables"].items()
        )
        spec = cls(frozenset(raw["lineage"]), frozenset(raw["zero_difference"]), tables, digest)
        if problems := spec.problems():
            raise SpecError("; ".join(problems))
        return spec

    def problems(self) -> list[str]:
        problems = []
        relations = {t.relation for t in self.tables}
        problems += [
            f"zero_difference names {r}, which is not compared" for r in sorted(self.zero_difference - relations)
        ]
        for table in self.tables:
            if not table.key:
                problems.append(f"{table.relation}: no key")
            if table.relation in self.zero_difference and table.tolerance:
                problems.append(
                    f"{table.relation}: must not differ at all, yet has tolerances {sorted(table.tolerance)}"
                )
            problems += [
                f"{table.relation}.{c}: tolerance {t} must be positive" for c, t in table.tolerance.items() if t <= 0
            ]
            problems += [
                f"{table.relation}.{c}: kept on premises without a reason"
                for c, r in table.on_premises.items()
                if not r
            ]
            listed = {*table.key, *([table.slice] if table.slice else []), *table.pseudonymize, *table.tolerance}
            problems += [
                f"{table.relation}.{c}: listed as compared and as kept on premises"
                for c in sorted(listed & set(table.on_premises))
            ]
            problems += [
                f"{table.relation}.{c}: a lineage column is never compared" for c in sorted(listed & self.lineage)
            ]
        return problems

    def table(self, relation: str) -> TableSpec:
        return next(t for t in self.tables if t.relation == relation)


@dataclass(frozen=True)
class Columns:
    """The columns of one table that the run compares, in the target's order."""

    compared: tuple[str, ...]
    problems: tuple[str, ...]


def reconcile(table: TableSpec, lineage: Collection[str], source: Collection[str], target: Collection[str]) -> Columns:
    """Compared columns, and every column of either side that the spec does not account for."""
    target_compared = [c for c in target if c not in lineage]
    source_compared = {c for c in source if c not in lineage and c not in table.on_premises}
    problems = [
        f"{table.relation}.{c}: on the target only; compare it or declare it lineage"
        for c in target_compared
        if c not in source_compared
    ]
    problems += [
        f"{table.relation}.{c}: on the source only; compare it or keep it on premises with a reason"
        for c in sorted(source_compared - set(target_compared))
    ]
    problems += [
        f"{table.relation}.{c}: declared on premises, absent from the source"
        for c in sorted(set(table.on_premises) - set(source))
    ]
    required = {*table.key, *([table.slice] if table.slice else []), *table.pseudonymize, *table.tolerance}
    problems += [
        f"{table.relation}.{c}: in the spec, not a compared column" for c in sorted(required - set(target_compared))
    ]
    return Columns(tuple(c for c in target_compared if c in source_compared), tuple(problems))
