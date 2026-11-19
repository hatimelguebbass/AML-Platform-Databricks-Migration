"""Access rules of the source query engine (Trino file-based access control)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from lakemig.inventory.model import AccessRule

_SCOPES = ("catalogs", "schemas", "tables")


def load_rules(path: Path) -> tuple[AccessRule, ...]:
    return parse_rules(json.loads(path.read_text(encoding="utf-8")))


def parse_rules(raw: dict[str, Any]) -> tuple[AccessRule, ...]:
    rules = []
    for scope in _SCOPES:
        for rule in raw.get(scope, ()):
            rules.append(
                AccessRule(
                    users=rule.get("user", ".*"),
                    scope=scope.removesuffix("s"),
                    catalog=rule.get("catalog"),
                    schema=rule.get("schema"),
                    allow=_allow(scope, rule),
                )
            )
    return tuple(rules)


def _allow(scope: str, rule: dict[str, Any]) -> str:
    if scope == "tables":
        return ", ".join(rule["privileges"]) or "none"
    if scope == "schemas":
        return "owner" if rule.get("owner") else "not owner"
    return str(rule["allow"])
