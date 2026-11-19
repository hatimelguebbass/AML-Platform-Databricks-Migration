"""Cost of the Azure window, estimated before any resource exists (`migration/cost-assumptions.yaml`).

Every quantity is a low / expected / high range with its basis written next to it; step 8 replaces
the assumptions with measured usage and compares.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from lakemig.cost.prices import MeterSpec, UnitPrice

SCENARIOS = ("low", "expected", "high")
GB = 1_000_000_000
DAYS_PER_MONTH = 30


@dataclass(frozen=True)
class Range:
    low: float
    expected: float
    high: float

    def get(self, scenario: str) -> float:
        return float(getattr(self, scenario))

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> Range:
        if raw is None:
            return cls(0.0, 0.0, 0.0)
        bounds = cls(float(raw["low"]), float(raw["expected"]), float(raw["high"]))
        if not bounds.low <= bounds.expected <= bounds.high:
            raise ValueError(f"range must satisfy low <= expected <= high: {raw}")
        return bounds


@dataclass(frozen=True)
class Usage:
    name: str
    meter: str
    once: Range
    per_day: Range
    basis: str


@dataclass(frozen=True)
class Assumptions:
    currency: str
    regions: tuple[str, ...]
    window_days: int
    meters: tuple[MeterSpec, ...]
    usage: tuple[Usage, ...]
    storage_meter: str
    # Stored bytes per byte migrated: table history kept by the format, and the landing copies.
    storage_copies: Range
    storage_basis: str

    @classmethod
    def read(cls, path: Path) -> Assumptions:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        storage = raw["storage"]
        return cls(
            currency=raw["currency"],
            regions=tuple(raw["regions"]),
            window_days=int(raw["window_days"]),
            meters=tuple(MeterSpec(**m) for m in raw["meters"]),
            usage=tuple(
                Usage(
                    name=u["name"],
                    meter=u["meter"],
                    once=Range.from_dict(u.get("once")),
                    per_day=Range.from_dict(u.get("per_day")),
                    basis=u["basis"],
                )
                for u in raw["usage"]
            ),
            storage_meter=storage["meter"],
            storage_copies=Range.from_dict(storage["copies"]),
            storage_basis=storage["basis"],
        )


@dataclass(frozen=True)
class Line:
    name: str
    meter: str
    unit: str
    unit_price: float
    quantity: Range
    cost: Range


@dataclass(frozen=True)
class Estimate:
    region: str
    currency: str
    window_days: int
    lines: tuple[Line, ...]

    def total(self, scenario: str) -> float:
        return sum(line.cost.get(scenario) for line in self.lines)


def estimate(assumptions: Assumptions, prices: Iterable[UnitPrice], migrated_bytes: int) -> list[Estimate]:
    by_key = {(p.region, p.key): p for p in prices}
    estimates = []
    for region in assumptions.regions:

        def price(key: str, region: str = region) -> UnitPrice:
            unit_price = by_key.get((region, key))
            if unit_price is None:
                raise LookupError(f"no price for {key} in {region}: refresh the snapshot with `make prices`")
            if unit_price.currency != assumptions.currency:
                raise ValueError(f"{key} in {region} is priced in {unit_price.currency}, not {assumptions.currency}")
            return unit_price

        lines = []
        for usage in assumptions.usage:
            unit_price = price(usage.meter)
            quantity = _scale(usage.once, usage.per_day, assumptions.window_days)
            lines.append(_line(usage.name, unit_price, quantity))
        storage = price(assumptions.storage_meter)
        gb_months = migrated_bytes / GB * assumptions.window_days / DAYS_PER_MONTH
        quantity = Range(*(gb_months * assumptions.storage_copies.get(s) for s in SCENARIOS))
        lines.append(_line("Lakehouse storage", storage, quantity))
        estimates.append(Estimate(region, assumptions.currency, assumptions.window_days, tuple(lines)))
    return estimates


def _scale(once: Range, per_day: Range, days: int) -> Range:
    return Range(*(once.get(s) + per_day.get(s) * days for s in SCENARIOS))


def _line(name: str, price: UnitPrice, quantity: Range) -> Line:
    return Line(
        name=name,
        meter=f"{price.product} / {price.meter}",
        unit=price.unit,
        unit_price=price.price,
        quantity=quantity,
        cost=Range(*(quantity.get(s) * price.price for s in SCENARIOS)),
    )
