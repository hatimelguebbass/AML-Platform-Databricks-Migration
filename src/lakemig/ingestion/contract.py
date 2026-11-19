"""The landing contract: the tables and typed columns a delivery may carry into bronze.

It is frozen from the manifests of real exports (`lakemig contract`) and reviewed like code, so the
cloud never learns its schema from whatever arrives: a delivery that differs from the contract by
one column, type or nullability is rejected, never evolved into bronze. Every column crossing the
boundary was classified and allowed by the export policy; a column the contract does not know was
not, and must not land even in a rescue column (ADR 0004).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from lakemig.manifest import Manifest

CONTRACT = Path(__file__).with_name("landing_contract.json")
FORMAT_VERSION = 1

# Lineage the ingestion adds to every bronze row; an exported column must never take these names.
DELIVERY_ID = "_delivery_id"
DELIVERY_DATE = "_delivery_date"
DELIVERY_FILE = "_delivery_file"
LOADED_AT = "_bronze_loaded_at"
LINEAGE: tuple[tuple[str, str], ...] = (
    (DELIVERY_ID, "STRING"),
    (DELIVERY_DATE, "DATE"),
    (DELIVERY_FILE, "STRING"),
    (LOADED_AT, "TIMESTAMP"),
)

_INTEGERS = {"int8": "TINYINT", "int16": "SMALLINT", "int32": "INT", "int64": "BIGINT"}
_SIMPLE = {**_INTEGERS, "string": "STRING", "large_string": "STRING", "bool": "BOOLEAN", "double": "DOUBLE"}
_DECIMAL = re.compile(r"decimal128\((\d+), (\d+)\)")
# Spark reads microsecond and millisecond Parquet timestamps; nanoseconds would need a cast on read.
_TIMESTAMP = re.compile(r"timestamp\[(us|ms)(?:, tz=(.+))?\]")


class ContractError(ValueError):
    pass


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    nullable: bool

    @property
    def spark_type(self) -> str:
        return spark_type(self.type)


@dataclass(frozen=True)
class Contract:
    tables: Mapping[str, tuple[Column, ...]]

    @classmethod
    def read(cls, path: Path = CONTRACT) -> Contract:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("format_version") != FORMAT_VERSION:
            raise ContractError(f"{path}: contract format {raw.get('format_version')} is not {FORMAT_VERSION}")
        return cls({name: tuple(Column(**c) for c in columns) for name, columns in raw["tables"].items()})

    def write(self, path: Path = CONTRACT) -> None:
        tables = {name: [vars(c) for c in columns] for name, columns in sorted(self.tables.items())}
        text = json.dumps({"format_version": FORMAT_VERSION, "tables": tables}, indent=2)
        path.write_text(text + "\n", encoding="utf-8")

    @classmethod
    def from_manifests(cls, manifests: Iterable[Manifest]) -> Contract:
        """The contract the given deliveries agree on; refuses deliveries that disagree with each other."""
        tables: dict[str, tuple[Column, ...]] = {}
        seen: dict[str, str] = {}
        for manifest in manifests:
            for table in manifest.tables:
                columns = tuple(Column(c.name, c.type, c.nullable) for c in table.columns)
                if table.name in tables and tables[table.name] != columns:
                    raise ContractError(
                        f"{table.name}: {manifest.delivery_id} and {seen[table.name]} disagree on the columns"
                    )
                tables[table.name], seen[table.name] = columns, manifest.delivery_id
        if not tables:
            raise ContractError("no delivery to derive the contract from")
        contract = cls(tables)
        problems = contract.problems()
        if problems:
            raise ContractError("; ".join(problems))
        return contract

    def problems(self) -> list[str]:
        """What would make a bronze table impossible to create from this contract."""
        found = []
        lineage = {name for name, _ in LINEAGE}
        for table, columns in self.tables.items():
            for column in columns:
                if column.name in lineage:
                    found.append(f"{table}.{column.name}: reserved for the ingestion lineage")
                try:
                    spark_type(column.type)
                except ContractError as error:
                    found.append(f"{table}.{column.name}: {error}")
        return found

    def deviations(self, manifest: Manifest) -> list[str]:
        """How a delivery's declared tables and columns differ from the contract; empty if they match."""
        found = []
        delivered = {t.name: t for t in manifest.tables}
        for name in sorted(set(self.tables) - set(delivered)):
            found.append(f"table {name} is missing")
        for name in sorted(set(delivered) - set(self.tables)):
            found.append(f"table {name} is not in the landing contract")
        for name in sorted(set(self.tables) & set(delivered)):
            expected = {c.name: c for c in self.tables[name]}
            actual = {c.name: Column(c.name, c.type, c.nullable) for c in delivered[name].columns}
            table_found = [f"{name}.{c}: column is missing" for c in sorted(set(expected) - set(actual))]
            table_found += [
                f"{name}.{c}: column is not in the landing contract" for c in sorted(set(actual) - set(expected))
            ]
            for column in sorted(set(expected) & set(actual)):
                e, a = expected[column], actual[column]
                if e != a:
                    table_found.append(
                        f"{name}.{column}: {a.type} nullable={a.nullable}, contract {e.type} nullable={e.nullable}"
                    )
            if not table_found and list(actual) != list(expected):
                table_found.append(f"{name}: columns are in another order than the contract")
            found += table_found
        return found


def spark_type(arrow_type: str) -> str:
    """Spark SQL type that reads the Parquet column the export wrote with this Arrow type."""
    if arrow_type in _SIMPLE:
        return _SIMPLE[arrow_type]
    if arrow_type == "date32[day]":
        return "DATE"
    if decimal := _DECIMAL.fullmatch(arrow_type):
        return f"DECIMAL({decimal[1]},{decimal[2]})"
    if timestamp := _TIMESTAMP.fullmatch(arrow_type):
        # A Parquet timestamp without a zone is a wall-clock time (booking time at the branch): it stays
        # one in Spark, where TIMESTAMP would shift it by the session zone.
        return "TIMESTAMP" if timestamp[2] else "TIMESTAMP_NTZ"
    raise ContractError(f"Arrow type {arrow_type} has no Spark mapping in the landing contract")


def totals_kind(arrow_type: str) -> str | None:
    """Which control total the export computed for a column of this type (see export.delivery)."""
    if arrow_type in _INTEGERS or _DECIMAL.fullmatch(arrow_type):
        return "sum"
    if arrow_type == "date32[day]" or _TIMESTAMP.fullmatch(arrow_type):
        return "range"
    if arrow_type in {"string", "large_string", "bool"}:
        return "distinct"
    return None
