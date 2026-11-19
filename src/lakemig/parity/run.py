"""The parity run, on premises: the source read from its lakehouse, the target through the SQL warehouse.

Only fingerprints and the rows of differing buckets come back from the target; nothing of the source
leaves the premises. Direct identifiers are never read: the source columns listed as kept on premises
are left out of every scan.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence

import pyarrow as pa
from pyiceberg.expressions import EqualTo

from lakemig.export.pseudonym import Key, pseudonymize
from lakemig.export.source import Source
from lakemig.parity import canonical, compare, fingerprint
from lakemig.parity.canonical import ColumnType
from lakemig.parity.compare import SliceResult
from lakemig.parity.fingerprint import Fingerprint, Layout
from lakemig.parity.report import TableResult
from lakemig.parity.spec import Spec, TableSpec, reconcile

log = logging.getLogger(__name__)

Rows = list[list[str | None]]
Query = Callable[[str], Rows]
# A slice whose buckets mostly differ is wrong as a whole: a sample of its buckets names the cause.
# The rows read stay well under the warehouse's 25 MiB inline result limit.
MAX_INSPECTED_BUCKETS = 16
MAX_INSPECTED_ROWS = 20_000


class ParitySetupError(RuntimeError):
    pass


def target_types(query: Query, catalog: str, relation: str) -> dict[str, ColumnType]:
    schema, table = relation.split(".")
    rows = query(
        f"select column_name, full_data_type from {canonical.quote(catalog)}.information_schema.columns "
        f"where table_schema = '{schema}' and table_name = '{table}' order by ordinal_position"
    )
    if not rows:
        raise ParitySetupError(f"{catalog}.{relation}: no such table, or this identity cannot read it")
    return {str(name): ColumnType.parse(str(data_type)) for name, data_type in rows}


def compare_table(
    table: TableSpec, spec: Spec, source: Source, query: Query, key: Key, catalog: str, dates: Sequence[str]
) -> TableResult:
    types = target_types(query, catalog, table.relation)
    columns = reconcile(table, spec.lineage, source.columns(table.relation), list(types))
    if columns.problems:
        raise ParitySetupError("; ".join(columns.problems))
    layout = Layout(columns.compared, {c: types[c] for c in columns.compared}, table.key, table.tolerance)
    target_relation = f"{catalog}.{table.relation}"
    slices: list[str | None] = list(dates) if table.slice else [None]
    profiles = fingerprint.parse_profiles(
        query(fingerprint.profile_sql(target_relation, layout, table.slice, dates)), layout
    )
    buckets = fingerprint.parse_buckets(query(fingerprint.buckets_sql(target_relation, layout, table.slice, dates)))
    unexpected_slices = set(profiles) - set(slices)
    if unexpected_slices:
        raise ParitySetupError(f"{target_relation}: slices {sorted(map(str, unexpected_slices))} outside the run")
    results = []
    for slice_value in slices:
        if slice_value in profiles:
            rows, keys, profile = profiles[slice_value]
            target = Fingerprint(rows, keys, profile, buckets.get(slice_value, {}))
        else:
            target = Fingerprint.empty(layout)
        results.append(_compare_slice(table, layout, source, query, key, target_relation, slice_value, target))
        log.info("%s %s: %s", table.relation, slice_value or "", "equal" if results[-1].equal else "DIFFERS")
    return TableResult(table.relation, table.key, table.slice, layout.columns, tuple(results))


def _compare_slice(
    table: TableSpec,
    layout: Layout,
    source: Source,
    query: Query,
    key: Key,
    target_relation: str,
    slice_value: str | None,
    target: Fingerprint,
) -> SliceResult:
    # pyiceberg binds the literal to the column type at scan time; its stubs only know keyword forms.
    row_filter = EqualTo(table.slice, slice_value) if table.slice and slice_value else None  # type: ignore[call-arg, arg-type]
    rows = _pseudonymized(source.read(table.relation, layout.columns, row_filter), table.pseudonymize, key)
    if rows.num_rows:
        source_print, members, by_key = fingerprint.of_arrow(rows, layout)
    else:
        source_print, members, by_key = Fingerprint.empty(layout), {}, {}
    differing = compare.differing_buckets(source_print.buckets, target.buckets)
    inspected = _sample(differing, source_print, target)
    differences = None
    if inspected:
        target_rows = fingerprint.parse_drill(
            query(fingerprint.drill_sql(target_relation, layout, table.slice, slice_value, inspected))
        )
        source_rows = {k: by_key[k] for b in inspected for k in members.get(b, ())}
        differences = compare.compare_rows(source_rows, target_rows, layout)
    return compare.judge(slice_value, source_print, target, layout, inspected, differences)


def _sample(differing: Sequence[str], source: Fingerprint, target: Fingerprint) -> list[str]:
    """The first differing buckets, as many as the row budget allows, and always one."""
    chosen: list[str] = []
    rows = 0
    for bucket in differing[:MAX_INSPECTED_BUCKETS]:
        size = max(source.buckets.get(bucket, (0, 0))[0], target.buckets.get(bucket, (0, 0))[0])
        if chosen and rows + size > MAX_INSPECTED_ROWS:
            break
        chosen.append(bucket)
        rows += size
    return chosen


def _pseudonymized(rows: pa.Table, namespaces: Mapping[str, str], key: Key) -> pa.Table:
    for column, namespace in namespaces.items():
        index = rows.schema.get_field_index(column)
        rows = rows.set_column(index, column, pseudonymize(rows.column(column), key, namespace))
    return rows


def run(
    spec: Spec, source: Source, query: Query, key: Key, catalog: str, dates: Sequence[str]
) -> tuple[TableResult, ...]:
    return tuple(compare_table(t, spec, source, query, key, catalog, dates) for t in spec.tables)


def publications(query: Query, catalog: str, dates: Sequence[str]) -> dict[str, str]:
    """The publication each date was last read from: the latest of that date's or of every date's."""
    rows = query(
        "select coalesce(cast(business_date as string), 'every date'), max_by(run_id, published_at), max(published_at) "
        f"from {canonical.quote(catalog)}.observability.publication group by 1"
    )
    latest = {str(d): (str(run_id), str(at)) for d, run_id, at in rows}
    missing = [d for d in dates if d not in latest and "every date" not in latest]
    if missing:
        raise ParitySetupError(f"not published on the target: {', '.join(missing)}; run make daily first")
    chosen = {}
    for day in dates:
        candidates = [latest[k] for k in (day, "every date") if k in latest]
        chosen[day] = max(candidates, key=lambda c: c[1])[0]
    return chosen
