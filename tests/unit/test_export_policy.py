import copy
from collections.abc import Callable
from typing import Any

import pyarrow as pa
import pytest
from conftest import EXPORT_POLICY, TABLES, inventory_of, resolve

from lakemig.export.policy import Treatment


def _problems(change: Callable[[dict[str, Any]], None]) -> tuple[str, ...]:
    raw = copy.deepcopy(EXPORT_POLICY)
    change(raw)
    return resolve(raw, inventory_of(TABLES)).problems


def _columns(raw: dict[str, Any], relation: str) -> dict[str, Any]:
    columns: dict[str, Any] = raw["tables"][relation]["columns"]
    return columns


def test_columns_not_listed_cross_or_stay_by_level() -> None:
    plan = resolve(EXPORT_POLICY, inventory_of(TABLES)).plan
    transfers = plan.table("bronze.payment_transfer")

    assert {c.source: c.treatment for c in transfers.columns if c.treatment is Treatment.KEEP} == {
        "transfer_id": Treatment.KEEP,
        "booked_at": Treatment.KEEP,
        "amount_paid": Treatment.KEEP,
        "payment_format": Treatment.KEEP,
        "_business_date": Treatment.KEEP,
        "_ingested_at": Treatment.KEEP,
    }


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        (
            lambda raw: _columns(raw, "bronze.corebanking_account").pop("rib"),
            "bronze.corebanking_account.rib: indirect identifier without a treatment",
        ),
        (
            lambda raw: _columns(raw, "bronze.payment_transfer").update(
                originator_account={"treatment": "pseudonymize"}
            ),
            "bronze.payment_transfer.originator_account: pseudonymize needs a namespace",
        ),
        (
            lambda raw: _columns(raw, "bronze.payment_transfer").update(memo="keep"),
            "bronze.payment_transfer.memo: in the export policy but not in the inventory",
        ),
        (
            lambda raw: raw["tables"]["bronze.payment_transfer"].update(order_by=["originator"]),
            "bronze.payment_transfer: order_by column originator does not cross",
        ),
        (
            lambda raw: _columns(raw, "bronze.payment_transfer").update(_business_date="drop"),
            "bronze.payment_transfer: business date column _business_date does not cross",
        ),
        (
            lambda raw: raw["tables"].pop("bronze.payment_transfer"),
            "bronze.payment_transfer: exported by the waves, absent from the export policy",
        ),
        (
            lambda raw: raw["gate"].update(
                exemptions=[{"table": "bronze.payment_transfer", "column": "rib", "detector": "phone", "reason": "r"}]
            ),
            "exemption bronze.payment_transfer.rib: not a column of the delivery",
        ),
        (
            lambda raw: raw["gate"].update(
                known_names={"table": "bronze.payment_transfer", "names": [["transfer_id"]]}
            ),
            "gate.known_names: bronze.payment_transfer must be a relation kept on premises",
        ),
    ],
)
def test_policy_problems(change: Callable[[dict[str, Any]], None], problem: str) -> None:
    assert problem in _problems(change)


def test_a_direct_identifier_cannot_be_listed_even_as_kept() -> None:
    accounts = TABLES["bronze.corebanking_account"].append_column("last_name", pa.array(["x"] * 4))
    tables = {**TABLES, "bronze.corebanking_account": accounts}
    raw = copy.deepcopy(EXPORT_POLICY)
    raw["tables"]["bronze.corebanking_account"]["columns"]["last_name"] = "keep"

    problems = resolve(raw, inventory_of(tables)).problems

    assert (
        "bronze.corebanking_account.last_name: direct identifier, never crosses: remove it from the policy" in problems
    )
