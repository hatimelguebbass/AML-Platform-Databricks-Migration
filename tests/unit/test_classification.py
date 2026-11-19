from lakemig.assessment.classification import Level, Policy, classify
from lakemig.inventory.model import Column, DataObject, ObjectKind

POLICY = Policy.from_dict(
    {
        "rules": [
            {"pattern": "(first|last)_name", "level": "direct", "reason": "names a person"},
            {"pattern": "birth_date", "level": "indirect", "reason": "quasi-identifier"},
            {"pattern": "_.*|.*_id", "level": "none", "reason": "technical"},
        ],
        "overrides": [
            {"table": "gold.sanctions_hit", "column": "customer_id", "level": "indirect", "reason": "linked to a hit"},
        ],
    }
)


def _object(relation: str, *columns: str) -> DataObject:
    schema, name = relation.split(".")
    return DataObject(schema, name, ObjectKind.MODEL, tuple(Column(c, "varchar") for c in columns))


def test_first_matching_rule_wins_and_overrides_take_precedence() -> None:
    result = classify(
        [
            _object("silver.customer", "first_name", "birth_date", "customer_id"),
            _object("gold.sanctions_hit", "customer_id"),
        ],
        POLICY,
    )

    assert result.of("silver.customer") == {
        "first_name": Level.DIRECT,
        "birth_date": Level.INDIRECT,
        "customer_id": Level.NONE,
    }
    assert result.of("gold.sanctions_hit") == {"customer_id": Level.INDIRECT}
    assert result.problems() == []


def test_rules_match_whole_names_only() -> None:
    result = classify([_object("silver.customer", "first_name_key")], POLICY)

    assert result.unclassified == (("silver.customer", "first_name_key"),)


def test_stale_override_is_reported() -> None:
    result = classify([_object("silver.customer", "customer_id")], POLICY)

    assert result.problems() == ["override gold.sanctions_hit.customer_id matches no inventoried column"]
