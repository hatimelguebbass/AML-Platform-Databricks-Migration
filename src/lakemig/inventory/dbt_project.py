"""Models, seeds and sources of the source dbt project, read from a compiled `manifest.json`."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lakemig.inventory.model import Column, DataObject, ObjectKind
from lakemig.inventory.sql_functions import FunctionCall, function_calls

# The lakehouse catalogs of the source query engine: main for readers, etl for the staging branch.
LAKEHOUSE_CATALOGS = frozenset({"lakehouse", "lakehouse_etl"})


@dataclass(frozen=True)
class DbtProject:
    objects: tuple[DataObject, ...]
    dbt_version: str
    # Source function name -> the names the translator renders it with, over every model.
    translations: dict[str, tuple[str | None, ...]]


def load_manifest(path: Path) -> DbtProject:
    return parse_manifest(json.loads(path.read_text(encoding="utf-8")))


def parse_manifest(manifest: Mapping[str, Any]) -> DbtProject:
    relations = {uid: _relation(node) for uid, node in _nodes_and_sources(manifest)}
    tests = _tests_per_node(manifest)
    objects: list[DataObject] = []
    translations: dict[str, set[str | None]] = {}
    for uid, node in manifest["nodes"].items():
        if node["resource_type"] not in ("model", "seed"):
            continue
        schema, name = relations[uid]
        compiled = node.get("compiled_code")
        if node["resource_type"] == "model" and compiled is None:
            raise ValueError(f"{uid} has no compiled SQL: build the manifest with `dbt compile`, not `dbt parse`")
        calls: Counter[FunctionCall] = function_calls(compiled) if compiled else Counter()
        used: Counter[str] = Counter()
        for call, count in calls.items():
            used[call.name] += count
            translations.setdefault(call.name, set()).add(call.target_name)
        objects.append(
            DataObject(
                schema=schema,
                name=name,
                kind=ObjectKind.MODEL if node["resource_type"] == "model" else ObjectKind.SEED,
                columns=tuple(Column(c["name"], c.get("data_type") or "") for c in node.get("columns", {}).values()),
                materialization=node["config"].get("materialized"),
                depends_on=tuple(sorted({".".join(relations[d]) for d in node["depends_on"].get("nodes", ())})),
                functions=dict(sorted(used.items())),
                macros=tuple(sorted(_project_macros(node["depends_on"].get("macros", ()), manifest["metadata"]))),
                tests=tests[uid],
            )
        )
    for uid in manifest["sources"]:
        schema, name = relations[uid]
        objects.append(DataObject(schema=schema, name=name, kind=ObjectKind.SOURCE_TABLE, columns=()))
    return DbtProject(
        objects=tuple(sorted(objects, key=lambda o: o.relation)),
        dbt_version=manifest["metadata"]["dbt_version"],
        translations={
            name: tuple(sorted(targets, key=lambda t: (t is None, t or "")))
            for name, targets in sorted(translations.items())
        },
    )


def _nodes_and_sources(manifest: Mapping[str, Any]) -> Iterable[tuple[str, Mapping[str, Any]]]:
    yield from ((uid, n) for uid, n in manifest["nodes"].items() if n["resource_type"] in ("model", "seed", "snapshot"))
    yield from manifest["sources"].items()


def _relation(node: Mapping[str, Any]) -> tuple[str, str]:
    name = node.get("alias") or node.get("identifier") or node["name"]
    database = node.get("database")
    # Sources outside the lakehouse (the core banking database behind its own catalog) keep their
    # catalog in the schema, so that they never collide with a lakehouse schema.
    if node["resource_type"] == "source" and database and database not in LAKEHOUSE_CATALOGS:
        return f"{database}.{node['schema']}", name
    return node["schema"], name


def _tests_per_node(manifest: Mapping[str, Any]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for node in manifest["nodes"].values():
        if node["resource_type"] == "test":
            attached = node.get("attached_node")
            targets = [attached] if attached else node["depends_on"].get("nodes", [])
            counts.update(t for t in targets if t)
    for unit_test in manifest.get("unit_tests", {}).values():
        counts.update(unit_test["depends_on"].get("nodes", [])[:1])
    return counts


def _project_macros(macros: Iterable[str], metadata: Mapping[str, Any]) -> set[str]:
    # Only the project's own macros need porting; dbt and adapter macros come with dbt-databricks.
    project = metadata.get("project_name")
    prefix = f"macro.{project}." if project else "macro."
    return {m.removeprefix(prefix) for m in macros if project and m.startswith(prefix)}
