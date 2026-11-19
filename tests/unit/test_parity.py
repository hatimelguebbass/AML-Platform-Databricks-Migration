import hashlib
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pytest

from lakemig.export.pseudonym import Key, pseudonym
from lakemig.parity import canonical, compare, fingerprint, run
from lakemig.parity.canonical import ColumnType, Kind, UnsupportedTypeError
from lakemig.parity.fingerprint import Fingerprint, Layout
from lakemig.parity.report import Report, TableResult, parity_md
from lakemig.parity.spec import Spec, SpecError, TableSpec, reconcile

SPEC = Path(__file__).parents[2] / "migration" / "parity.yaml"
KEY = Key(bytes(range(32)))


def raw_spec(**table: object) -> dict[str, object]:
    return {
        "lineage": ["_loaded_at"],
        "zero_difference": ["gold.aml_alert"],
        "tables": {"gold.aml_alert": {"key": ["alert_id"], **table}},
    }


# Canonical text --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("BIGINT", Kind.INTEGER),
        ("string", Kind.STRING),
        ("timestamp_ntz", Kind.LOCAL_TIMESTAMP),
        ("timestamp", Kind.INSTANT),
        ("decimal(22,6)", Kind.DECIMAL),
        ("array<bigint>", Kind.ARRAY),
    ],
)
def test_types_parse(text: str, kind: Kind) -> None:
    assert ColumnType.parse(text).kind is kind


def test_unsupported_types_are_refused() -> None:
    with pytest.raises(UnsupportedTypeError):
        ColumnType.parse("map<string,string>")
    with pytest.raises(UnsupportedTypeError):
        ColumnType.parse("array<double>")


def test_tolerance_decimals_keep_larger_differences_visible() -> None:
    assert canonical.digits_for(None) == canonical.EXACT_DOUBLE_DIGITS
    assert canonical.digits_for(1e-12) == 13
    for tolerance in (1e-12, 1e-6, 0.05):
        digits = canonical.digits_for(tolerance)
        a, b = 0.1234567, 0.1234567 + tolerance * 1.01
        texts = canonical.arrow(pa.array([a, b]), ColumnType(Kind.DOUBLE), digits).to_pylist()
        assert texts[0] != texts[1]


def test_doubles_print_as_rounded_decimals_without_negative_zero() -> None:
    texts = canonical.arrow(pa.array([-1e-15, 0.5, None]), ColumnType(Kind.DOUBLE), 2).to_pylist()
    assert texts == ["0.00", "0.50", None]


def test_timestamps_need_the_same_meaning_on_both_sides() -> None:
    with pytest.raises(UnsupportedTypeError):
        canonical.arrow(pa.array([0], pa.timestamp("us", tz="UTC")), ColumnType(Kind.LOCAL_TIMESTAMP))
    with pytest.raises(UnsupportedTypeError):
        canonical.arrow(pa.array([0], pa.timestamp("us")), ColumnType(Kind.INSTANT))


def test_rows_keep_nulls_apart_from_empty_text() -> None:
    rows = canonical.arrow_row([pa.array(["", None]), pa.array(["x", "x"])]).to_pylist()
    assert rows == ["\x1fx", "\\N\x1fx"]


# Spec ------------------------------------------------------------------------------------------------


def test_the_committed_spec_is_valid() -> None:
    spec = Spec.read(SPEC)
    assert {"gold.aml_alert", "gold.aml_alert_transfer", "gold.sanctions_hit"} <= spec.zero_difference
    assert all(not spec.table(t).tolerance for t in spec.zero_difference)


def test_zero_difference_tables_take_no_tolerance() -> None:
    with pytest.raises(SpecError, match="must not differ at all"):
        Spec.parse(raw_spec(tolerance={"amount": 0.01}))


def test_columns_kept_on_premises_need_a_reason() -> None:
    with pytest.raises(SpecError, match="without a reason"):
        Spec.parse(raw_spec(on_premises={"listed_name": ""}))


def test_every_column_is_accounted_for() -> None:
    table = TableSpec("gold.sanctions_hit", ("hit_id",), on_premises={"listed_name": "names stay"})
    columns = reconcile(
        table,
        {"_loaded_at", "_delivery_id"},
        ["hit_id", "listed_name", "score", "_loaded_at", "source_only"],
        ["hit_id", "score", "_delivery_id", "_loaded_at", "target_only"],
    )
    assert columns.compared == ("hit_id", "score")
    assert len(columns.problems) == 2
    assert any("target_only" in p and "target only" in p for p in columns.problems)
    assert any("source_only" in p and "source only" in p for p in columns.problems)


# Fingerprints and verdicts ---------------------------------------------------------------------------

LAYOUT = Layout(
    columns=("transfer_id", "business_date", "amount", "rate"),
    types={
        "transfer_id": ColumnType(Kind.STRING),
        "business_date": ColumnType(Kind.DATE),
        "amount": ColumnType(Kind.DECIMAL, 22, 6),
        "rate": ColumnType(Kind.DOUBLE),
    },
    key=("transfer_id",),
    tolerance={"rate": 1e-6},
)


def transfers(amounts: list[str], rates: list[float], ids: list[str] | None = None) -> pa.Table:
    ids = ids or [f"T{i:04d}" for i in range(len(amounts))]
    return pa.table(
        {
            "transfer_id": ids,
            "business_date": pa.array([date(2022, 9, 1)] * len(ids)),
            "amount": pa.array([Decimal(a) for a in amounts], pa.decimal128(22, 6)),
            "rate": rates,
        }
    )


class FakeTarget:
    """Answers the drill-down statement with the target table's rows, as the warehouse would."""

    def __init__(self, table: pa.Table) -> None:
        self.print, _, self.rows = fingerprint.of_arrow(table, LAYOUT)
        self.statements: list[str] = []

    def __call__(self, statement: str) -> run.Rows:
        self.statements.append(statement)
        buckets = set(re.findall(r"'([0-9a-f]{2})'", statement.rsplit(" in (", 1)[1]))
        return [
            [k, r.digest, *r.values]
            for k, r in self.rows.items()
            if hashlib.sha256(k.encode()).hexdigest()[: fingerprint.BUCKET_HEX] in buckets
        ]


class FakeSource:
    def __init__(self, table: pa.Table) -> None:
        self.table = table
        self.ref = "main@0"

    def columns(self, relation: str) -> tuple[str, ...]:
        return tuple(self.table.column_names)

    def read(self, relation: str, columns: object, row_filter: object = None) -> pa.Table:
        return self.table


def slice_result(source: pa.Table, target: pa.Table) -> compare.SliceResult:
    fake = FakeTarget(target)
    table = TableSpec("silver.payment_transfer", ("transfer_id",), "business_date", tolerance={"rate": 1e-6})
    return run._compare_slice(table, LAYOUT, FakeSource(source), fake, KEY, "t", "2022-09-01", fake.print)


def test_equal_tables_are_equal_without_reading_rows() -> None:
    rows = transfers(["1.5", "2", "-3.25"], [0.1, 0.2, 0.3])
    fake = FakeTarget(rows)
    result = slice_result(rows, rows)
    assert result.equal
    assert result.buckets_differing == 0
    assert not fake.statements


def test_a_changed_amount_is_found_and_named() -> None:
    result = slice_result(
        transfers(["1.5", "2", "3"], [0.1, 0.2, 0.3]), transfers(["1.5", "2.01", "3"], [0.1, 0.2, 0.3])
    )
    assert not result.equal
    assert result.different == {"amount": 1}
    assert result.examples == ("changed: T0001",)
    assert any(m.column == "amount" and m.metric == "total" for m in result.profile_mismatches)


def test_differences_within_tolerance_are_accepted() -> None:
    result = slice_result(transfers(["1"], [0.1]), transfers(["1"], [0.1000004]))
    assert result.equal
    assert result.within_tolerance == {"rate": 1}


def test_missing_and_unexpected_rows_are_counted() -> None:
    result = slice_result(
        transfers(["1", "2"], [0.1, 0.2], ids=["A", "B"]), transfers(["1", "2"], [0.1, 0.2], ids=["A", "C"])
    )
    assert (result.missing_in_target, result.unexpected_in_target) == (1, 1)
    assert not result.equal


def test_a_slice_missing_on_the_target_differs() -> None:
    fake = FakeTarget(transfers([], []))
    table = TableSpec("silver.payment_transfer", ("transfer_id",), "business_date")
    result = run._compare_slice(
        table,
        LAYOUT,
        FakeSource(transfers(["1"], [0.1])),
        fake,
        KEY,
        "t",
        "2022-09-01",
        Fingerprint.empty(LAYOUT),
    )
    assert result.missing_in_target == 1
    assert not result.equal


def test_duplicate_keys_fail_the_slice() -> None:
    result = slice_result(transfers(["1", "1"], [0.1, 0.1], ids=["A", "A"]), transfers(["1"], [0.1], ids=["A"]))
    assert result.duplicate_keys == 1
    assert not result.equal


def test_source_values_are_pseudonymized_before_the_comparison() -> None:
    rows = run._pseudonymized(pa.table({"account": ["MA64011"], "n": [1]}), {"account": "account_number"}, KEY)
    assert rows.column("account").to_pylist() == [pseudonym("MA64011", KEY, "account_number")]


def test_target_statements_name_every_compared_column() -> None:
    statement = fingerprint.profile_sql("aml_prod.silver.payment_transfer", LAYOUT, "business_date", ["2022-09-01"])
    assert "`aml_prod`.`silver`.`payment_transfer`" in statement
    assert "date'2022-09-01'" in statement
    # The tolerated double has no distinct count: values within the tolerance may print differently.
    assert statement.count("count(distinct") == 1 + len(LAYOUT.columns) - 1
    parsed = fingerprint.parse_profiles(
        [["2022-09-01", "3", "3", "0", "3", "0", "1", "2022-09-01", "2022-09-01", "0", "3", "6.000000", "0"]],
        LAYOUT,
    )
    rows, keys, columns = parsed["2022-09-01"]
    assert (rows, keys) == (3, 3)
    assert columns["amount"].total == Decimal("6")
    assert columns["rate"].distinct is None


def test_the_report_round_trips_and_renders(tmp_path: Path) -> None:
    spec = Spec.parse(raw_spec(slice="business_date"), "abc")
    differing = compare.SliceResult(
        "2022-09-01", 2, 1, missing_in_target=1, buckets=2, buckets_differing=1, buckets_inspected=1
    )
    report = Report(
        format_version=1,
        measured_at="2026-11-16T00:00:00+00:00",
        spec_digest="abc",
        source_repository="https://github.com/o/r",
        source_commit="0" * 40,
        lakehouse_ref="main@1",
        target_catalog="aml_prod",
        engine_version="Databricks SQL 2026.40",
        business_dates=("2022-09-01", "2022-09-01"),
        publications={"2022-09-01": "1"},
        tables=(TableResult("gold.aml_alert", ("alert_id",), "business_date", ("alert_id",), (differing,)),),
    )
    report.write(tmp_path / "parity.json")
    again = Report.read(tmp_path / "parity.json")
    assert again == report
    assert not again.accepted
    assert again.problems(spec) == []
    text = parity_md(again, spec)
    assert "**not accepted**" in text
    assert "1 missing in the target" in text


def test_drill_down_stays_within_the_row_budget() -> None:
    sizes = {f"{b:02x}": (run.MAX_INSPECTED_ROWS // 3, 0) for b in range(10)}
    source = Fingerprint(0, 0, {}, sizes)
    chosen = run._sample(sorted(sizes), source, Fingerprint(0, 0, {}, {}))
    assert chosen == ["00", "01", "02"]
    huge = Fingerprint(0, 0, {}, {"00": (run.MAX_INSPECTED_ROWS * 5, 0)})
    assert run._sample(["00", "01"], huge, Fingerprint(0, 0, {}, {})) == ["00"]
