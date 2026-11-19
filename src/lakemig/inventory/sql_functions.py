"""Functions the source SQL calls, and what the translator makes of each one in Databricks SQL.

This measures the conversion effort before it starts: a function the translator renders under the
same name usually needs no work, a renamed one needs its semantics checked (argument order, null
and rounding behaviour), and one it does not know is passed through verbatim and must be ported by
hand.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

import sqlglot
from sqlglot import exp

SOURCE_DIALECT = "trino"
TARGET_DIALECT = "databricks"

_CALL = re.compile(r"^([A-Za-z_][A-Za-z0-9_.]*)\s*\(")
_WRITTEN_CALL = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")


@dataclass(frozen=True, order=True)
class FunctionCall:
    name: str
    # Name the translator renders the call with in Databricks SQL; None when it does not know the
    # function and copies the call unchanged.
    target_name: str | None


def function_calls(sql: str) -> Counter[FunctionCall]:
    calls: Counter[FunctionCall] = Counter()
    written = {m.lower() for m in _WRITTEN_CALL.findall(sql)}
    for statement in sqlglot.parse(sql, read=SOURCE_DIALECT):
        if statement is None:
            continue
        for node in statement.find_all(exp.Func):
            source = _call_name(node, SOURCE_DIALECT)
            # The parser normalizes some syntax into function nodes (CASE into IF, for one): only
            # calls the source SQL actually writes count.
            if source is None or source not in written:
                continue
            target = None if isinstance(node, exp.Anonymous) else _call_name(node, TARGET_DIALECT)
            calls[FunctionCall(source, target)] += 1
    return calls


def _call_name(node: exp.Func, dialect: str) -> str | None:
    # Only what renders as a call counts: operators and predicates are expressions too.
    match = _CALL.match(node.sql(dialect=dialect))
    return match.group(1).lower() if match else None
