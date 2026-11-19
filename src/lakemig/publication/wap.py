"""Staging and publication of the transformed layers (ADR 0006).

The source published a business date by merging its Nessie branch `etl` into `main` once the
audits passed. Delta has no branches, and Unity Catalog refuses both shallow clones and time travel
on tables with column masks, so the staging copy of a layer is a set of ordinary tables, kept in
step with the published layer one business date at a time:

- dated tables (a `business_date` column, one slice per date) carry the pipeline run that wrote
  each slice. Opening the staging makes every slice whose run differs from the published one equal
  to it again, so a build reads audited history only, whatever a failed run left behind. In steady
  state that touches nothing; the first run copies the history once.
- whole tables are rebuilt by every run and need no reset.

Publishing copies, table by table, what the run built and the audits passed: the slice of the
business date with `REPLACE WHERE`, or the whole table with `INSERT OVERWRITE`, each one atomic
commit. Every check (schema, masks, rows of this run only) is made on every table before the first
write. A published table takes the liquid clustering its model declares, and is reclustered in full
once when that changes (ADR 0009). Delta has no transaction across tables, so a reader can see one
table published before the next: `observability.publication` records each publication once all its
tables are written.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

Sql = Callable[[str], Sequence[Sequence[Any]]]

SLICE_COLUMN = "business_date"
RUN_COLUMN = "_pipeline_run_id"
STAGING_SUFFIX = "_staging"
PUBLICATION_LOG = "observability.publication"
_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]*")
_RUN_ID = re.compile(r"[A-Za-z0-9_-]+")


class SchemaChangedError(RuntimeError):
    """Staging and published tables differ in columns: a model changed and needs a full rebuild."""


class GovernanceDriftError(RuntimeError):
    """A published column is masked by another function than its model declares."""


class StaleStagingError(RuntimeError):
    """Rows to publish were not built by the run being published."""


def identifier(name: str) -> str:
    # Names reach SQL as identifiers: only the plain lower-case names Unity Catalog stores are accepted.
    if not _IDENTIFIER.fullmatch(name):
        raise ValueError(f"not a plain identifier: {name!r}")
    return f"`{name}`"


def literal(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


@dataclass(frozen=True)
class Table:
    catalog: str
    schema: str
    name: str

    def __str__(self) -> str:
        return ".".join(identifier(part) for part in (self.catalog, self.schema, self.name))

    def staging(self) -> Table:
        return Table(self.catalog, self.schema + STAGING_SUFFIX, self.name)


@dataclass(frozen=True)
class Mask:
    column: str
    function: str
    using_columns: str | None


@dataclass(frozen=True)
class Published:
    table: str
    rows: int
    version: int


class Lakehouse:
    """The few catalog reads and writes staging and publication need, over any SQL runner."""

    def __init__(self, sql: Sql, catalog: str) -> None:
        identifier(catalog)
        self.sql = sql
        self.catalog = catalog

    def tables(self, schema: str) -> list[Table]:
        rows = self.sql(
            f"SELECT table_name FROM {identifier(self.catalog)}.information_schema.tables "
            f"WHERE table_schema = {literal(schema)} AND table_type <> 'VIEW' ORDER BY table_name"
        )
        return [Table(self.catalog, schema, row[0]) for row in rows]

    def columns(self, table: Table) -> tuple[tuple[str, str], ...]:
        rows = self.sql(
            f"SELECT column_name, full_data_type FROM {identifier(self.catalog)}.information_schema.columns "
            f"WHERE table_schema = {literal(table.schema)} AND table_name = {literal(table.name)} "
            "ORDER BY ordinal_position"
        )
        return tuple((row[0], row[1]) for row in rows)

    def slice_runs(self, table: Table) -> dict[date, str | None]:
        """The run that wrote each slice; None for a slice mixing runs, which no single run explains."""
        rows = self.sql(
            f"SELECT {SLICE_COLUMN}, min({RUN_COLUMN}), max({RUN_COLUMN}), count_if({RUN_COLUMN} IS NULL) "
            f"FROM {table} GROUP BY {SLICE_COLUMN}"
        )
        return {row[0]: row[1] if row[1] == row[2] and not row[3] else None for row in rows}

    def masks(self, table: Table) -> set[Mask]:
        rows = self.sql(
            "SELECT column_name, mask_name, using_columns FROM system.information_schema.column_masks "
            f"WHERE table_catalog = {literal(table.catalog)} AND table_schema = {literal(table.schema)} "
            f"AND table_name = {literal(table.name)}"
        )
        return {Mask(row[0], row[1], row[2] or None) for row in rows}

    def set_mask(self, table: Table, mask: Mask) -> None:
        using = f" USING COLUMNS ({mask.using_columns})" if mask.using_columns else ""
        self.sql(f"ALTER TABLE {table} ALTER COLUMN {identifier(mask.column)} SET MASK {mask.function}{using}")

    def copy_governance(self, source: Table, target: Table) -> None:
        """Masks, tags and comments of a table built by dbt onto its new published copy, before any row."""
        where = (
            f"catalog_name = {literal(source.catalog)} AND schema_name = {literal(source.schema)} "
            f"AND table_name = {literal(source.name)}"
        )
        for mask in sorted(self.masks(source), key=lambda m: m.column):
            self.set_mask(target, mask)
        column_tags: dict[str, list[str]] = {}
        for column, name, value in self.sql(
            f"SELECT column_name, tag_name, tag_value FROM system.information_schema.column_tags WHERE {where}"
        ):
            column_tags.setdefault(column, []).append(f"{literal(name)} = {literal(value or '')}")
        for column, tags in sorted(column_tags.items()):
            self.sql(f"ALTER TABLE {target} ALTER COLUMN {identifier(column)} SET TAGS ({', '.join(sorted(tags))})")
        table_tags = [
            f"{literal(name)} = {literal(value or '')}"
            for name, value in self.sql(
                f"SELECT tag_name, tag_value FROM system.information_schema.table_tags WHERE {where}"
            )
        ]
        if table_tags:
            self.sql(f"ALTER TABLE {target} SET TAGS ({', '.join(sorted(table_tags))})")
        comment = self.sql(
            f"SELECT comment FROM {identifier(self.catalog)}.information_schema.tables "
            f"WHERE table_schema = {literal(source.schema)} AND table_name = {literal(source.name)}"
        )
        if comment and comment[0][0]:
            self.sql(f"COMMENT ON TABLE {target} IS {literal(comment[0][0])}")
        for column, text in self.sql(
            f"SELECT column_name, comment FROM {identifier(self.catalog)}.information_schema.columns "
            f"WHERE table_schema = {literal(source.schema)} AND table_name = {literal(source.name)} "
            "AND comment IS NOT NULL"
        ):
            self.sql(f"ALTER TABLE {target} ALTER COLUMN {identifier(column)} COMMENT {literal(text)}")

    def rows_to_publish(self, table: Table, business_date: date | None, run_id: str | None) -> tuple[int, int]:
        """Rows of the slice (or table) and how many of them another run than `run_id` wrote."""
        scope = f"{SLICE_COLUMN} = DATE'{business_date.isoformat()}'" if business_date else "true"
        foreign = f"count_if({RUN_COLUMN} IS DISTINCT FROM {literal(run_id)})" if run_id else "0"
        rows, others = self.sql(f"SELECT count(*), {foreign} FROM {table} WHERE {scope}")[0]
        return int(rows), int(others)

    def clustering(self, table: Table) -> tuple[str, ...]:
        """Liquid clustering columns of a table, in order; empty for a table without clustering."""
        rows = self.sql(f"SHOW TBLPROPERTIES {table} ('clusteringColumns')")
        try:
            paths = json.loads(rows[0][1]) if rows else []
        except (TypeError, ValueError):
            # A table never clustered answers that it does not have the property.
            return ()
        return tuple(".".join(path) for path in paths)

    def cluster(self, table: Table, columns: Sequence[str]) -> None:
        """Change the clustering columns and recluster every file: OPTIMIZE alone only clusters new data."""
        keys = f"({', '.join(identifier(c) for c in columns)})" if columns else "NONE"
        self.sql(f"ALTER TABLE {table} CLUSTER BY {keys}")
        if columns:
            self.sql(f"OPTIMIZE {table} FULL")

    def version(self, table: Table) -> int:
        return int(self.sql(f"DESCRIBE HISTORY {table} LIMIT 1")[0][0])


def diverged(staging: Mapping[date, str | None], published: Mapping[date, str | None]) -> list[date]:
    """Dates whose staging slice is not the published one, including slices on one side only."""
    return sorted(
        d for d in staging.keys() | published.keys() if staging.get(d) is None or staging.get(d) != published.get(d)
    )


def _dates(dates: Iterable[date]) -> str:
    return ", ".join(f"DATE'{d.isoformat()}'" for d in dates)


def open_staging(lake: Lakehouse, layers: Sequence[str]) -> dict[str, list[date]]:
    """Make the dated staging tables hold exactly the published slices; returns the dates reset per table."""
    reset: dict[str, list[date]] = {}
    for layer in layers:
        published_tables = {t.name: t for t in lake.tables(layer)}
        for published in published_tables.values():
            if not _built_by_slices(lake, published):
                continue
            staging = published.staging()
            lake.sql(f"CREATE TABLE IF NOT EXISTS {staging} LIKE {published}")
            if lake.columns(staging) != lake.columns(published):
                raise SchemaChangedError(f"{staging} and {published} differ in columns: {_REBUILD}")
            dates = diverged(lake.slice_runs(staging), lake.slice_runs(published))
            if dates:
                lake.sql(
                    f"INSERT INTO {staging} REPLACE WHERE {SLICE_COLUMN} IN ({_dates(dates)}) "
                    f"SELECT * FROM {published} WHERE {SLICE_COLUMN} IN ({_dates(dates)})"
                )
                reset[str(staging)] = dates
        for staging in lake.tables(layer + STAGING_SUFFIX):
            # A dated table never published holds nothing audited: its history is rebuilt in full.
            if staging.name not in published_tables and _built_by_slices(lake, staging):
                lake.sql(f"DELETE FROM {staging}")
                reset[str(staging)] = []
    return reset


def _built_by_slices(lake: Lakehouse, table: Table) -> bool:
    """A table the pipeline writes one business date at a time. A layer can hold other dated tables,
    such as the publication log in observability, which no run builds and staging must never copy."""
    columns = dict(lake.columns(table))
    return SLICE_COLUMN in columns and RUN_COLUMN in columns


_REBUILD = "a model changed; rebuild every date (the daily job without business_date), as the runbook describes"


@dataclass(frozen=True)
class _Step:
    staging: Table
    published: Table
    action: str  # create, recreate or write
    sliced: bool
    rows: int
    missing_masks: frozenset[Mask]
    clustering: tuple[str, ...]


def publish(lake: Lakehouse, layers: Sequence[str], business_date: date | None, run_id: str) -> list[Published]:
    """Publish what run `run_id` built and audited: the business date's slices, or everything without one.

    Without a business date (a rebuild), a published table whose columns changed is dropped and
    created again: readers miss it for the seconds between, which is why only a rebuild may do it.
    """
    if not _RUN_ID.fullmatch(run_id):
        raise ValueError(f"not a run id: {run_id!r}")
    steps = [
        _plan(lake, staging, business_date, run_id)
        for layer in layers
        for staging in lake.tables(layer + STAGING_SUFFIX)
    ]
    published: list[Published] = []
    for step in steps:
        if step.action == "recreate":
            lake.sql(f"DROP TABLE {step.published}")
        if step.action in ("create", "recreate"):
            lake.sql(f"CREATE TABLE {step.published} LIKE {step.staging}")
            lake.copy_governance(step.staging, step.published)
        for mask in sorted(step.missing_masks, key=lambda m: m.column):
            lake.set_mask(step.published, mask)
        if step.sliced and business_date is not None:
            scope = f"{SLICE_COLUMN} = DATE'{business_date.isoformat()}'"
            lake.sql(f"INSERT INTO {step.published} REPLACE WHERE {scope} SELECT * FROM {step.staging} WHERE {scope}")
        else:
            lake.sql(f"INSERT OVERWRITE {step.published} SELECT * FROM {step.staging}")
        # After the rows, so the full reclustering also covers what this publication wrote.
        if lake.clustering(step.published) != step.clustering:
            lake.cluster(step.published, step.clustering)
        published.append(Published(str(step.published), step.rows, lake.version(step.published)))
    _record(lake, business_date, run_id, published)
    return published


def _plan(lake: Lakehouse, staging: Table, business_date: date | None, run_id: str) -> _Step:
    published = Table(staging.catalog, staging.schema.removesuffix(STAGING_SUFFIX), staging.name)
    columns = lake.columns(staging)
    action, missing = "create", frozenset[Mask]()
    if any(t.name == published.name for t in lake.tables(published.schema)):
        action = "write"
        if lake.columns(published) != columns:
            if business_date is not None:
                raise SchemaChangedError(f"{staging} and {published} differ in columns: {_REBUILD}")
            action = "recreate"
        else:
            missing = _missing_masks(lake.masks(staging), lake.masks(published), published)
    names = {name for name, _ in columns}
    sliced = SLICE_COLUMN in names
    rows, foreign = lake.rows_to_publish(
        staging, business_date if sliced else None, run_id if RUN_COLUMN in names else None
    )
    if foreign:
        raise StaleStagingError(f"{staging}: {foreign} of {rows} rows to publish were not built by run {run_id}")
    return _Step(staging, published, action, sliced, int(rows), missing, lake.clustering(staging))


def _missing_masks(staging: set[Mask], published: set[Mask], table: Table) -> frozenset[Mask]:
    """Masks a model gained are added before its rows are published; a changed mask is never applied
    unreviewed, as it could let more readers through."""
    by_column = {m.column: m for m in published}
    changed = sorted(m.column for m in staging if m.column in by_column and by_column[m.column] != m)
    if changed:
        raise GovernanceDriftError(f"{table}: column masks differ from the model's on {', '.join(changed)}")
    return frozenset(m for m in staging if m.column not in by_column)


def _record(lake: Lakehouse, business_date: date | None, run_id: str, published: Sequence[Published]) -> None:
    log = Table(lake.catalog, *PUBLICATION_LOG.split("."))
    lake.sql(
        f"CREATE TABLE IF NOT EXISTS {log} (business_date DATE, run_id STRING NOT NULL, "
        "published_at TIMESTAMP NOT NULL, tables ARRAY<STRUCT<name: STRING, rows: BIGINT, version: BIGINT>> NOT NULL) "
        "COMMENT 'One row per publication of the transformed layers; a null business_date republished every date.'"
    )
    tables = ", ".join(
        f"named_struct('name', {literal(p.table)}, 'rows', {p.rows}, 'version', {p.version})" for p in published
    )
    day = f"DATE'{business_date.isoformat()}'" if business_date else "CAST(NULL AS DATE)"
    lake.sql(f"INSERT INTO {log} VALUES ({day}, {literal(run_id)}, current_timestamp(), array({tables}))")
