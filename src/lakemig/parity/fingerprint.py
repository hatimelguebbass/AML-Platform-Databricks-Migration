"""Fingerprints of a slice of a table: a profile of each column and a digest of the rows, bucket by bucket.

Each row's canonical text is hashed with SHA-256 and the row lands in one of 256 buckets chosen by
the hash of its key, so that a changed row falls in the same bucket on both sides. A bucket's digest
is its row count and the sum of the first 60 bits of its rows' hashes. Equal digests stand for equal
rows; only the buckets whose digests differ are read row by row (`drill_*`), which keeps the rows
where they are and still names every missing, unexpected or changed row of an inspected bucket.

The source side is computed here with Arrow from the source's rows; the target side is three SQL
statements, built here, run on the warehouse and parsed back into the same shapes.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

import pyarrow as pa
import pyarrow.compute as pc

from lakemig.parity import canonical
from lakemig.parity.canonical import ColumnType, Kind

BUCKET_HEX = 2
DIGEST_HEX = 15


@dataclass(frozen=True)
class ColumnProfile:
    nulls: int
    # Distinct canonical values; not taken on a column compared with a tolerance.
    distinct: int | None = None
    total: Decimal | None = None
    low: str | None = None
    high: str | None = None


@dataclass(frozen=True)
class Layout:
    """The compared columns of a table in the target's order, their types and how they are rendered."""

    columns: tuple[str, ...]
    types: Mapping[str, ColumnType]
    key: tuple[str, ...]
    tolerance: Mapping[str, float]

    def digits(self, column: str) -> int:
        return canonical.digits_for(self.tolerance.get(column))


@dataclass(frozen=True)
class Fingerprint:
    rows: int
    keys: int
    columns: Mapping[str, ColumnProfile]
    # Bucket -> (rows, digest).
    buckets: Mapping[str, tuple[int, int]]

    @classmethod
    def empty(cls, layout: Layout) -> Fingerprint:
        """A slice one side does not have."""
        return cls(0, 0, {c: ColumnProfile(0, None if c in layout.tolerance else 0) for c in layout.columns}, {})


@dataclass(frozen=True)
class Row:
    digest: str
    values: tuple[str | None, ...]


# Source side ----------------------------------------------------------------------------------------


def of_arrow(table: pa.Table, layout: Layout) -> tuple[Fingerprint, dict[str, list[str]], dict[str, Row]]:
    """The fingerprint of the source's rows, with the keys of each bucket and every row by key.

    `table` holds the compared columns with the source's values already pseudonymized where the spec
    says so.
    """
    texts = {c: canonical.arrow(table.column(c), layout.types[c], layout.digits(c)) for c in layout.columns}
    profiles = {c: _arrow_profile(table.column(c), texts[c], layout, c) for c in layout.columns}
    keys = canonical.arrow_row([texts[c] for c in layout.key]).to_pylist()
    rows = canonical.arrow_row([texts[c] for c in layout.columns]).to_pylist()
    columns = [texts[c].to_pylist() for c in layout.columns]
    buckets: dict[str, list[int]] = {}
    members: dict[str, list[str]] = {}
    by_key: dict[str, Row] = {}
    for index, (key, row) in enumerate(zip(keys, rows, strict=True)):
        bucket = _sha256(key)[:BUCKET_HEX]
        digest = _sha256(row)
        counts = buckets.setdefault(bucket, [0, 0])
        counts[0] += 1
        counts[1] += int(digest[:DIGEST_HEX], 16)
        members.setdefault(bucket, []).append(key)
        by_key[key] = Row(digest, tuple(values[index] for values in columns))
    fingerprint = Fingerprint(
        rows=table.num_rows,
        keys=len(by_key),
        columns=profiles,
        buckets={b: (n, d) for b, (n, d) in buckets.items()},
    )
    return fingerprint, members, by_key


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _arrow_profile(values: pa.ChunkedArray, texts: pa.Array, layout: Layout, column: str) -> ColumnProfile:
    column_type = layout.types[column]
    nulls = values.null_count
    distinct = None if column in layout.tolerance else pc.count_distinct(texts, mode="only_valid").as_py()
    total = low = high = None
    if column_type.numeric:
        typed = canonical.arrow(values, column_type)
        valid = [Decimal(v) for v in typed.to_pylist() if v is not None]
        total = sum(valid, Decimal(0)) if valid else None
    elif column_type.ordered and len(values) > nulls:
        # Fixed-width renderings order like the values, except instants' epoch microseconds.
        if column_type.kind is Kind.INSTANT:
            micros = [int(v) for v in texts.to_pylist() if v is not None]
            low, high = str(min(micros)), str(max(micros))
        else:
            extremes = pc.min_max(texts)
            low, high = extremes["min"].as_py(), extremes["max"].as_py()
    return ColumnProfile(nulls, distinct, total, low, high)


# Target side ----------------------------------------------------------------------------------------


def _qualified(relation: str) -> str:
    return ".".join(canonical.quote(part) for part in relation.split("."))


def _where(slice_column: str | None, slices: Sequence[str]) -> str:
    if slice_column is None:
        return ""
    values = ", ".join(f"date'{s}'" for s in slices)
    return f" where {canonical.quote(slice_column)} in ({values})"


def _slice_expression(slice_column: str | None) -> str:
    return f"cast({canonical.quote(slice_column)} as string)" if slice_column else "cast(null as string)"


def _key_row(layout: Layout) -> str:
    return canonical.sql_row([canonical.sql(c, layout.types[c], layout.digits(c)) for c in layout.key])


def _row(layout: Layout) -> str:
    return canonical.sql_row([canonical.sql(c, layout.types[c], layout.digits(c)) for c in layout.columns])


def profile_sql(relation: str, layout: Layout, slice_column: str | None, slices: Sequence[str]) -> str:
    """One row per slice: slice, rows, distinct keys, then each column's metrics in `profile_metrics` order."""
    selected = [_slice_expression(slice_column) + " as s", "count(*)", f"count(distinct {_key_row(layout)})"]
    for column in layout.columns:
        column_type = layout.types[column]
        ref = canonical.quote(column)
        selected.append(f"count_if({ref} is null)")
        if column not in layout.tolerance:
            selected.append(f"count(distinct {canonical.sql(column, column_type, layout.digits(column))})")
        if column_type.numeric:
            selected.append(f"cast(sum({ref}) as string)")
        elif column_type.ordered:
            selected.append(canonical.expression(f"min({ref})", column_type, 0))
            selected.append(canonical.expression(f"max({ref})", column_type, 0))
    return f"select {', '.join(selected)} from {_qualified(relation)}{_where(slice_column, slices)} group by 1"


def parse_profiles(
    rows: Sequence[Sequence[str | None]], layout: Layout
) -> dict[str | None, tuple[int, int, dict[str, ColumnProfile]]]:
    parsed = {}
    for row in rows:
        values = iter(row[3:])
        columns = {}
        for column in layout.columns:
            column_type = layout.types[column]
            nulls = int(next(values) or 0)
            distinct = None if column in layout.tolerance else int(next(values) or 0)
            total = low = high = None
            if column_type.numeric:
                text = next(values)
                total = Decimal(text) if text is not None else None
            elif column_type.ordered:
                low, high = next(values), next(values)
            columns[column] = ColumnProfile(nulls, distinct, total, low, high)
        parsed[row[0]] = (int(row[1] or 0), int(row[2] or 0), columns)
    return parsed


def buckets_sql(relation: str, layout: Layout, slice_column: str | None, slices: Sequence[str]) -> str:
    """One row per slice and bucket: slice, bucket, rows, digest."""
    hashed = (
        f"select {_slice_expression(slice_column)} as s, substr(sha2({_key_row(layout)}, 256), 1, {BUCKET_HEX}) as b, "
        f"sha2({_row(layout)}, 256) as h from {_qualified(relation)}{_where(slice_column, slices)}"
    )
    digest = f"cast(sum(cast(conv(substr(h, 1, {DIGEST_HEX}), 16, 10) as decimal(38, 0))) as string)"
    return f"select s, b, count(*), {digest} from ({hashed}) group by s, b"


def parse_buckets(rows: Sequence[Sequence[str | None]]) -> dict[str | None, dict[str, tuple[int, int]]]:
    parsed: dict[str | None, dict[str, tuple[int, int]]] = {}
    for slice_value, bucket, count, digest in rows:
        parsed.setdefault(slice_value, {})[str(bucket)] = (int(count or 0), int(digest or 0))
    return parsed


def drill_sql(
    relation: str, layout: Layout, slice_column: str | None, slice_value: str | None, buckets: Sequence[str]
) -> str:
    """Every row of the given buckets of one slice: key text, row hash, then each compared column's text."""
    texts = [canonical.sql(c, layout.types[c], layout.digits(c)) for c in layout.columns]
    in_buckets = ", ".join(f"'{b}'" for b in buckets)
    where = f"substr(sha2({_key_row(layout)}, 256), 1, {BUCKET_HEX}) in ({in_buckets})"
    if slice_column is not None:
        where = f"{canonical.quote(slice_column)} = date'{slice_value}' and {where}"
    selected = [_key_row(layout), f"sha2({_row(layout)}, 256)", *texts]
    return f"select {', '.join(selected)} from {_qualified(relation)} where {where}"


def parse_drill(rows: Sequence[Sequence[str | None]]) -> dict[str, Row]:
    return {str(row[0]): Row(str(row[1]), tuple(row[2:])) for row in rows}
