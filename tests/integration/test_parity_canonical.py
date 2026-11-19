"""Arrow and Databricks SQL render the same values to the same canonical text and the same row hash."""

import hashlib
from datetime import UTC, date, datetime
from decimal import Decimal

import pyarrow as pa
import pytest
from databricks.sdk import WorkspaceClient

from lakemig.parity import canonical
from lakemig.parity.canonical import ColumnType
from lakemig.translation.warehouse import runner, strict

pytestmark = pytest.mark.integration

# (SQL literal, Arrow value, Arrow type, target type, digits); edge cases of every rendering.
CASES = [
    ("'Café | 中文'", "Café | 中文", pa.string(), "string", 12),
    ("cast(-9007199254740993 as bigint)", -9007199254740993, pa.int64(), "bigint", 12),
    ("cast(7 as int)", 7, pa.int32(), "int", 12),
    ("cast('-0.000001' as decimal(22,6))", Decimal("-0.000001"), pa.decimal128(22, 6), "decimal(22,6)", 12),
    (
        "cast('1234567890123456.5' as decimal(22,6))",
        Decimal("1234567890123456.5"),
        pa.decimal128(22, 6),
        "decimal(22,6)",
        12,
    ),
    ("cast('12.3' as decimal(38,2))", Decimal("12.3"), pa.decimal128(38, 2), "decimal(38,2)", 12),
    ("cast(0.1 as double)", 0.1, pa.float64(), "double", 12),
    ("cast(1e-15 as double)", 1e-15, pa.float64(), "double", 12),
    ("cast(-1e-15 as double)", -1e-15, pa.float64(), "double", 12),
    ("cast(0.87654321987654 as double)", 0.87654321987654, pa.float64(), "double", 12),
    ("cast(2.5e-7 as double)", 2.5e-7, pa.float64(), "double", 6),
    ("cast(123456.000000049 as double)", 123456.000000049, pa.float64(), "double", 9),
    ("true", True, pa.bool_(), "boolean", 12),
    ("date'1970-01-01'", date(1970, 1, 1), pa.date32(), "date", 12),
    (
        "timestamp_ntz'2022-09-01 23:59:59.000001'",
        datetime(2022, 9, 1, 23, 59, 59, 1),
        pa.timestamp("us"),
        "timestamp_ntz",
        12,
    ),
    (
        "timestamp'2022-09-01 00:00:00.5+02:00'",
        datetime(2022, 8, 31, 22, 0, 0, 500000, tzinfo=UTC),
        pa.timestamp("us", tz="UTC"),
        "timestamp",
        12,
    ),
    ("array(3L, 1L, 2L)", [3, 1, 2], pa.list_(pa.int64()), "array<bigint>", 12),
    ("array('MA', 'FR')", ["MA", "FR"], pa.list_(pa.string()), "array<string>", 12),
    ("cast(array() as array<string>)", [], pa.list_(pa.string()), "array<string>", 12),
    ("cast(null as string)", None, pa.string(), "string", 12),
]


@pytest.fixture(scope="module")
def query():  # type: ignore[no-untyped-def]
    client = WorkspaceClient()
    warehouse = next(iter(client.warehouses.list())).id
    assert warehouse
    return strict(runner(client, warehouse, "system"))


def test_every_type_renders_alike(query) -> None:  # type: ignore[no-untyped-def]
    columns = [f"c{i}" for i in range(len(CASES))]
    types = [ColumnType.parse(case[3]) for case in CASES]
    source = ", ".join(f"{case[0]} as {column}" for case, column in zip(CASES, columns, strict=True))
    texts = [canonical.sql(c, t, case[4]) for c, t, case in zip(columns, types, CASES, strict=True)]
    statement = f"select {', '.join(texts)}, sha2({canonical.sql_row(texts)}, 256) from (select {source})"
    target = query(statement)[0]
    expected = [
        canonical.arrow(pa.array([case[1]], case[2]), t, case[4])[0].as_py()
        for case, t in zip(CASES, types, strict=True)
    ]
    assert target[:-1] == expected
    row = canonical.arrow_row([pa.array([e], pa.string()) for e in expected])[0].as_py()
    assert target[-1] == hashlib.sha256(row.encode()).hexdigest()
