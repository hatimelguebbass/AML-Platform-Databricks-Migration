"""A small source lakehouse with the shapes of the real one: an Iceberg catalog in a temporary
directory, its inventory, classification, waves and export policy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from pyiceberg.catalog.memory import InMemoryCatalog

from lakemig.assessment.classification import Policy, classify
from lakemig.assessment.waves import Plan
from lakemig.export import gate, policy
from lakemig.export.pseudonym import Key
from lakemig.export.run import Context
from lakemig.export.source import IcebergSource
from lakemig.inventory.model import Column, DataObject, Inventory, ObjectKind

DAY = date(2022, 9, 1)
NEXT_DAY = date(2022, 9, 2)
_TS = pa.timestamp("us")
_TSTZ = pa.timestamp("us", tz="UTC")

TABLES: dict[str, pa.Table] = {
    "restricted.customer_identity": pa.table(
        {
            "customer_id": pa.array([1, 2, 3], pa.int64()),
            "first_name": ["Salma", "Youssef", None],
            "last_name": ["Benjelloun", "El Amrani", None],
            "legal_name": [None, None, "Atlas Négoce SARL"],
        }
    ),
    # r0 is the initial snapshot, captured before the daily runs began: it has no business date.
    "audit.change_batch": pa.table(
        {
            "run_id": ["r0", "r1", "r1", "r2"],
            "target_table": ["bronze.corebanking_account"] * 4,
            "topic": ["corebanking.core.account"] * 4,
            "kafka_partition": pa.array([0, 0, 1, 0], pa.int32()),
            "business_date": pa.array([None, DAY, DAY, NEXT_DAY], pa.date32()),
            "processed_at": pa.array(
                [datetime(2022, 8, 31, tzinfo=UTC)] + [datetime(2022, 9, 2, tzinfo=UTC)] * 3, _TSTZ
            ),
        }
    ),
    "bronze.corebanking_account": pa.table(
        {
            "account_id": pa.array([9, 10, 11, 12], pa.int64()),
            "customer_id": pa.array([1, 1, 2, 3], pa.int64()),
            "account_number": ["800100200", "8000EBD30", "80012ACA0", "800737690"],
            "rib": ["011780000012345678901234"] * 4,
            "_kafka_topic": ["corebanking.core.account"] * 4,
            "_kafka_partition": pa.array([0, 0, 1, 0], pa.int32()),
            "_kafka_offset": pa.array([0, 1, 0, 2], pa.int64()),
            "_ingest_run_id": ["r0", "r1", "r1", "r2"],
        }
    ),
    "bronze.payment_transfer": pa.table(
        {
            "transfer_id": ["TRF000000002", "TRF000000001", "TRF000000003"],
            "booked_at": pa.array([datetime(2022, 9, 1, 10), datetime(2022, 9, 1, 9), datetime(2022, 9, 2, 8)], _TS),
            "originator_account": ["8000EBD30", "80012ACA0", "8000EBD30"],
            "beneficiary_account": ["80012ACA0", "800737690", None],
            "amount_paid": pa.array([Decimal("10.50"), Decimal("2500.000001"), Decimal("1")], pa.decimal128(22, 6)),
            "payment_format": ["Wire", "ACH", "Cheque"],
            "_business_date": pa.array([DAY, DAY, NEXT_DAY], pa.date32()),
            "_ingested_at": pa.array([datetime(2022, 9, 2, tzinfo=UTC)] * 3, _TSTZ),
        }
    ),
}

CLASSIFICATION_RULES: dict[str, Any] = {
    "rules": [
        {"pattern": "(first|last|legal)_name", "level": "direct", "reason": "name"},
        {
            "pattern": "customer_id|account_id|account_number|rib|(originator|beneficiary)_account",
            "level": "indirect",
            "reason": "key",
        },
        {"pattern": ".*", "level": "none", "reason": "everything else"},
    ]
}
WAVES: dict[str, Any] = {
    "waves": [
        {
            "wave": 1,
            "name": "All",
            "goal": "g",
            "objects": {
                "audit.change_batch": "export",
                "bronze.corebanking_account": "export",
                "bronze.payment_transfer": "export",
            },
        }
    ],
    "on_premises": {"restricted.customer_identity": "identities"},
}
EXPORT_POLICY: dict[str, Any] = {
    "first_business_date": DAY,
    "tables": {
        "audit.change_batch": {
            "selection": "capture_runs",
            "order_by": ["run_id", "kafka_partition"],
        },
        "bronze.corebanking_account": {
            "selection": {"capture_runs": "bronze.corebanking_account"},
            "order_by": ["_kafka_topic", "_kafka_partition", "_kafka_offset"],
            "columns": {
                "account_id": "keep",
                "customer_id": "keep",
                "account_number": {"treatment": "pseudonymize", "namespace": "account_number"},
                "rib": "drop",
            },
        },
        "bronze.payment_transfer": {
            "selection": {"business_date": "_business_date"},
            "order_by": ["transfer_id"],
            "columns": {
                "originator_account": {"treatment": "pseudonymize", "namespace": "account_number"},
                "beneficiary_account": {"treatment": "pseudonymize", "namespace": "account_number"},
            },
        },
    },
    "gate": {
        "known_names": {
            "table": "restricted.customer_identity",
            "names": [["first_name", "last_name"], ["legal_name"]],
        },
        "exemptions": [],
    },
}


@dataclass
class Bank:
    catalog: InMemoryCatalog
    source: IcebergSource
    inventory: Inventory
    plan: policy.ExportPlan
    key: Key
    context: Context

    def replace(self, relation: str, table: pa.Table) -> None:
        self.catalog.load_table(relation).overwrite(table)


def inventory_of(tables: dict[str, pa.Table]) -> Inventory:
    objects = tuple(
        DataObject(
            relation.split(".")[0],
            relation.split(".")[1],
            ObjectKind.SOURCE_TABLE,
            tuple(Column(f.name, str(f.type)) for f in table.schema),
        )
        for relation, table in tables.items()
    )
    return Inventory("repo", "abc", "2026-11-14T00:00:00+00:00", "484", objects, (), {})


def resolve(raw: dict[str, Any], inventory: Inventory) -> policy.Resolution:
    classification = classify(inventory.objects, Policy.from_dict(CLASSIFICATION_RULES))
    return policy.resolve(raw, inventory, classification, Plan.from_dict(WAVES), gate.DETECTOR_NAMES)


@pytest.fixture
def bank(tmp_path: Path) -> Bank:
    catalog = InMemoryCatalog("source", warehouse=(tmp_path / "warehouse").as_uri())
    for relation, table in TABLES.items():
        namespace = relation.split(".")[0]
        if (namespace,) not in catalog.list_namespaces():
            catalog.create_namespace(namespace)
        catalog.create_table(relation, schema=table.schema).append(table)
    inventory = inventory_of(TABLES)
    resolution = resolve(EXPORT_POLICY, inventory)
    assert resolution.problems == ()
    return Bank(
        catalog=catalog,
        source=IcebergSource(catalog, "main@0123abcd"),
        inventory=inventory,
        plan=resolution.plan,
        key=Key(bytes(range(32))),
        context=Context("https://example.org/source", "abc", "policy-digest"),
    )
