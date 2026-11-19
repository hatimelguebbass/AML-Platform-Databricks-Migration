from pathlib import Path

import pytest

from lakemig.cost.estimate import Assumptions, Range, estimate
from lakemig.cost.prices import UnitPrice

ASSUMPTIONS = """
currency: USD
regions: [westeurope]
window_days: 10
meters:
  - {key: jobs, service: Azure Databricks, product: P, sku: S, meter: Jobs DBU}
  - {key: storage, service: Storage, product: P, sku: Hot LRS, meter: Hot LRS Data Stored}
usage:
  - name: Daily pipeline
    meter: jobs
    once: {low: 1, expected: 2, high: 3}
    per_day: {low: 0.5, expected: 1, high: 2}
    basis: measured on the source
storage:
  meter: storage
  copies: {low: 1, expected: 2, high: 3}
  basis: history and landing
"""


def _price(key: str, price: float, unit: str) -> UnitPrice:
    return UnitPrice(key, "westeurope", "P", "S", key, unit, price, "USD", "2026-01-01")


def test_estimate_scales_usage_over_the_window(tmp_path: Path) -> None:
    path = tmp_path / "assumptions.yaml"
    path.write_text(ASSUMPTIONS, encoding="utf-8")

    (result,) = estimate(
        Assumptions.read(path), [_price("jobs", 0.5, "1 Hour"), _price("storage", 0.03, "1 GB/Month")], 3_000_000_000
    )

    pipeline, storage = result.lines
    assert pipeline.quantity == Range(6.0, 12.0, 23.0)
    assert pipeline.cost == Range(3.0, 6.0, 11.5)
    # 3 GB for a third of a month, times the copies.
    assert storage.quantity == pytest.approx(Range(1.0, 2.0, 3.0))
    assert result.total("expected") == pytest.approx(6.0 + 0.06)


def test_missing_price_names_the_fix(tmp_path: Path) -> None:
    path = tmp_path / "assumptions.yaml"
    path.write_text(ASSUMPTIONS, encoding="utf-8")

    with pytest.raises(LookupError, match="make prices"):
        estimate(Assumptions.read(path), [_price("jobs", 0.5, "1 Hour")], 0)


def test_range_must_be_ordered() -> None:
    with pytest.raises(ValueError, match="low <= expected <= high"):
        Range.from_dict({"low": 2, "expected": 1, "high": 3})
