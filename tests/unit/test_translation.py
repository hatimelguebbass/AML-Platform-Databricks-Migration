from pathlib import Path

import pytest

from lakemig.translation.report import (
    LINEAGE,
    Change,
    ChangeKind,
    HandChanges,
    ModelResult,
    Outcome,
    Report,
    comparison_sql,
    measure,
    translation_md,
    transpile,
)

SOURCE = """
select
    lower(to_hex(md5(to_utf8(rule_id || '|' || cast(account_id as varchar))))) as alert_id,
    cardinality(tokens) as token_count,
    cast(current_timestamp at time zone 'UTC' as timestamp(6)) as _loaded_at
from "aml_dev"."gold"."evidence"
"""
COLUMNS = ["alert_id", "token_count", "_loaded_at"]


def test_transpile_reports_the_functions_it_rewrote() -> None:
    sql, rewritten = transpile(SOURCE)

    assert "`aml_dev`.`gold`.`evidence`" in sql
    assert rewritten["cardinality(…)"] == "size(…)"
    assert rewritten["to_utf8(…)"] == "encode(…, 'utf-8')"
    assert "lower(…)" not in rewritten


def test_comparison_counts_rows_missing_on_each_side_as_multisets() -> None:
    sql = comparison_sql("select 1 as a", "aml_dev.gold.t", ["a"])

    assert "except all" in sql
    assert sql.count("select count(*)") == 3


class FakeWarehouse:
    def __init__(self, rows: list[list[str | None]] | None, error: str | None = None) -> None:
        self.rows, self.error = rows, error
        self.statements: list[str] = []

    def __call__(self, statement: str) -> tuple[list[list[str | None]] | None, str | None]:
        self.statements.append(statement)
        return self.rows, self.error


@pytest.mark.parametrize(
    ("rows", "outcome"),
    [([["0", "0", "12"]], Outcome.IDENTICAL), ([["1", "0", "12"]], Outcome.DIFFERENT)],
)
def test_measure_compares_without_lineage_columns(rows: list[list[str | None]], outcome: Outcome) -> None:
    warehouse = FakeWarehouse(rows)

    result = measure("gold.alert", SOURCE, "aml_dev.gold.alert", COLUMNS, warehouse)

    assert result.outcome is outcome
    assert result.port_rows == 12
    assert "`_loaded_at`" not in warehouse.statements[0]
    assert "_loaded_at" in LINEAGE


def test_measure_keeps_the_first_line_of_a_warehouse_error() -> None:
    error = "[UNRESOLVED_COLUMN] `birth_date` cannot be resolved. SQLSTATE: 42703\n== SQL ==\nselect ..."

    result = measure("silver.customer", SOURCE, "aml_dev.silver.customer", COLUMNS, FakeWarehouse(None, error))

    assert result.outcome is Outcome.FAILS
    assert result.error == "[UNRESOLVED_COLUMN] `birth_date` cannot be resolved. SQLSTATE: 42703"


def test_measure_does_not_run_what_sqlglot_cannot_parse() -> None:
    warehouse = FakeWarehouse([["0", "0", "0"]])

    result = measure("gold.alert", "select from where (", "aml_dev.gold.alert", COLUMNS, warehouse)

    assert result.outcome is Outcome.UNTRANSLATED
    assert warehouse.statements == []


def test_hand_changes_cover_exactly_the_ported_models(tmp_path: Path) -> None:
    path = tmp_path / "translation.yaml"
    path.write_text(
        "common: []\nmodels:\n  silver.a: []\n  silver.gone:\n    - {change: x, reason: y, kind: syntax}\n",
        encoding="utf-8",
    )

    problems = HandChanges.read(path).problems(["silver.a", "silver.b"])

    assert problems == [
        "translation.yaml: no entry for ported model silver.b",
        "translation.yaml: silver.gone is not a ported model",
    ]


def test_report_round_trips_and_renders(tmp_path: Path) -> None:
    report = Report(
        format_version=1,
        sqlglot_version="30.31.0",
        source_commit="db07483041d7ec803ce3799cd7b201e0ec2859bb",
        engine_version="Databricks SQL 2026.44",
        measured_on="2026-11-15",
        models=(
            ModelResult("silver.a", Outcome.IDENTICAL, {"array_agg(…)": "collect_list(…)"}, 40, 10, 0, 0),
            ModelResult("silver.b", Outcome.FAILS, {}, 90, error="[UNRESOLVED_COLUMN] x | y"),
        ),
    )
    report.write(tmp_path / "translation.json")
    changes = HandChanges(
        common=(Change("REPLACE WHERE", "atomic", ChangeKind.ENGINE),),
        models={"silver.a": (), "silver.b": (Change("Birth year", "boundary", ChangeKind.BOUNDARY),)},
    )

    rendered = translation_md(Report.read(tmp_path / "translation.json"), changes, "https://github.com/o/source")

    assert Report.read(tmp_path / "translation.json") == report
    assert "| identical | 1 of 2 |" in rendered
    assert "| fails | 1 of 2 |" in rendered
    assert "`array_agg(…)` → `collect_list(…)`" in rendered
    assert "fails: [UNRESOLVED_COLUMN] x \\| y" in rendered
    assert "`silver.a`:\n\n- None." in rendered
    assert "- **boundary**: Birth year. boundary" in rendered
