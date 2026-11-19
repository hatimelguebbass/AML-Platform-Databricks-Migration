from typing import Any

import pytest

from lakemig.assessment.classification import Policy, classify
from lakemig.assessment.waves import Plan, check
from lakemig.inventory.model import Column, DataObject, Inventory, ObjectKind


def _inventory(*objects: DataObject) -> Inventory:
    return Inventory("repo", "abc", "2026-11-14T00:00:00+00:00", "484", objects, (), {})


def _object(relation: str, *depends_on: str, columns: tuple[str, ...] = ("id",)) -> DataObject:
    schema, name = relation.split(".")
    return DataObject(schema, name, ObjectKind.MODEL, tuple(Column(c, "varchar") for c in columns), "table", depends_on)


INVENTORY = _inventory(
    _object("bronze.payment"),
    _object("silver.payment", "bronze.payment"),
    _object("gold.alert", "silver.payment"),
    _object("restricted.identity", columns=("id", "full_name")),
    _object("audit.run"),
)
POLICY = Policy.from_dict(
    {
        "rules": [
            {"pattern": "full_name", "level": "direct", "reason": "name"},
            {"pattern": "id", "level": "none", "reason": "key"},
        ]
    }
)


def _plan(**overrides: Any) -> Plan:
    raw: dict[str, Any] = {
        "waves": [
            {
                "wave": 1,
                "name": "Payments",
                "goal": "g",
                "objects": {"bronze.payment": "export", "silver.payment": "rebuild"},
            },
            {"wave": 2, "name": "Rules", "goal": "g", "objects": {"gold.alert": "rebuild"}},
        ],
        "on_premises": {"restricted.identity": "names"},
        "retired": {"audit.run": "job run history"},
    }
    raw.update(overrides)
    return Plan.from_dict(raw)


def _problems(plan: Plan) -> list[str]:
    return check(plan, INVENTORY, classify(INVENTORY.objects, POLICY))


def test_consistent_plan_has_no_problem() -> None:
    assert _problems(_plan()) == []


def test_every_object_must_be_placed_exactly_once() -> None:
    plan = _plan(retired={"audit.run": "x", "silver.payment": "x", "gold.missing": "x"})

    assert _problems(plan) == [
        "gold.missing: placed but not in the inventory",
        "silver.payment: placed 2 times",
    ]


def test_rebuilt_object_cannot_read_a_later_wave_or_what_stays() -> None:
    plan = _plan(
        waves=[
            {"wave": 1, "name": "Rules", "goal": "g", "objects": {"gold.alert": "rebuild"}},
            {"wave": 2, "name": "Payments", "goal": "g", "objects": {"silver.payment": "rebuild"}},
        ],
        on_premises={"restricted.identity": "names", "bronze.payment": "x"},
    )

    assert _problems(plan) == [
        "gold.alert: wave 1 reads silver.payment of later wave 2",
        "silver.payment: rebuilt in the cloud but reads bronze.payment, which does not migrate",
    ]


def test_rebuilt_object_cannot_hold_direct_identifiers() -> None:
    plan = _plan(
        on_premises={},
        waves=[
            {
                "wave": 1,
                "name": "All",
                "goal": "g",
                "objects": {
                    "bronze.payment": "export",
                    "silver.payment": "rebuild",
                    "gold.alert": "rebuild",
                    "restricted.identity": "rebuild",
                },
            },
        ],
    )

    assert _problems(plan) == ["restricted.identity: rebuilt in the cloud with direct identifiers full_name"]


def test_waves_must_be_numbered_in_order() -> None:
    with pytest.raises(ValueError, match="numbered"):
        _plan(waves=[{"wave": 2, "name": "x", "goal": "g", "objects": {}}])
