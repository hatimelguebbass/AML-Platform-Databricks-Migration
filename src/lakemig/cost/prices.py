"""Unit prices from the Azure Retail Prices API, snapshotted to `migration/azure-prices.json`.

The estimate renders from the snapshot, so it is reproducible offline and in CI; `make prices`
refreshes it, and the diff shows any price change in review.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx

API = "https://prices.azure.com/api/retail/prices"
API_VERSION = "2023-01-01-preview"


@dataclass(frozen=True)
class MeterSpec:
    """Identifies one retail meter; the key is how the cost assumptions refer to it."""

    key: str
    service: str
    product: str
    sku: str
    meter: str


@dataclass(frozen=True)
class UnitPrice:
    key: str
    region: str
    product: str
    sku: str
    meter: str
    unit: str
    price: float
    currency: str
    effective_from: str


def fetch(specs: Iterable[MeterSpec], regions: Iterable[str], client: httpx.Client) -> list[UnitPrice]:
    prices = []
    for region in regions:
        for spec in specs:
            items = [i for i in _items(client, _filter(spec, region)) if _first_tier(i)]
            if len(items) != 1:
                raise LookupError(f"{spec.key} in {region}: expected one retail price, found {len(items)}")
            item = items[0]
            prices.append(
                UnitPrice(
                    key=spec.key,
                    region=region,
                    product=item["productName"],
                    sku=item["skuName"],
                    meter=item["meterName"],
                    unit=item["unitOfMeasure"],
                    price=float(item["retailPrice"]),
                    currency=item["currencyCode"],
                    effective_from=item["effectiveStartDate"][:10],
                )
            )
    return prices


@dataclass(frozen=True)
class Snapshot:
    retrieved_on: str
    prices: tuple[UnitPrice, ...]

    def write(self, path: Path) -> None:
        rows = sorted((asdict(p) for p in self.prices), key=lambda p: (p["region"], p["key"]))
        path.write_text(
            json.dumps({"retrieved_on": self.retrieved_on, "prices": rows}, indent=2) + "\n", encoding="utf-8"
        )

    @classmethod
    def read(cls, path: Path) -> Snapshot:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(retrieved_on=raw["retrieved_on"], prices=tuple(UnitPrice(**row) for row in raw["prices"]))


def _filter(spec: MeterSpec, region: str) -> str:
    clauses = {
        "serviceName": spec.service,
        "productName": spec.product,
        "skuName": spec.sku,
        "meterName": spec.meter,
        "armRegionName": region,
        "priceType": "Consumption",
    }
    return " and ".join(f"{field} eq '{_quote(value)}'" for field, value in clauses.items())


def _quote(value: str) -> str:
    return value.replace("'", "''")


def _first_tier(item: Mapping[str, Any]) -> bool:
    # Storage is priced in volume tiers; the first one applies far beyond this platform's size.
    return float(item.get("tierMinimumUnits", 0)) == 0


def _items(client: httpx.Client, odata_filter: str) -> Iterator[dict[str, Any]]:
    response = client.get(API, params={"api-version": API_VERSION, "$filter": odata_filter})
    while True:
        response.raise_for_status()
        page = response.json()
        yield from page["Items"]
        next_page = page.get("NextPageLink")
        if not next_page:
            return
        response = client.get(next_page)
