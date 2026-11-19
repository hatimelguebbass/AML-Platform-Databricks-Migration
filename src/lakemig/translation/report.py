"""How much of the source SQL an automatic translation carries over, measured on the target's data.

For every ported model, the source's compiled Trino SQL is transpiled to Databricks SQL by sqlglot,
run on the warehouse against the same inputs as the port, and its rows compared with the table the
port built (lineage columns aside). A model is auto-translated when sqlglot's query gives the port's
rows exactly. The port's hand changes, and why each was needed, are reviewed in
`migration/translation.yaml`; the measure says which of them were necessary for the result.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import sqlglot
import yaml
from sqlglot import exp
from sqlglot.errors import ErrorLevel, SqlglotError

# Written per run or per delivery: they differ between any two builds.
LINEAGE = frozenset({"_pipeline_run_id", "_loaded_at", "_delivery_id"})
FORMAT_VERSION = 1


class Outcome(StrEnum):
    # sqlglot's query returns the port's rows.
    IDENTICAL = "identical"
    # It runs, with other rows.
    DIFFERENT = "different"
    # The warehouse rejects it (unknown column or function, type error).
    FAILS = "fails"
    # sqlglot cannot translate it.
    UNTRANSLATED = "untranslated"


class ChangeKind(StrEnum):
    # The hybrid boundary changed the input: a column reduced, renamed or resolved on premises.
    BOUNDARY = "boundary"
    # The same SQL means something else on Databricks (time zones, decimal scale, ordering).
    SEMANTICS = "semantics"
    # Types of the contract or of an expression.
    TYPES = "types"
    # Syntax Databricks SQL does not have.
    SYNTAX = "syntax"
    # A Delta or Unity Catalog feature replaces a Trino or Iceberg one.
    ENGINE = "engine"


@dataclass(frozen=True)
class Change:
    change: str
    reason: str
    kind: ChangeKind


@dataclass(frozen=True)
class HandChanges:
    common: tuple[Change, ...]
    models: Mapping[str, tuple[Change, ...]]

    @classmethod
    def read(cls, path: Path) -> HandChanges:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        return cls(
            common=tuple(_change(c) for c in raw.get("common", ())),
            models={relation: tuple(_change(c) for c in changes or ()) for relation, changes in raw["models"].items()},
        )

    def problems(self, ported: Collection[str]) -> list[str]:
        found = [f"translation.yaml: no entry for ported model {r}" for r in sorted(set(ported) - set(self.models))]
        found += [f"translation.yaml: {r} is not a ported model" for r in sorted(set(self.models) - set(ported))]
        return found


def _change(raw: Mapping[str, Any]) -> Change:
    return Change(raw["change"].strip(), raw["reason"].strip(), ChangeKind(raw["kind"]))


@dataclass(frozen=True)
class ModelResult:
    relation: str
    outcome: Outcome
    # Functions sqlglot renamed or rewrote, source name -> target name.
    rewritten_functions: Mapping[str, str] = field(default_factory=dict)
    source_lines: int = 0
    port_rows: int | None = None
    only_in_translation: int | None = None
    only_in_port: int | None = None
    error: str | None = None


@dataclass(frozen=True)
class Report:
    format_version: int
    sqlglot_version: str
    source_commit: str
    engine_version: str
    measured_on: str
    models: tuple[ModelResult, ...]

    def write(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), indent=2, sort_keys=False) + "\n", encoding="utf-8")

    @classmethod
    def read(cls, path: Path) -> Report:
        raw = json.loads(path.read_text(encoding="utf-8"))
        models = tuple(ModelResult(**{**m, "outcome": Outcome(m["outcome"])}) for m in raw.pop("models"))
        return cls(**raw, models=models)

    def count(self, outcome: Outcome) -> int:
        return sum(1 for m in self.models if m.outcome is outcome)


def transpile(sql: str) -> tuple[str, dict[str, str]]:
    """Databricks SQL of a compiled Trino model, and the functions the translation changed."""
    tree = sqlglot.parse_one(sql, read="trino", error_level=ErrorLevel.RAISE)
    before = [_call_shape(node, "trino") for node in tree.find_all(exp.Func)]
    translated = sqlglot.transpile(sql, read="trino", write="databricks", unsupported_level=ErrorLevel.RAISE)[0]
    after = [_call_shape(node, "databricks") for node in tree.find_all(exp.Func)]
    # `current_timestamp` and `current_timestamp()` are the same call written two ways.
    rewritten = {b: a for b, a in zip(before, after, strict=True) if b.removesuffix("()") != a.removesuffix("()")}
    return translated, dict(sorted(rewritten.items()))


def _call_shape(node: exp.Func, dialect: str) -> str:
    """A call with its arguments elided: `to_utf8(…)` becomes `encode(…, 'utf-8')` in Databricks SQL."""
    rendered = node.sql(dialect=dialect)
    for child in node.iter_expressions():
        if not isinstance(child, exp.Literal) and (child_sql := child.sql(dialect=dialect)):
            rendered = rendered.replace(child_sql, "…", 1)
    return rendered.lower()


def comparison_sql(translated: str, port_table: str, columns: Sequence[str]) -> str:
    """Rows of each side missing from the other, multiset semantics, and the port's row count."""
    cols = ", ".join(f"`{c}`" for c in columns)
    return (
        # Generated and reviewed SQL of the two projects, run by its author: not user input.
        f"with translation as (select {cols} from ({translated})), "  # noqa: S608
        f"port as (select {cols} from {port_table}) "
        "select "
        "(select count(*) from (select * from translation except all select * from port)) as only_in_translation, "
        "(select count(*) from (select * from port except all select * from translation)) as only_in_port, "
        "(select count(*) from port) as port_rows"
    )


# Runs a statement on the warehouse: (rows, None), or (None, error message).
Execute = Callable[[str], tuple[list[list[str | None]] | None, str | None]]


def measure(relation: str, source_sql: str, port_table: str, columns: Sequence[str], execute: Execute) -> ModelResult:
    lines = len([line for line in source_sql.splitlines() if line.strip()])
    try:
        translated, rewritten = transpile(source_sql)
    except SqlglotError as failure:
        return ModelResult(relation, Outcome.UNTRANSLATED, source_lines=lines, error=_first_line(str(failure)))
    rows, error = execute(comparison_sql(translated, port_table, [c for c in columns if c not in LINEAGE]))
    if rows is None:
        return ModelResult(relation, Outcome.FAILS, rewritten, lines, error=_first_line(error or ""))
    only_translation, only_port, port_rows = (int(v or 0) for v in rows[0])
    outcome = Outcome.IDENTICAL if only_translation == only_port == 0 else Outcome.DIFFERENT
    return ModelResult(relation, outcome, rewritten, lines, port_rows, only_translation, only_port)


def _first_line(message: str) -> str:
    # Warehouse errors carry the query plan and a request id after the first line.
    return message.strip().splitlines()[0][:300] if message.strip() else "no message"


HEADER = "<!-- Generated by `make assessment` from migration/translation.{json,yaml}. Edit those, not this file. -->\n"


def translation_md(report: Report, changes: HandChanges, source_repository: str) -> str:
    total = len(report.models)
    out = [
        HEADER,
        "# SQL translation report\n",
        f"Each ported model's compiled source SQL (Trino, [{source_repository.rsplit('/', 1)[-1]}]"
        f"({source_repository}) at `{report.source_commit[:12]}`) was transpiled to Databricks SQL by "
        f"sqlglot {report.sqlglot_version}, run on the dev SQL warehouse ({report.engine_version}), "
        "and its rows compared with the table the reviewed port built, lineage "
        "columns aside. Measured by `make translation`.\n",
        "| Outcome | Models |",
        "|---|---|",
        *(f"| {o.value} | {report.count(o)} of {total} |" for o in Outcome),
        "",
        "Outcomes: `identical` gives the port's rows; `different` runs with other rows; `fails` is rejected "
        "by the warehouse; `untranslated` is rejected by sqlglot. The seven AML rules are ephemeral models "
        "inlined into `gold.aml_alert_transfer` and measured with it.\n",
        "## Models\n",
        "| Model | Source lines | Outcome | Rows of the port | Only in translation | Only in port | "
        "Functions sqlglot rewrote |",
        "|---|---|---|---|---|---|---|",
    ]
    for m in report.models:
        rewritten = ", ".join(f"`{k}` → `{v}`" for k, v in m.rewritten_functions.items())
        detail = m.outcome.value if m.error is None else f"{m.outcome.value}: {m.error}"
        out.append(
            f"| `{m.relation}` | {m.source_lines} | {_cell(detail)} | {_n(m.port_rows)} | "
            f"{_n(m.only_in_translation)} | {_n(m.only_in_port)} | {rewritten} |"
        )
    out += ["", "## Hand changes\n", "Reviewed in `migration/translation.yaml`.\n", "Applied to every model:\n"]
    out += [f"- **{c.kind.value}**: {c.change}. {c.reason}" for c in changes.common]
    for m in report.models:
        model_changes = changes.models.get(m.relation, ())
        out.append(f"\n`{m.relation}`:\n")
        out += [f"- **{c.kind.value}**: {c.change}. {c.reason}" for c in model_changes] or ["- None."]
    return "\n".join(out) + "\n"


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


def _n(value: int | None) -> str:
    return "" if value is None else f"{value:,}"
