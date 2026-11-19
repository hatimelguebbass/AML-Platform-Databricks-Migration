import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from lakemig.cutover import runbook
from lakemig.cutover.semantic_model import (
    MODEL,
    REPORT,
    ImportedTable,
    ModelError,
    Plan,
    Target,
    imported_tables,
    render,
)

PLAN = Plan(
    on_premises={"observability.reconciliation": "core banking stays"},
    retired={"observability.pipeline_run": "replaced by system tables"},
    parameter="ServingPlatform",
    rollback_window_days=14,
)


def _table(name: str, schema: str, relation: str, columns: dict[str, str]) -> str:
    quoted = f"'{name}'" if " " in name else name
    selected = ", ".join(f'"{c}"' for c in columns)
    typed = ", ".join(f'{{"{c}", {t}}}' for c, t in columns.items())
    return (
        f"table {quoted}\n\tlineageTag: x\n\n\tcolumn Id\n\t\tsourceColumn: id\n\n"
        f"\tpartition {quoted} = m\n\t\tmode: import\n\t\tsource =\n\t\t\t\tlet\n"
        f'\t\t\t\t    Source = Lakehouse{{[Name = "{schema}", Kind = "Schema"]}}[Data]'
        f'{{[Name = "{relation}", Kind = "Table"]}}[Data],\n'
        f"\t\t\t\t    Columns = Table.SelectColumns(Source, {{{selected}}}),\n"
        f"\t\t\t\t    Typed = Table.TransformColumnTypes(Columns, {{{typed}}})\n"
        "\t\t\t\tin\n\t\t\t\t    Typed\n"
    )


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "source-bi"
    tables = root / MODEL / "definition" / "tables"
    tables.mkdir(parents=True)
    (tables / "Alert.tmdl").write_text(
        _table("Alert", "gold", "aml_alert", {"alert_id": "type text", "amount": "Currency.Type"})
    )
    (tables / "Reconciliation.tmdl").write_text(
        _table("Reconciliation", "observability", "reconciliation", {"entity": "type text"})
    )
    (tables / "Pipeline run.tmdl").write_text(
        _table("Pipeline run", "observability", "pipeline_run", {"step": "type text"})
    )
    (tables / "Date.tmdl").write_text("table Date\n\tpartition Date = calculated\n\t\tsource = CALENDAR(1, 2)\n")
    definition = root / MODEL / "definition"
    (definition / "expressions.tmdl").write_text("expression Lakehouse = trino\n")
    (definition / "model.tmdl").write_text(
        'model Model\n\nannotation PBI_QueryOrder = ["TrinoHost","TrinoPort","Lakehouse","Alert",'
        '"Reconciliation","Pipeline run","Date"]\n\nref table Alert\nref table Reconciliation\n'
        "ref table 'Pipeline run'\nref table Date\n"
    )
    (definition / "relationships.tmdl").write_text(
        "relationship a\n\tfromColumn: Alert.'Business date'\n\ttoColumn: Date.Date\n\n"
        "relationship b\n\tfromColumn: 'Pipeline run'.'Business date'\n\ttoColumn: Date.Date\n"
    )
    for visual, entity in (("minutes", "Pipeline run"), ("alerts", "Alert")):
        folder = root / REPORT / "definition" / "pages" / "health" / "visuals" / visual
        folder.mkdir(parents=True)
        (folder / "visual.json").write_text(json.dumps({"query": {"Entity": entity}}, indent=2))
    return root


def test_the_model_lists_what_each_table_imports(project: Path) -> None:
    tables = {t.name: t for t in imported_tables(project / MODEL)}

    assert sorted(tables) == ["Alert", "Pipeline run", "Reconciliation"]
    assert tables["Alert"].relation == "gold.aml_alert"
    assert dict(tables["Alert"].columns) == {"alert_id": "type text", "amount": "Currency.Type"}
    assert tables["Pipeline run"].expression == "Lakehouse"


def test_render_switches_the_lakehouse_keeps_on_premises_and_drops_retired(project: Path, tmp_path: Path) -> None:
    out = tmp_path / "bi"
    removed = render(project, out, PLAN, Target("adb-1.azuredatabricks.net", "/sql/1.0/warehouses/w", "aml_prod"))

    tables = {t.name: t for t in imported_tables(out / MODEL)}
    assert sorted(tables) == ["Alert", "Reconciliation"]
    assert tables["Reconciliation"].expression == "OnPremises"
    assert tables["Alert"].expression == "Lakehouse"
    assert removed == ["health/minutes"]
    assert (out / REPORT / "definition/pages/health/visuals/alerts/visual.json").exists()
    definition = (out / MODEL / "definition").as_posix()
    expressions = Path(definition, "expressions.tmdl").read_text()
    assert 'if ServingPlatform = "Databricks" then' in expressions
    assert "Databricks.Catalogs(DatabricksHost, DatabricksHttpPath" in expressions
    assert 'expression DatabricksCatalog = "aml_prod"' in expressions
    model = Path(definition, "model.tmdl").read_text()
    assert "Pipeline run" not in model
    assert '"Lakehouse","OnPremises","Alert","Reconciliation","Date"' in model
    assert "Pipeline run" not in Path(definition, "relationships.tmdl").read_text()


def test_render_is_reproducible(project: Path, tmp_path: Path) -> None:
    target = Target("h", "/p", "c")
    render(project, tmp_path / "a", PLAN, target)
    render(project, tmp_path / "b", PLAN, target)

    first = {p.relative_to(tmp_path / "a"): p.read_bytes() for p in (tmp_path / "a").rglob("*") if p.is_file()}
    second = {p.relative_to(tmp_path / "b"): p.read_bytes() for p in (tmp_path / "b").rglob("*") if p.is_file()}
    assert first == second


def test_a_plan_placing_a_relation_the_report_does_not_import_is_refused(project: Path, tmp_path: Path) -> None:
    plan = Plan({"gold.unknown": "?"}, {}, "ServingPlatform", 14)
    with pytest.raises(ModelError, match=r"gold\.unknown"):
        render(project, tmp_path / "bi", plan, Target("h", "/p", "c"))


def _imported(relation: str, expression: str = "Lakehouse", **columns: str) -> ImportedTable:
    return ImportedTable(relation.split(".")[1], Path("x.tmdl"), expression, relation, columns)


def test_the_report_reads_only_published_unmasked_columns_of_loadable_types() -> None:
    tables = [
        _imported(
            "gold.aml_alert", alert_id="type text", amount="Currency.Type", account="type text", gone="type text"
        ),
        _imported("gold.aml_rule", rule_id="type text"),
        _imported("observability.reconciliation", "Lakehouse", entity="type text"),
        _imported("observability.pipeline_run", step="type text"),
    ]
    models = {
        "gold.aml_alert": {
            "alert_id": ("string", None),
            "amount": ("double", None),
            "account": ("string", "governance.mask_pseudonym"),
        }
    }

    assert runbook.contract_problems(tables, PLAN, models) == [
        "aml_alert: gold.aml_alert.amount is double, imported as Currency.Type",
        "aml_alert: gold.aml_alert.account is masked by governance.mask_pseudonym",
        "aml_alert: gold.aml_alert.gone is not published",
        "aml_rule: imports gold.aml_rule, which the transformations do not publish",
        "reconciliation: observability.reconciliation stays on premises but is read from Lakehouse",
        "pipeline_run: imports observability.pipeline_run, retired by the cutover plan: render bi/ again",
    ]


@pytest.mark.parametrize(
    ("m_type", "data_type", "loads"),
    [
        ("Currency.Type", "decimal(22,6)", True),
        ("type datetime", "timestamp_ntz", True),
        ("Int64.Type", "BIGINT", True),
        ("Int64.Type", "string", False),
        ("type logical", "boolean", True),
        ("type date", "timestamp", False),
    ],
)
def test_power_query_types_load_from_their_databricks_types(m_type: str, data_type: str, loads: bool) -> None:
    assert runbook.compatible(m_type, data_type) is loads


def test_key_custody_fails_when_the_target_holds_pseudonyms_of_another_key() -> None:
    check = runbook.key_custody(lambda _: [["7f3a"]], "aml_prod", "49fe")

    assert not check.passed
    assert "under key 7f3a, the on-premises key is 49fe" in check.detail
    assert runbook.key_custody(lambda _: [["49fe"]], "aml_prod", "49fe").passed


def test_a_date_whose_latest_delivery_was_refused_blocks_the_cutover() -> None:
    rows: runbook.Rows = [
        ["2022-09-01", "d1", "accepted", "t1"],
        ["2022-09-02", "d2", "accepted", "t1"],
        ["2022-09-02", "d3", "rejected", "t2"],
        ["2022-09-02", "d0", "superseded", "t3"],
    ]
    check = runbook.deliveries(lambda _: rows, "aml_prod", ["2022-09-01", "2022-09-02", "2022-09-03"])

    assert not check.passed
    assert check.detail == "1/3 dates with an accepted delivery; none for 2022-09-03; latest refused: 2022-09-02 (d3)"


def _parity(**changes: object) -> SimpleNamespace:
    table = SimpleNamespace(relation="gold.aml_alert", equal=True, slices=(SimpleNamespace(target_rows=10),))
    report = {
        "accepted": True,
        "tables": (table,),
        "business_dates": ("2022-09-01", "2022-09-02"),
        "lakehouse_ref": "cutover@abc123",
        "publications": {"2022-09-01": "r1", "2022-09-02": "r1"},
        "measured_at": "2026-11-16T22:00:00+00:00",
    }
    return SimpleNamespace(**{**report, **changes})


def test_parity_must_compare_the_frozen_source_with_the_current_publications() -> None:
    dates = ["2022-09-01", "2022-09-02"]
    published = {"2022-09-01": "r1", "2022-09-02": "r1"}

    assert runbook.parity(_parity(), dates, published, "abc123").passed
    stale = runbook.parity(_parity(lakehouse_ref="main@fff000"), dates, {**published, "2022-09-02": "r2"}, "abc123")
    assert not stale.passed
    assert stale.detail == (
        "measured at source commit fff000, the frozen reference is abc123; republished since the comparison: 2022-09-02"
    )


def test_the_reader_group_needs_the_catalog_and_each_served_schema() -> None:
    tables = [_imported("gold.aml_alert"), _imported("observability.volume_check")]

    def query(statement: str) -> runbook.Rows:
        if "catalog_privileges" in statement:
            return [["USE CATALOG"]]
        return [["gold", "USE SCHEMA"], ["gold", "SELECT"], ["observability", "USE SCHEMA"]]

    check = runbook.reader_grants(query, "aml_prod", tables, PLAN, "aml-reporting")
    assert not check.passed
    assert check.detail == "observability: lacks SELECT"


def test_a_rollback_lists_the_dates_the_source_must_run_first() -> None:
    check = runbook.catch_up(["2022-09-17", "2022-09-18", "2022-09-19"], ["2022-09-17", "2022-09-18"], "2022-09-18")

    assert not check.passed
    assert check.detail == "source must run 2022-09-19 before the switch back"


def test_every_check_runs_and_the_record_keeps_who_serves(tmp_path: Path) -> None:
    def broken() -> runbook.Check:
        raise ConnectionError("trino down")

    failed = runbook.timed("switch", [lambda: runbook.Check("a", True, "ok"), broken])
    assert not failed.passed
    assert failed.serving is None
    assert failed.checks[1] == runbook.Check("broken", False, "ConnectionError: trino down")

    record = runbook.Record(1, "aml_prod", ()).append(failed).append(runbook.timed("switch", []))
    assert record.serving() == "Databricks"
    record = record.append(runbook.timed("rollback", []))
    record.write(tmp_path / "cutover.json")
    assert runbook.Record.read(tmp_path / "cutover.json").serving() == "Trino"


def test_a_check_that_raised_stays_on_one_row_of_its_table() -> None:
    def denied() -> runbook.Check:
        raise RuntimeError("[INSUFFICIENT_PERMISSIONS]\nselect a | b from t")

    record = runbook.Record(1, "aml_prod", ()).append(runbook.timed("preflight", [denied]))
    rows = [line for line in runbook.cutover_md(record, PLAN, []).splitlines() if line.startswith("| denied")]

    assert rows == ["| denied | **fail** | RuntimeError: [INSUFFICIENT_PERMISSIONS] select a \\| b from t |"]
