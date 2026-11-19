"""The compliance report's Power BI project, read and rendered for the cutover.

The source platform's report imports each table through one shared expression, `Lakehouse`, with
`Lakehouse{[Name = <schema>, Kind = "Schema"]}[Data]{[Name = <table>, Kind = "Table"]}[Data]`
then `Table.SelectColumns` and `Table.TransformColumnTypes`. The Databricks connector navigates a
catalog the same way, so the cutover changes the expressions only: `Lakehouse` reads Trino or the
SQL warehouse depending on one dataset parameter, `OnPremises` keeps Trino for the relations that
stay, and the relations retired by the migration leave the model with the visuals that read them.
The report is rendered from the pinned source rather than forked, so the diff is these changes.
"""

from __future__ import annotations

import json
import re
import shutil
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

MODEL = "AmlCompliance.SemanticModel"
REPORT = "AmlCompliance.Report"
SERVING_PLATFORMS = ("Databricks", "Trino")
ON_PREMISES = "OnPremises"

_TABLE = re.compile(r"^table (?:'(?P<quoted>(?:[^']|'')+)'|(?P<plain>\S+))", re.MULTILINE)
_NAVIGATION = re.compile(
    r'(?P<expression>\w+)\{\[Name = "(?P<schema>\w+)", Kind = "Schema"\]\}\[Data\]'
    r'\{\[Name = "(?P<table>\w+)", Kind = "Table"\]\}\[Data\]'
)
_SELECTED = re.compile(r"Table\.SelectColumns\(Source, \{(?P<columns>[^}]*)\}\)")
_TYPED = re.compile(r'\{"(?P<column>\w+)", (?P<type>type \w+|[\w.]+)\}')
# Model-wide names the cutover adds; the source's parameter names are kept for a rollback.
_LINEAGE = uuid.UUID("0b8a3c43-5d1e-5c7e-9f0a-6a4f1d2e7c11")


class ModelError(ValueError):
    pass


@dataclass(frozen=True)
class ImportedTable:
    """One table of the semantic model loaded from a lakehouse relation."""

    name: str
    file: Path
    expression: str
    relation: str
    # Column name -> Power Query type, in the order the model selects them.
    columns: Mapping[str, str]


@dataclass(frozen=True)
class Plan:
    on_premises: Mapping[str, str]
    retired: Mapping[str, str]
    parameter: str
    rollback_window_days: int

    @classmethod
    def read(cls, path: Path) -> Plan:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        report = raw["report"]
        plan = cls(
            on_premises=dict(report.get("on_premises") or {}),
            retired=dict(report.get("retired") or {}),
            parameter=raw["parameter"],
            rollback_window_days=int(raw["rollback_window_days"]),
        )
        both = set(plan.on_premises) & set(plan.retired)
        if both:
            raise ModelError(f"{path}: {sorted(both)} both on premises and retired")
        return plan

    def serving(self, relation: str) -> str:
        if relation in self.retired:
            return "retired"
        return "on_premises" if relation in self.on_premises else "target"


def imported_tables(model: Path) -> list[ImportedTable]:
    """The tables of `<model>/definition/tables` that import a relation, in file name order."""
    tables = []
    for file in sorted((model / "definition" / "tables").glob("*.tmdl")):
        text = file.read_text(encoding="utf-8")
        navigation = _NAVIGATION.search(text)
        if navigation is None:
            continue  # a calculated table
        name = _table_name(text, file)
        selected = _SELECTED.search(text)
        if selected is None:
            raise ModelError(f"{file.name}: imports {navigation['table']} without Table.SelectColumns")
        columns = [c.strip().strip('"') for c in selected["columns"].split(",")]
        types = {m["column"]: m["type"] for m in _TYPED.finditer(text[selected.end() :])}
        untyped = [c for c in columns if c not in types]
        if untyped:
            raise ModelError(f"{file.name}: no type for {untyped}")
        tables.append(
            ImportedTable(
                name=name,
                file=file,
                expression=navigation["expression"],
                relation=f"{navigation['schema']}.{navigation['table']}",
                columns={c: types[c] for c in columns},
            )
        )
    return tables


def _table_name(text: str, file: Path) -> str:
    found = _TABLE.search(text)
    if found is None:
        raise ModelError(f"{file.name}: no table declaration")
    return found["quoted"].replace("''", "'") if found["quoted"] else found["plain"]


def _quoted(name: str) -> str:
    return name if re.fullmatch(r"\w+", name) else "'" + name.replace("'", "''") + "'"


@dataclass(frozen=True)
class Target:
    """Where the rendered model's `Lakehouse` reads from once switched to Databricks."""

    host: str
    http_path: str
    catalog: str


def render(source: Path, out: Path, plan: Plan, target: Target) -> list[str]:
    """Writes the migrated project to `out` (replaced whole); returns the report visuals removed."""
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(source, out)
    model = out / MODEL
    tables = imported_tables(model)
    unknown = (set(plan.on_premises) | set(plan.retired)) - {t.relation for t in tables}
    if unknown:
        raise ModelError(f"the cutover plan places {sorted(unknown)}, which the report does not import")
    for table in tables:
        if table.expression != "Lakehouse":
            raise ModelError(f"{table.file.name}: reads {table.expression}, the cutover expects Lakehouse")

    retired = [t for t in tables if t.relation in plan.retired]
    for table in tables:
        if table.relation in plan.on_premises:
            text = table.file.read_text(encoding="utf-8")
            table.file.write_text(text.replace("Lakehouse{", f"{ON_PREMISES}{{", 1), encoding="utf-8")
    for table in retired:
        table.file.unlink()

    definition = model / "definition"
    (definition / "expressions.tmdl").write_text(_expressions(plan, target), encoding="utf-8")
    _rewrite_model(definition / "model.tmdl", plan, [t.name for t in retired])
    _rewrite_relationships(definition / "relationships.tmdl", [t.name for t in retired])
    return _remove_visuals(out / REPORT, [t.name for t in retired])


def _lineage_tag(name: str) -> str:
    # Stable across renders, so that a re-render after a pin move diffs only what changed.
    return str(uuid.uuid5(_LINEAGE, name))


def _parameter(name: str, value: str, meta: str, result_type: str, doc: str) -> str:
    return (
        f"/// {doc}\n"
        f"expression {name} = {value} meta [IsParameterQuery = true, {meta}IsParameterQueryRequired = true]\n"
        f"\tlineageTag: {_lineage_tag(name)}\n\n"
        f"\tannotation PBI_ResultType = {result_type}\n\n"
    )


def _expressions(plan: Plan, target: Target) -> str:
    platforms = ", ".join(f'"{p}"' for p in SERVING_PLATFORMS)
    trino = (
        'StarburstPresto.Contents(TrinoHost, TrinoPort, [Catalog = "lakehouse"])'
        '{[Name = "lakehouse", Kind = "Database"]}[Data]'
    )
    databricks = (
        "Databricks.Catalogs(DatabricksHost, DatabricksHttpPath, "
        '[Catalog = null, Database = null, EnableAutomaticProxyDiscovery = null, Implementation = "2.0"])'
        '{[Name = DatabricksCatalog, Kind = "Database"]}[Data]'
    )
    parts = [
        _parameter(
            plan.parameter,
            '"Databricks"',
            f'List = {{{platforms}}}, DefaultValue = "Databricks", Type = "Text", ',
            "Text",
            "Platform the published lakehouse is read from: Databricks since the cutover, Trino to roll back "
            "(docs/cutover.md).",
        ),
        _parameter(
            "DatabricksHost",
            json.dumps(target.host),
            'Type = "Text", ',
            "Text",
            "Server hostname of the SQL warehouse.",
        ),
        _parameter(
            "DatabricksHttpPath",
            json.dumps(target.http_path),
            'Type = "Text", ',
            "Text",
            "HTTP path of the SQL warehouse.",
        ),
        _parameter(
            "DatabricksCatalog",
            json.dumps(target.catalog),
            'Type = "Text", ',
            "Text",
            "Unity Catalog catalog of the platform.",
        ),
        _parameter(
            "TrinoHost", '"localhost"', 'Type = "Text", ', "Text", "Host of Trino's HTTPS listener, on premises."
        ),
        _parameter("TrinoPort", "8443", 'Type = "Number", ', "Number", "Port of Trino's HTTPS listener."),
        "/// The published lakehouse: reports never read a staging schema or branch.\n"
        "expression Lakehouse =\n"
        "\t\tlet\n"
        "\t\t    Source =\n"
        f'\t\t        if {plan.parameter} = "Databricks" then\n'
        f"\t\t            {databricks}\n"
        "\t\t        else\n"
        f"\t\t            {trino}\n"
        "\t\tin\n"
        "\t\t    Source\n"
        f"\tlineageTag: {_lineage_tag('Lakehouse')}\n\n"
        "\tannotation PBI_ResultType = Table\n\n"
        "\tannotation PBI_NavigationStepName = Navigation\n\n",
        "/// Relations that stay on premises after the cutover, read from Trino through the data gateway.\n"
        f"expression {ON_PREMISES} =\n"
        "\t\tlet\n"
        f"\t\t    Source = {trino}\n"
        "\t\tin\n"
        "\t\t    Source\n"
        f"\tlineageTag: {_lineage_tag(ON_PREMISES)}\n\n"
        "\tannotation PBI_ResultType = Table\n\n"
        "\tannotation PBI_NavigationStepName = Navigation\n",
    ]
    return "".join(parts)


def _rewrite_model(path: Path, plan: Plan, retired: list[str]) -> None:
    text = path.read_text(encoding="utf-8")
    for name in retired:
        text = text.replace(f"ref table {_quoted(name)}\n", "")
    order = re.search(r"annotation PBI_QueryOrder = (\[.*\])", text)
    if order is None:
        raise ModelError(f"{path.name}: no PBI_QueryOrder annotation")
    queries = [q for q in json.loads(order[1]) if q not in retired and q not in ("TrinoHost", "TrinoPort", "Lakehouse")]
    added = [plan.parameter, "DatabricksHost", "DatabricksHttpPath", "DatabricksCatalog", "TrinoHost", "TrinoPort"]
    rendered = json.dumps([*added, "Lakehouse", ON_PREMISES, *queries], separators=(",", ":"))
    path.write_text(text.replace(order[1], rendered), encoding="utf-8")


def _rewrite_relationships(path: Path, retired: list[str]) -> None:
    blocks = path.read_text(encoding="utf-8").strip().split("\n\n")
    kept = [b for b in blocks if not any(f"{_quoted(name)}." in b for name in retired)]
    path.write_text("\n\n".join(kept) + "\n", encoding="utf-8")


def _remove_visuals(report: Path, retired: list[str]) -> list[str]:
    removed = []
    for visual in sorted(report.glob("definition/pages/*/visuals/*/visual.json")):
        text = visual.read_text(encoding="utf-8")
        if any(re.search(rf'"Entity":\s*{re.escape(json.dumps(name))}', text) for name in retired):
            shutil.rmtree(visual.parent)
            removed.append(f"{visual.parent.parent.parent.name}/{visual.parent.name}")
    return removed
