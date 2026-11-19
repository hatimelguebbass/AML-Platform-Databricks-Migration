from lakemig.inventory.access import parse_rules
from lakemig.inventory.model import AccessRule


def test_rules_keep_their_evaluation_order_per_scope() -> None:
    rules = parse_rules(
        {
            "catalogs": [{"user": "bi", "catalog": "lakehouse", "allow": "read-only"}, {"allow": "none"}],
            "tables": [{"user": "bi", "catalog": "lakehouse", "schema": "restricted", "privileges": []}],
        }
    )

    assert rules == (
        AccessRule("bi", "catalog", "lakehouse", None, "read-only"),
        AccessRule(".*", "catalog", None, None, "none"),
        AccessRule("bi", "table", "lakehouse", "restricted", "none"),
    )
