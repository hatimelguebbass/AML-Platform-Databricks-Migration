"""Assembles the inventory from the pinned source checkout, its compiled dbt manifest and its live engine."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from lakemig.inventory.access import load_rules
from lakemig.inventory.dbt_project import DbtProject
from lakemig.inventory.model import DataObject, Inventory, ObjectKind
from lakemig.inventory.trino import CatalogTable, Query, collect_catalog, engine_version
from lakemig.source import SourcePin

LAKEHOUSE = "lakehouse"
# The core banking database, exposed read-only to the engine for reconciliation.
COREBANKING = "corebanking"
ACCESS_RULES = Path("infra/trino/rules.json")


def collect(pin: SourcePin, checkout: Path, project: DbtProject, query: Query) -> Inventory:
    pin.verify_checkout(checkout)
    live = collect_catalog(query, LAKEHOUSE, iceberg=True)
    live += [
        replace(t, schema=f"{COREBANKING}.{t.schema}")
        for t in collect_catalog(query, COREBANKING, iceberg=False, schemas=["core"])
    ]
    return Inventory(
        source_repository=pin.repository,
        source_commit=pin.commit,
        collected_at=datetime.now(UTC).replace(microsecond=0).isoformat(),
        engine_version=engine_version(query),
        objects=merge(project.objects, live),
        access_rules=load_rules(checkout / ACCESS_RULES),
        function_translations=project.translations,
    )


def merge(declared: tuple[DataObject, ...], live: list[CatalogTable]) -> tuple[DataObject, ...]:
    """Declared objects take the columns and volume the engine reports; undeclared tables are added.

    Every declared object except ephemeral models must exist in the engine: a missing one means
    the source was not fully built, and the inventory would understate what has to move.
    """
    objects = {o.relation: o for o in declared}
    for table in live:
        relation = f"{table.schema}.{table.name}"
        known = objects.get(relation)
        if known is None:
            objects[relation] = DataObject(
                schema=table.schema,
                name=table.name,
                kind=ObjectKind.SOURCE_TABLE,
                columns=table.columns,
                volume=table.volume,
            )
        else:
            objects[relation] = replace(known, columns=table.columns, volume=table.volume)
    missing = sorted(o.relation for o in objects.values() if o.volume is None and o.materialization != "ephemeral")
    if missing:
        raise IncompleteSourceError(f"declared but absent from the engine: {', '.join(missing)}")
    return tuple(sorted(objects.values(), key=lambda o: o.relation))


class IncompleteSourceError(Exception):
    pass
