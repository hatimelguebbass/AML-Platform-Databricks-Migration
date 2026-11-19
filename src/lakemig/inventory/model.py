"""The inventory of the source platform, as collected and as committed to `migration/inventory.json`.

The committed file is the contract between the assessment and every later step: classification,
waves, cost estimate and parity all read it, so CI checks them without the source platform running.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class ObjectKind(StrEnum):
    # Written by Spark jobs (ingestion, change data capture, audit), read by dbt as sources.
    SOURCE_TABLE = "source_table"
    MODEL = "model"
    SEED = "seed"


@dataclass(frozen=True)
class Column:
    name: str
    data_type: str


@dataclass(frozen=True)
class Volume:
    rows: int
    # Unknown for tables outside the lakehouse, whose storage the query engine does not expose.
    data_bytes: int | None
    data_files: int | None


@dataclass(frozen=True)
class DataObject:
    """A table of the source lakehouse, identified as `<schema>.<name>` like its Trino relation."""

    schema: str
    name: str
    kind: ObjectKind
    columns: tuple[Column, ...]
    materialization: str | None = None
    depends_on: tuple[str, ...] = ()
    # Function name -> number of calls in the compiled SQL, models only.
    functions: dict[str, int] = field(default_factory=dict)
    macros: tuple[str, ...] = ()
    # Data and unit tests attached to the object, all of which are ported with it.
    tests: int = 0
    volume: Volume | None = None

    @property
    def relation(self) -> str:
        return f"{self.schema}.{self.name}"

    def column_names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns)


@dataclass(frozen=True)
class AccessRule:
    """One rule of the source query engine's access control, in evaluation order."""

    users: str
    scope: str
    catalog: str | None
    schema: str | None
    allow: str


@dataclass(frozen=True)
class Inventory:
    source_repository: str
    source_commit: str
    collected_at: str
    engine_version: str
    objects: tuple[DataObject, ...]
    access_rules: tuple[AccessRule, ...]
    # Source SQL function -> names the translator renders it with in Databricks SQL (None: unknown).
    function_translations: dict[str, tuple[str | None, ...]]

    def by_relation(self) -> dict[str, DataObject]:
        return {o.relation: o for o in self.objects}

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=False) + "\n"

    def write(self, path: Path) -> None:
        path.write_text(self.to_json(), encoding="utf-8")

    @classmethod
    def read(cls, path: Path) -> Inventory:
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Inventory:
        return cls(
            source_repository=raw["source_repository"],
            source_commit=raw["source_commit"],
            collected_at=raw["collected_at"],
            engine_version=raw["engine_version"],
            objects=tuple(_object(o) for o in raw["objects"]),
            access_rules=tuple(AccessRule(**r) for r in raw["access_rules"]),
            function_translations={k: tuple(v) for k, v in raw["function_translations"].items()},
        )


def _object(raw: dict[str, Any]) -> DataObject:
    volume = raw.get("volume")
    return DataObject(
        schema=raw["schema"],
        name=raw["name"],
        kind=ObjectKind(raw["kind"]),
        columns=tuple(Column(**c) for c in raw["columns"]),
        materialization=raw.get("materialization"),
        depends_on=tuple(raw.get("depends_on", ())),
        functions=dict(raw.get("functions", {})),
        macros=tuple(raw.get("macros", ())),
        tests=raw.get("tests", 0),
        volume=Volume(**volume) if volume else None,
    )
