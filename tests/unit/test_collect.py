from collections.abc import Sequence
from typing import Any

import pytest

from lakemig.inventory.collect import IncompleteSourceError, merge
from lakemig.inventory.model import Column, DataObject, ObjectKind, Volume
from lakemig.inventory.trino import CatalogTable, collect_catalog


class FakeEngine:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def __call__(self, sql: str) -> Sequence[Sequence[Any]]:
        self.statements.append(sql)
        if "information_schema.columns" in sql:
            return [
                ("bronze", "payment_transfer", "transfer_id", "varchar"),
                ("bronze", "payment_transfer", "amount", "decimal(22,2)"),
                ("gold", "alerts_view", "alert_id", "varchar"),
            ]
        if "information_schema.tables" in sql:
            return [("gold", "alerts_view")]
        if "$files" in sql:
            return [(2048, 2)]
        if "count(*)" in sql:
            return [(10,)]
        raise AssertionError(sql)


def test_collects_tables_with_volumes_and_skips_views() -> None:
    engine = FakeEngine()

    tables = collect_catalog(engine, "lakehouse", iceberg=True)

    assert tables == [
        CatalogTable(
            "bronze",
            "payment_transfer",
            (Column("transfer_id", "varchar"), Column("amount", "decimal(22,2)")),
            Volume(rows=10, data_bytes=2048, data_files=2),
        )
    ]


def test_refuses_unexpected_identifiers() -> None:
    def engine(sql: str) -> Sequence[Sequence[Any]]:
        if "information_schema.columns" in sql:
            return [("bronze", 'x"; DROP TABLE y; --', "c", "varchar")]
        return []

    with pytest.raises(ValueError, match="unexpected identifier"):
        collect_catalog(engine, "lakehouse", iceberg=True)


def test_merge_takes_live_columns_and_adds_undeclared_tables() -> None:
    declared = (
        DataObject("silver", "payment_transfer", ObjectKind.MODEL, (Column("transfer_id", ""),), "table"),
        DataObject("gold", "rule_fan_in", ObjectKind.MODEL, (), "ephemeral"),
    )
    live = [
        CatalogTable("silver", "payment_transfer", (Column("transfer_id", "varchar"),), Volume(5, 100, 1)),
        CatalogTable("audit", "ingestion_run", (Column("run_id", "varchar"),), Volume(3, 10, 1)),
    ]

    merged = {o.relation: o for o in merge(declared, live)}

    assert merged["silver.payment_transfer"].columns == (Column("transfer_id", "varchar"),)
    assert merged["silver.payment_transfer"].volume == Volume(5, 100, 1)
    assert merged["audit.ingestion_run"].kind is ObjectKind.SOURCE_TABLE
    assert merged["gold.rule_fan_in"].volume is None


def test_merge_refuses_a_declared_table_the_engine_lacks() -> None:
    declared = (DataObject("gold", "aml_alert", ObjectKind.MODEL, (), "incremental"),)

    with pytest.raises(IncompleteSourceError, match=r"gold\.aml_alert"):
        merge(declared, [])
