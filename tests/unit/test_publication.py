from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from lakemig.assessment.governance import read_models
from lakemig.publication import job, wap
from lakemig.publication.wap import Lakehouse, Mask, Table

ROOT = Path(__file__).resolve().parents[2]
D1, D2, D3 = date(2022, 9, 1), date(2022, 9, 2), date(2022, 9, 3)
SLICED = (("transfer_id", "string"), ("business_date", "date"), ("_pipeline_run_id", "string"))
WHOLE = (("account_id", "bigint"), ("_pipeline_run_id", "string"))
SEED = (("code", "string"),)
MASK = Mask("transfer_id", "aml.governance.mask_pseudonym", None)
_LIKE = re.compile(r"CREATE TABLE (?:IF NOT EXISTS )?`(\w+)`\.`(\w+)`\.`(\w+)` LIKE `\w+`\.`(\w+)`\.`(\w+)`")


@dataclass
class FakeTable:
    columns: tuple[tuple[str, str], ...]
    runs: dict[date, str | None] = field(default_factory=dict)
    masks: set[Mask] = field(default_factory=set)
    foreign: int = 0
    clustering: tuple[str, ...] = ()


class FakeLakehouse(Lakehouse):
    """Catalog state in memory; every statement is recorded, and CREATE ... LIKE copies columns."""

    def __init__(self, tables: dict[str, FakeTable]) -> None:
        self.state = tables
        self.statements: list[str] = []
        super().__init__(self._run, "aml")

    def _run(self, statement: str) -> Sequence[Sequence[Any]]:
        self.statements.append(statement)
        like = _LIKE.match(statement)
        if like:
            _, schema, name, source_schema, source_name = like.groups()
            target = f"{schema}.{name}"
            if target not in self.state:
                source = self.state[f"{source_schema}.{source_name}"]
                self.state[target] = FakeTable(source.columns, clustering=source.clustering)
        drop = re.match(r"DROP TABLE `\w+`\.`(\w+)`\.`(\w+)`", statement)
        if drop:
            del self.state[".".join(drop.groups())]
        return []

    def _get(self, table: Table) -> FakeTable:
        return self.state[f"{table.schema}.{table.name}"]

    def tables(self, schema: str) -> list[Table]:
        return [Table("aml", *key.split(".")) for key in sorted(self.state) if key.split(".")[0] == schema]

    def columns(self, table: Table) -> tuple[tuple[str, str], ...]:
        return self._get(table).columns

    def slice_runs(self, table: Table) -> dict[date, str | None]:
        return dict(self._get(table).runs)

    def masks(self, table: Table) -> set[Mask]:
        return set(self._get(table).masks)

    def rows_to_publish(self, table: Table, business_date: date | None, run_id: str | None) -> tuple[int, int]:
        return 10, self._get(table).foreign if run_id else 0

    def copy_governance(self, source: Table, target: Table) -> None:
        self.statements.append(f"GOVERNANCE {source} -> {target}")
        self._get(target).masks = set(self._get(source).masks)

    def set_mask(self, table: Table, mask: Mask) -> None:
        super().set_mask(table, mask)
        self._get(table).masks.add(mask)

    def clustering(self, table: Table) -> tuple[str, ...]:
        return self._get(table).clustering

    def cluster(self, table: Table, columns: Sequence[str]) -> None:
        super().cluster(table, columns)
        self._get(table).clustering = tuple(columns)

    def version(self, table: Table) -> int:
        return 7

    def writes(self) -> list[str]:
        return [s for s in self.statements if not s.startswith(("CREATE TABLE IF NOT EXISTS", "SELECT"))]


def test_diverged_dates_are_the_slices_whose_run_differs() -> None:
    staging = {D1: "r1", D2: "r9", D3: None}
    published = {D1: "r1", D2: "r2", D3: None}

    assert wap.diverged(staging, published) == [D2, D3]
    assert wap.diverged({}, {D1: "r1"}) == [D1]
    assert wap.diverged({D1: "r9"}, {}) == [D1]
    assert wap.diverged({D1: "r1"}, {D1: "r1"}) == []


def test_first_opening_copies_the_published_history_into_a_new_staging_table() -> None:
    lake = FakeLakehouse({"silver.payment_transfer": FakeTable(SLICED, {D1: "r1", D2: "r2"})})

    reset = wap.open_staging(lake, ["silver"])

    assert reset == {"`aml`.`silver_staging`.`payment_transfer`": [D1, D2]}
    assert lake.statements[0] == (
        "CREATE TABLE IF NOT EXISTS `aml`.`silver_staging`.`payment_transfer` LIKE `aml`.`silver`.`payment_transfer`"
    )
    assert lake.writes() == [
        "INSERT INTO `aml`.`silver_staging`.`payment_transfer` REPLACE WHERE business_date IN "
        "(DATE'2022-09-01', DATE'2022-09-02') SELECT * FROM `aml`.`silver`.`payment_transfer` "
        "WHERE business_date IN (DATE'2022-09-01', DATE'2022-09-02')"
    ]


def test_opening_resets_only_what_a_failed_run_left_and_ignores_whole_tables() -> None:
    lake = FakeLakehouse(
        {
            "silver.payment_transfer": FakeTable(SLICED, {D1: "r1", D2: "r2"}),
            "silver_staging.payment_transfer": FakeTable(SLICED, {D1: "r1", D2: "r2", D3: "failed"}),
            "silver.account_history": FakeTable(WHOLE),
            "silver_staging.account_history": FakeTable(WHOLE),
        }
    )

    assert wap.open_staging(lake, ["silver"]) == {"`aml`.`silver_staging`.`payment_transfer`": [D3]}
    assert len(lake.writes()) == 1


def test_opening_in_steady_state_writes_nothing() -> None:
    lake = FakeLakehouse(
        {
            "gold.aml_alert": FakeTable(SLICED, {D1: "r1"}),
            "gold_staging.aml_alert": FakeTable(SLICED, {D1: "r1"}),
        }
    )

    assert wap.open_staging(lake, ["gold"]) == {}
    assert lake.writes() == []


def test_opening_empties_a_dated_staging_table_never_published() -> None:
    lake = FakeLakehouse({"gold_staging.new_alerts": FakeTable(SLICED, {D1: "r1"})})

    assert wap.open_staging(lake, ["gold"]) == {"`aml`.`gold_staging`.`new_alerts`": []}
    assert lake.writes() == ["DELETE FROM `aml`.`gold_staging`.`new_alerts`"]


def test_opening_refuses_staging_columns_that_changed() -> None:
    lake = FakeLakehouse(
        {
            "silver.payment_transfer": FakeTable(SLICED, {D1: "r1"}),
            "silver_staging.payment_transfer": FakeTable((*SLICED, ("extra", "int")), {D1: "r1"}),
        }
    )

    with pytest.raises(wap.SchemaChangedError, match="rebuild every date"):
        wap.open_staging(lake, ["silver"])


def _built(run: str = "run42") -> dict[str, FakeTable]:
    return {
        "reference.iso_currency": FakeTable(SEED),
        "reference_staging.iso_currency": FakeTable(SEED),
        "silver.payment_transfer": FakeTable(SLICED, masks={MASK}),
        "silver_staging.payment_transfer": FakeTable(SLICED, {D2: run}, masks={MASK}),
        "silver.account_history": FakeTable(WHOLE),
        "silver_staging.account_history": FakeTable(WHOLE),
    }


def test_publishing_a_date_replaces_its_slices_and_overwrites_whole_tables() -> None:
    lake = FakeLakehouse(_built())

    published = wap.publish(lake, ["reference", "silver"], D2, "run42")

    assert [p.table for p in published] == [
        "`aml`.`reference`.`iso_currency`",
        "`aml`.`silver`.`account_history`",
        "`aml`.`silver`.`payment_transfer`",
    ]
    writes = lake.writes()
    assert writes[:3] == [
        "INSERT OVERWRITE `aml`.`reference`.`iso_currency` SELECT * FROM `aml`.`reference_staging`.`iso_currency`",
        "INSERT OVERWRITE `aml`.`silver`.`account_history` SELECT * FROM `aml`.`silver_staging`.`account_history`",
        "INSERT INTO `aml`.`silver`.`payment_transfer` REPLACE WHERE business_date = DATE'2022-09-02' "
        "SELECT * FROM `aml`.`silver_staging`.`payment_transfer` WHERE business_date = DATE'2022-09-02'",
    ]
    assert writes[-1].startswith("INSERT INTO `aml`.`observability`.`publication` VALUES (DATE'2022-09-02', 'run42',")
    assert "named_struct('name', '`aml`.`silver`.`payment_transfer`', 'rows', 10, 'version', 7)" in writes[-1]


def test_rebuild_overwrites_every_table_and_records_no_date() -> None:
    lake = FakeLakehouse(_built())

    wap.publish(lake, ["silver"], None, "run42")

    assert all(w.startswith("INSERT OVERWRITE") for w in lake.writes()[:-1])
    assert "VALUES (CAST(NULL AS DATE), 'run42'," in lake.writes()[-1]


def test_rows_of_another_run_stop_the_publication_before_any_write() -> None:
    tables = _built()
    tables["silver_staging.payment_transfer"].foreign = 3
    lake = FakeLakehouse(tables)

    with pytest.raises(wap.StaleStagingError, match="3 of 10 rows to publish were not built by run run42"):
        wap.publish(lake, ["reference", "silver"], D2, "run42")
    assert lake.writes() == []


def test_a_new_table_is_created_with_its_governance_before_its_rows() -> None:
    tables = _built()
    del tables["silver.payment_transfer"]
    lake = FakeLakehouse(tables)

    wap.publish(lake, ["silver"], D2, "run42")

    writes = lake.writes()
    create = writes.index(
        "CREATE TABLE `aml`.`silver`.`payment_transfer` LIKE `aml`.`silver_staging`.`payment_transfer`"
    )
    assert (
        writes[create + 1]
        == "GOVERNANCE `aml`.`silver_staging`.`payment_transfer` -> `aml`.`silver`.`payment_transfer`"
    )
    assert writes[create + 2].startswith("INSERT INTO `aml`.`silver`.`payment_transfer` REPLACE WHERE")


def test_changed_columns_fail_a_daily_publication_and_recreate_on_a_rebuild() -> None:
    tables = _built()
    tables["silver_staging.account_history"] = FakeTable((*WHOLE, ("risk", "string")))

    with pytest.raises(wap.SchemaChangedError):
        wap.publish(FakeLakehouse(dict(tables)), ["silver"], D2, "run42")

    lake = FakeLakehouse(tables)
    wap.publish(lake, ["silver"], None, "run42")
    writes = lake.writes()
    drop = writes.index("DROP TABLE `aml`.`silver`.`account_history`")
    assert (
        writes[drop + 1]
        == "CREATE TABLE `aml`.`silver`.`account_history` LIKE `aml`.`silver_staging`.`account_history`"
    )
    assert lake.state["silver.account_history"].columns == (*WHOLE, ("risk", "string"))


def test_a_mask_the_model_gained_is_set_before_the_rows_and_a_changed_one_stops() -> None:
    tables = _built()
    tables["silver.payment_transfer"].masks = set()
    lake = FakeLakehouse(tables)

    wap.publish(lake, ["silver"], D2, "run42")
    writes = lake.writes()
    mask = writes.index(
        "ALTER TABLE `aml`.`silver`.`payment_transfer` ALTER COLUMN `transfer_id` "
        "SET MASK aml.governance.mask_pseudonym"
    )
    assert writes[mask + 1].startswith("INSERT INTO `aml`.`silver`.`payment_transfer`")

    tables = _built()
    tables["silver.payment_transfer"].masks = {Mask("transfer_id", "aml.governance.reveal", None)}
    with pytest.raises(wap.GovernanceDriftError, match="transfer_id"):
        wap.publish(FakeLakehouse(tables), ["silver"], D2, "run42")


@pytest.mark.parametrize("name", ["Silver", "silver; DROP", "a.b", ""])
def test_only_plain_identifiers_reach_sql(name: str) -> None:
    with pytest.raises(ValueError, match="identifier"):
        wap.identifier(name)


def test_literals_are_escaped() -> None:
    assert wap.literal("it's \\ done") == "'it\\'s \\\\ done'"


def test_publication_refuses_an_odd_run_id() -> None:
    with pytest.raises(ValueError, match="run id"):
        wap.publish(FakeLakehouse(_built()), ["silver"], D2, "42' OR '1")


def test_an_empty_business_date_fails_instead_of_rebuilding_everything() -> None:
    parser = job._parser("test")

    assert parser.parse_args(["--catalog=aml", "--layers=silver", "--business-date=all"]).business_date is None
    assert parser.parse_args(["--catalog=aml", "--layers=silver", "--business-date=2022-09-02"]).business_date == D2
    with pytest.raises(SystemExit):
        parser.parse_args(["--catalog=aml", "--layers=silver", "--business-date="])


def test_dated_models_and_business_date_columns_are_the_same_set() -> None:
    """The publication slices a table by its business_date column: exactly the models dbt replaces per date."""
    project = ROOT / "transform"
    dated = {
        path.stem for path in (project / "models").rglob("*.sql") if "dated_table_config()" in path.read_text("utf-8")
    }
    with_column = {m.name for m in read_models(project) if any(c.name == wap.SLICE_COLUMN for c in m.columns)}

    assert dated == with_column
    assert dated  # the convention is in use, so the check checks something


def test_a_changed_clustering_is_applied_after_the_rows_and_reclusters_the_whole_table() -> None:
    clustered = ("business_date", "originator_account_id")
    lake = FakeLakehouse(
        {
            "silver.payment_transfer": FakeTable(SLICED, {D1: "r1"}),
            "silver_staging.payment_transfer": FakeTable(SLICED, {D1: "r1", D2: "r2"}, clustering=clustered),
        }
    )

    wap.publish(lake, ["silver"], D2, "r2")

    writes = [w for w in lake.writes() if not w.startswith("INSERT INTO `aml`.`observability`")]
    assert writes[-3:] == [
        "INSERT INTO `aml`.`silver`.`payment_transfer` REPLACE WHERE business_date = DATE'2022-09-02' "
        "SELECT * FROM `aml`.`silver_staging`.`payment_transfer` WHERE business_date = DATE'2022-09-02'",
        "ALTER TABLE `aml`.`silver`.`payment_transfer` CLUSTER BY (`business_date`, `originator_account_id`)",
        "OPTIMIZE `aml`.`silver`.`payment_transfer` FULL",
    ]

    lake.statements.clear()
    lake.state["silver_staging.payment_transfer"].runs[D3] = "r3"
    wap.publish(lake, ["silver"], D3, "r3")

    assert not [w for w in lake.writes() if w.startswith(("ALTER", "OPTIMIZE"))]


def test_clustering_dropped_from_a_model_is_dropped_without_rewriting() -> None:
    lake = FakeLakehouse(
        {
            "silver.account_history": FakeTable(WHOLE, clustering=("account_id",)),
            "silver_staging.account_history": FakeTable(WHOLE),
        }
    )

    wap.publish(lake, ["silver"], None, "r1")

    assert "ALTER TABLE `aml`.`silver`.`account_history` CLUSTER BY NONE" in lake.writes()
    assert not [w for w in lake.writes() if w.startswith("OPTIMIZE")]


def test_opening_never_stages_a_dated_table_no_run_builds() -> None:
    log = (("business_date", "date"), ("run_id", "string"), ("published_at", "timestamp"))
    lake = FakeLakehouse(
        {
            "observability.publication": FakeTable(log),
            "observability.volume_check": FakeTable(SLICED, {D1: "r1"}),
            "observability_staging.volume_check": FakeTable(SLICED, {D1: "r1"}),
        }
    )

    assert wap.open_staging(lake, ["observability"]) == {}
    assert "observability_staging.publication" not in lake.state
