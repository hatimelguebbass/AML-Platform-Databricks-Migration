"""Migration waves (`migration/waves.yaml`) and the checks that keep them consistent with the source.

Every inventoried object has exactly one fate:
- exported: computed on premises, shipped without its direct identifiers, landed as is;
- rebuilt: recomputed in the cloud from objects of the same or an earlier wave;
- kept on premises: it needs direct identifiers or a system that stays (law 09-08, ADR 0003);
- retired: replaced by a mechanism of the target platform.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from lakemig.assessment.classification import Classification, Level
from lakemig.inventory.model import DataObject, Inventory


class Fate(StrEnum):
    EXPORT = "export"
    REBUILD = "rebuild"
    ON_PREMISES = "on_premises"
    RETIRE = "retire"


@dataclass(frozen=True)
class Wave:
    number: int
    name: str
    goal: str
    objects: dict[str, Fate]


@dataclass(frozen=True)
class Plan:
    waves: tuple[Wave, ...]
    on_premises: dict[str, str]
    retired: dict[str, str]

    @classmethod
    def read(cls, path: Path) -> Plan:
        return cls.from_dict(yaml.safe_load(path.read_text(encoding="utf-8")))

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Plan:
        waves = tuple(
            Wave(
                number=w["wave"],
                name=w["name"],
                goal=w["goal"],
                objects={relation: Fate(fate) for relation, fate in w["objects"].items()},
            )
            for w in raw["waves"]
        )
        numbers = [w.number for w in waves]
        if numbers != list(range(1, len(waves) + 1)):
            raise ValueError(f"waves must be numbered 1..n in order, got {numbers}")
        for wave in waves:
            invalid = {r for r, f in wave.objects.items() if f not in (Fate.EXPORT, Fate.REBUILD)}
            if invalid:
                raise ValueError(f"wave {wave.number}: only export or rebuild, got {sorted(invalid)}")
        return cls(waves=waves, on_premises=dict(raw.get("on_premises", {})), retired=dict(raw.get("retired", {})))

    def placements(self) -> dict[str, list[tuple[Fate, int | None]]]:
        found: dict[str, list[tuple[Fate, int | None]]] = {}
        for wave in self.waves:
            for relation, fate in wave.objects.items():
                found.setdefault(relation, []).append((fate, wave.number))
        for relation in self.on_premises:
            found.setdefault(relation, []).append((Fate.ON_PREMISES, None))
        for relation in self.retired:
            found.setdefault(relation, []).append((Fate.RETIRE, None))
        return found


def check(plan: Plan, inventory: Inventory, classification: Classification) -> list[str]:
    objects = inventory.by_relation()
    placements = plan.placements()
    problems = [f"{r}: not placed in any wave, on premises or retired" for r in sorted(objects.keys() - placements)]
    problems += [f"{r}: placed but not in the inventory" for r in sorted(placements.keys() - objects.keys())]
    problems += [f"{r}: placed {len(p)} times" for r, p in sorted(placements.items()) if len(p) > 1 and r in objects]
    wave_of = {relation: wave.number for wave in plan.waves for relation in wave.objects}
    for wave in plan.waves:
        for relation, fate in wave.objects.items():
            obj = objects.get(relation)
            if obj is None or fate is not Fate.REBUILD:
                continue
            problems += _rebuild_problems(obj, wave.number, wave_of, classification)
    return problems


def _rebuild_problems(
    obj: DataObject, wave: int, wave_of: Mapping[str, int], classification: Classification
) -> list[str]:
    problems = []
    for dependency in obj.depends_on:
        dependency_wave = wave_of.get(dependency)
        if dependency_wave is None:
            problems.append(f"{obj.relation}: rebuilt in the cloud but reads {dependency}, which does not migrate")
        elif dependency_wave > wave:
            problems.append(f"{obj.relation}: wave {wave} reads {dependency} of later wave {dependency_wave}")
    direct = sorted(c for c, level in classification.of(obj.relation).items() if level is Level.DIRECT)
    if direct:
        problems.append(f"{obj.relation}: rebuilt in the cloud with direct identifiers {', '.join(direct)}")
    return problems
