"""One text form per value, produced identically by Arrow on premises and by Databricks SQL in the cloud.

Both sides hash rows they cannot exchange: the source holds direct identifiers that never cross, and
moving millions of target rows on premises would turn a check into a transfer. Each value is
therefore rendered to text the same way by both engines, keyed by the target column's type, and the
two renderings of each type sit next to each other here so that a change to one is reviewed with
the other. `tests/integration/test_parity_canonical.py` runs both on the same values.

Doubles are rendered rounded to a number of decimals: the engines print binary floats differently,
and a column with a tolerance must hash equal when its values are within it. Rounding at
`digits_for(tolerance)` decimals keeps every difference larger than the tolerance visible in the
hash; the comparison of the differing rows then decides on the values themselves.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

import pyarrow as pa
import pyarrow.compute as pc

NULL = "\\N"
FIELD_SEPARATOR = "\x1f"
ELEMENT_SEPARATOR = "\x1e"
# Decimals a double without a tolerance is compared at.
EXACT_DOUBLE_DIGITS = 12
TIMESTAMP_FORMAT_SQL = "yyyy-MM-dd HH:mm:ss.SSSSSS"
# Arrow prints the fraction of a microsecond timestamp as part of %S.
TIMESTAMP_FORMAT_ARROW = "%Y-%m-%d %H:%M:%S"


class Kind(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    DECIMAL = "decimal"
    DOUBLE = "double"
    BOOLEAN = "boolean"
    DATE = "date"
    # Wall-clock time without a zone (TIMESTAMP_NTZ): compared as printed.
    LOCAL_TIMESTAMP = "local_timestamp"
    # An instant (TIMESTAMP): compared as microseconds since the epoch, whatever the session zone.
    INSTANT = "instant"
    ARRAY = "array"


class UnsupportedTypeError(ValueError):
    pass


_INTEGERS = {"tinyint", "smallint", "int", "integer", "bigint"}
_SIMPLE = {
    "string": Kind.STRING,
    "double": Kind.DOUBLE,
    "float": Kind.DOUBLE,
    "boolean": Kind.BOOLEAN,
    "date": Kind.DATE,
    "timestamp_ntz": Kind.LOCAL_TIMESTAMP,
    "timestamp": Kind.INSTANT,
}
_DECIMAL = re.compile(r"decimal\((\d+),\s*(\d+)\)")
_ARRAY = re.compile(r"array<(.+)>")


@dataclass(frozen=True)
class ColumnType:
    """A column's type on the target, as Unity Catalog's information schema writes it."""

    kind: Kind
    precision: int = 0
    scale: int = 0
    element: ColumnType | None = None

    @classmethod
    def parse(cls, full_data_type: str) -> ColumnType:
        text = full_data_type.strip().lower()
        if text in _INTEGERS:
            return cls(Kind.INTEGER)
        if text in _SIMPLE:
            return cls(_SIMPLE[text])
        if match := _DECIMAL.fullmatch(text):
            return cls(Kind.DECIMAL, int(match[1]), int(match[2]))
        if match := _ARRAY.fullmatch(text):
            element = cls.parse(match[1])
            if element.kind not in (Kind.STRING, Kind.INTEGER):
                raise UnsupportedTypeError(f"array of {match[1]}: only arrays of strings or integers are compared")
            return cls(Kind.ARRAY, element=element)
        raise UnsupportedTypeError(f"no canonical form for {full_data_type}")

    @property
    def numeric(self) -> bool:
        return self.kind in (Kind.INTEGER, Kind.DECIMAL)

    @property
    def ordered(self) -> bool:
        return self.kind in (Kind.DATE, Kind.LOCAL_TIMESTAMP, Kind.INSTANT)


def digits_for(tolerance: float | None) -> int:
    """Decimals that make any two doubles further apart than `tolerance` print differently.

    Each engine rounds within half a unit of the last decimal, and not necessarily the same way at
    ties: two units must fit in the tolerance.
    """
    if tolerance is None or tolerance == 0:
        return EXACT_DOUBLE_DIGITS
    return max(0, math.ceil(-math.log10(tolerance / 2)))


def quote(identifier: str) -> str:
    return "`" + identifier.replace("`", "``") + "`"


def sql(column: str, column_type: ColumnType, digits: int = EXACT_DOUBLE_DIGITS) -> str:
    """Databricks SQL expression of the column's canonical text; null where the value is null."""
    ref = quote(column)
    return expression(ref, column_type, digits)


def expression(ref: str, column_type: ColumnType, digits: int) -> str:
    """Canonical text of a SQL expression of the given type (a column, or an aggregate of it)."""
    match column_type.kind:
        case Kind.STRING:
            return ref
        case Kind.INTEGER | Kind.DECIMAL | Kind.BOOLEAN | Kind.DATE:
            return f"cast({ref} as string)"
        case Kind.DOUBLE:
            return f"cast(cast({ref} as decimal(38, {digits})) as string)"
        case Kind.LOCAL_TIMESTAMP:
            return f"date_format({ref}, '{TIMESTAMP_FORMAT_SQL}')"
        case Kind.INSTANT:
            return f"cast(unix_micros({ref}) as string)"
        case Kind.ARRAY:
            if column_type.element is None:
                raise UnsupportedTypeError("an array without an element type")
            element = expression("e", column_type.element, digits)
            return f"array_join(transform({ref}, e -> {element}), char(30), '{_sql_escape(NULL)}')"


def _sql_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("'", "\\'")


def sql_row(expressions: list[str]) -> str:
    """Canonical text of a row: fields joined by the unit separator, nulls written as \\N."""
    fields = ", ".join(f"coalesce({e}, '{_sql_escape(NULL)}')" for e in expressions)
    return f"concat_ws(char(31), {fields})"


def arrow(values: pa.Array | pa.ChunkedArray, column_type: ColumnType, digits: int = EXACT_DOUBLE_DIGITS) -> pa.Array:
    """The canonical text of each value, as `sql` renders it on the target; nulls stay null."""
    if isinstance(values, pa.ChunkedArray):
        values = values.combine_chunks()
    match column_type.kind:
        case Kind.STRING:
            return values.cast(pa.string())
        case Kind.INTEGER:
            return values.cast(pa.int64()).cast(pa.string())
        case Kind.DECIMAL:
            target = pa.decimal128(column_type.precision, column_type.scale)
            if pa.types.is_floating(values.type):
                return pa.array([_decimal_text(v, column_type.scale) for v in values.to_pylist()], pa.string())
            return values.cast(target).cast(pa.string())
        case Kind.BOOLEAN | Kind.DATE:
            return values.cast(pa.string())
        case Kind.DOUBLE:
            return pa.array([_decimal_text(v, digits) for v in values.cast(pa.float64()).to_pylist()], pa.string())
        case Kind.LOCAL_TIMESTAMP:
            if getattr(values.type, "tz", None) is not None:
                raise UnsupportedTypeError("an instant on the source cannot be compared with a local timestamp")
            return pc.strftime(values.cast(pa.timestamp("us")), format=TIMESTAMP_FORMAT_ARROW)
        case Kind.INSTANT:
            if getattr(values.type, "tz", None) is None:
                raise UnsupportedTypeError("a local timestamp on the source cannot be compared with an instant")
            return values.cast(pa.timestamp("us", tz="UTC")).cast(pa.int64()).cast(pa.string())
        case Kind.ARRAY:
            if column_type.element is None:
                raise UnsupportedTypeError("an array without an element type")
            element = column_type.element
            return pa.array(
                [
                    None
                    if items is None
                    else ELEMENT_SEPARATOR.join(NULL if i is None else _scalar_text(i, element) for i in items)
                    for items in values.to_pylist()
                ],
                pa.string(),
            )


def _scalar_text(value: object, column_type: ColumnType) -> str:
    return str(value) if column_type.kind is Kind.STRING else str(int(value))  # type: ignore[call-overload]


def _decimal_text(value: float | None, digits: int) -> str | None:
    """A double as Databricks casts it to decimal(38, digits): its shortest decimal form, rounded half up."""
    if value is None:
        return None
    if math.isnan(value) or math.isinf(value):
        raise UnsupportedTypeError(f"{value} has no decimal form")
    rounded = Decimal(repr(value)).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)
    return f"{rounded.copy_abs() if rounded.is_zero() else rounded:f}"


def arrow_row(columns: list[pa.Array]) -> pa.Array:
    filled = [c.fill_null(NULL) for c in columns]
    return pc.binary_join_element_wise(*filled, FIELD_SEPARATOR)
