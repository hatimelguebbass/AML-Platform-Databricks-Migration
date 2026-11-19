from typing import Any

import pytest

from lakemig.inventory.dbt_project import parse_manifest
from lakemig.inventory.model import ObjectKind


def _manifest() -> dict[str, Any]:
    return {
        "metadata": {"dbt_version": "1.11.17", "project_name": "aml"},
        "nodes": {
            "model.aml.payment_transfer": {
                "resource_type": "model",
                "name": "payment_transfer",
                "alias": "payment_transfer",
                "schema": "silver",
                "database": "lakehouse_etl",
                "config": {"materialized": "table"},
                "depends_on": {
                    "nodes": ["source.aml.bronze.payment_transfer", "seed.aml.iso_currency"],
                    "macros": ["macro.aml.lineage_columns", "macro.dbt.run_query"],
                },
                "columns": {"transfer_id": {"name": "transfer_id", "data_type": "varchar"}},
                "compiled_code": "SELECT upper(x) AS transfer_id FROM lakehouse_etl.bronze.payment_transfer",
            },
            "seed.aml.iso_currency": {
                "resource_type": "seed",
                "name": "iso_currency",
                "schema": "reference",
                "database": "lakehouse_etl",
                "config": {"materialized": "seed"},
                "depends_on": {"nodes": [], "macros": []},
                "columns": {},
            },
            "test.aml.unique_transfer_id": {
                "resource_type": "test",
                "attached_node": "model.aml.payment_transfer",
                "depends_on": {"nodes": ["model.aml.payment_transfer"]},
            },
        },
        "sources": {
            "source.aml.bronze.payment_transfer": {
                "resource_type": "source",
                "name": "payment_transfer",
                "identifier": "payment_transfer",
                "schema": "bronze",
                "database": "lakehouse_etl",
            },
            "source.aml.corebanking.customer": {
                "resource_type": "source",
                "name": "customer",
                "identifier": "customer",
                "schema": "core",
                "database": "corebanking",
            },
        },
        "unit_tests": {
            "unit_test.aml.payment_transfer.dedup": {"depends_on": {"nodes": ["model.aml.payment_transfer"]}},
        },
    }


def test_models_seeds_and_sources_become_objects() -> None:
    project = parse_manifest(_manifest())
    objects = {o.relation: o for o in project.objects}

    assert set(objects) == {
        "silver.payment_transfer",
        "reference.iso_currency",
        "bronze.payment_transfer",
        "corebanking.core.customer",
    }
    model = objects["silver.payment_transfer"]
    assert model.kind is ObjectKind.MODEL
    assert model.depends_on == ("bronze.payment_transfer", "reference.iso_currency")
    assert model.functions == {"upper": 1}
    assert model.macros == ("lineage_columns",)
    assert model.tests == 2
    assert objects["corebanking.core.customer"].kind is ObjectKind.SOURCE_TABLE
    assert project.translations == {"upper": ("upper",)}


def test_parse_only_manifest_is_refused() -> None:
    manifest = _manifest()
    del manifest["nodes"]["model.aml.payment_transfer"]["compiled_code"]

    with pytest.raises(ValueError, match="dbt compile"):
        parse_manifest(manifest)
