"""Sensitivity classification of every inventoried column (`migration/classification.yaml`).

Rules match column names, first match wins; overrides pin a level for one column of one table.
There is deliberately no default: a column no rule matches fails the check, so a column the
source adds later is classified by a reviewed commit before anything can export it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from lakemig.inventory.model import DataObject


class Level(StrEnum):
    # Identifies a person on its own: name, national ID, phone, e-mail, street address.
    DIRECT = "direct"
    # Identifies a person only combined with other data: birth date, city, account or customer key.
    INDIRECT = "indirect"
    NONE = "none"


@dataclass(frozen=True)
class Rule:
    pattern: re.Pattern[str]
    level: Level
    reason: str


@dataclass(frozen=True)
class Override:
    relation: str
    column: str
    level: Level
    reason: str


@dataclass(frozen=True)
class Policy:
    rules: tuple[Rule, ...]
    overrides: tuple[Override, ...]

    @classmethod
    def read(cls, path: Path) -> Policy:
        return cls.from_dict(yaml.safe_load(path.read_text(encoding="utf-8")))

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Policy:
        return cls(
            rules=tuple(
                Rule(re.compile(r["pattern"], re.VERBOSE), Level(r["level"]), r["reason"]) for r in raw.get("rules", ())
            ),
            overrides=tuple(
                Override(o["table"], o["column"], Level(o["level"]), o["reason"]) for o in raw.get("overrides", ())
            ),
        )


@dataclass(frozen=True)
class ColumnClass:
    relation: str
    column: str
    level: Level
    reason: str


@dataclass(frozen=True)
class Classification:
    columns: tuple[ColumnClass, ...]
    unclassified: tuple[tuple[str, str], ...]
    # Overrides naming a column the inventory does not have: stale after a source change.
    unused_overrides: tuple[Override, ...]

    def of(self, relation: str) -> dict[str, Level]:
        return {c.column: c.level for c in self.columns if c.relation == relation}

    def problems(self) -> list[str]:
        found = [f"{relation}.{column}: no classification rule matches" for relation, column in self.unclassified]
        found += [f"override {o.relation}.{o.column} matches no inventoried column" for o in self.unused_overrides]
        return found


def classify(objects: Iterable[DataObject], policy: Policy) -> Classification:
    overrides = {(o.relation, o.column): o for o in policy.overrides}
    used: set[tuple[str, str]] = set()
    columns: list[ColumnClass] = []
    unclassified: list[tuple[str, str]] = []
    for obj in objects:
        for name in obj.column_names():
            key = (obj.relation, name)
            override = overrides.get(key)
            if override is not None:
                used.add(key)
                columns.append(ColumnClass(obj.relation, name, override.level, override.reason))
                continue
            rule = next((r for r in policy.rules if r.pattern.fullmatch(name)), None)
            if rule is None:
                unclassified.append(key)
            else:
                columns.append(ColumnClass(obj.relation, name, rule.level, rule.reason))
    return Classification(
        columns=tuple(columns),
        unclassified=tuple(unclassified),
        unused_overrides=tuple(o for key, o in overrides.items() if key not in used),
    )
