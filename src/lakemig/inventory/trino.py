"""Live collection from the source query engine: every table, its columns and its volume."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from lakemig.inventory.model import Column, Volume

Query = Callable[[str], Sequence[Sequence[Any]]]

# Metadata schemas the engine adds to every catalog.
_ENGINE_SCHEMAS = ("information_schema", "system")
_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")


@dataclass(frozen=True)
class CatalogTable:
    schema: str
    name: str
    columns: tuple[Column, ...]
    volume: Volume


def engine_version(query: Query) -> str:
    return str(query("SELECT node_version FROM system.runtime.nodes WHERE coordinator")[0][0])


def collect_catalog(
    query: Query, catalog: str, *, iceberg: bool, schemas: Sequence[str] | None = None
) -> list[CatalogTable]:
    """Tables of one catalog. Iceberg catalogs also report data files and bytes from table metadata."""
    _check(catalog)
    wanted = None if schemas is None else {_check(s) for s in schemas}
    columns: dict[tuple[str, str], list[Column]] = {}
    rows = query(
        f"SELECT table_schema, table_name, column_name, data_type FROM {catalog}.information_schema.columns "  # noqa: S608
        "ORDER BY table_schema, table_name, ordinal_position"
    )
    for schema, table, column, data_type in rows:
        if schema not in _ENGINE_SCHEMAS and (wanted is None or schema in wanted):
            columns.setdefault((schema, table), []).append(Column(column, data_type))
    views = {
        (schema, table)
        for schema, table in query(
            f"SELECT table_schema, table_name FROM {catalog}.information_schema.tables WHERE table_type = 'VIEW'"  # noqa: S608
        )
    }
    tables = []
    for (schema, table), cols in sorted(columns.items()):
        if (schema, table) in views:
            continue
        tables.append(CatalogTable(schema, table, tuple(cols), _volume(query, catalog, schema, table, iceberg=iceberg)))
    return tables


def _volume(query: Query, catalog: str, schema: str, table: str, *, iceberg: bool) -> Volume:
    relation = f'{catalog}."{_check(schema)}"."{_check(table)}"'
    rows = int(query(f"SELECT count(*) FROM {relation}")[0][0])  # noqa: S608
    if not iceberg:
        return Volume(rows=rows, data_bytes=None, data_files=None)
    # content = 0 keeps data files: delete files are not data the target has to hold.
    files_relation = f'{catalog}."{schema}"."{table}$files"'
    data_bytes, data_files = query(
        f"SELECT coalesce(sum(file_size_in_bytes), 0), count(*) FROM {files_relation} WHERE content = 0"  # noqa: S608
    )[0]
    return Volume(rows=rows, data_bytes=int(data_bytes), data_files=int(data_files))


def _check(identifier: str) -> str:
    # Identifiers come from the engine's own information schema; anything unexpected is refused
    # rather than quoted into SQL.
    if not _IDENTIFIER.match(identifier):
        raise ValueError(f"unexpected identifier {identifier!r}")
    return identifier
