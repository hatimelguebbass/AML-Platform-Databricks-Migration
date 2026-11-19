"""Proof that the Azure window left nothing behind (ADR 0007, step 10).

Before `terraform destroy`, `lakemig teardown inventory` keeps the identifier of every object the
Terraform state of a root manages. Afterwards, `lakemig teardown verify` looks each one up where it
lived, sweeps the subscription and the Databricks account for anything else, and records the
window's actual cost per day from Cost Management. The identifiers stay on the operator's host
(`.teardown/`); `migration/teardown.json`, which is committed, holds counts per type.

Objects of the workspace (warehouse, grants, entitlements, catalog and its bindings) cannot be
looked up once the workspace is gone: they are reported as removed with it, and only if it is.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Any

import httpx

FORMAT_VERSION = 1

ARM = "https://management.azure.com"
GRAPH = "https://graph.microsoft.com/v1.0"
ACCOUNTS = "https://accounts.azuredatabricks.net/api/2.0/accounts"
READ_ATTEMPTS = 3
# Entra ID application of Azure Databricks: the audience of tokens for the account API.
DATABRICKS_RESOURCE = "2ff814a6-3304-4ab8-85cb-cd0e6f879c1d"


class Place(StrEnum):
    AZURE = "Azure Resource Manager"
    ENTRA = "Microsoft Entra ID"
    ACCOUNT = "Databricks account"
    WORKSPACE = "prod workspace"


ACCOUNT_TYPES = {
    "databricks_group",
    "databricks_service_principal",
    "databricks_user",
    "databricks_group_member",
    "databricks_mws_permission_assignment",
}

# The API version to read each ARM type with; a type missing here cannot be verified and fails.
ARM_API_VERSIONS = {
    "resourcegroups": "2021-04-01",
    "microsoft.storage/storageaccounts": "2023-05-01",
    "microsoft.storage/storageaccounts/blobservices/containers": "2023-05-01",
    "microsoft.databricks/workspaces": "2024-05-01",
    "microsoft.databricks/accessconnectors": "2024-05-01",
    "microsoft.authorization/roleassignments": "2022-04-01",
    "microsoft.consumption/budgets": "2023-11-01",
}

_RESOURCE_GROUP = re.compile(r"/subscriptions/[^/]+/resourceGroups/[^/]+", re.IGNORECASE)


class UnverifiableError(Exception):
    """An object of the inventory has no lookup: the teardown cannot be proved."""


@dataclass(frozen=True)
class Managed:
    root: str
    address: str
    type: str
    place: Place
    locator: str

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> Managed:
        return cls(raw["root"], raw["address"], raw["type"], Place(raw["place"]), raw["locator"])


def place_of(resource_type: str) -> Place:
    if resource_type.startswith(("azurerm_", "azapi_")):
        return Place.AZURE
    if resource_type.startswith("azuread_"):
        return Place.ENTRA
    if resource_type in ACCOUNT_TYPES:
        return Place.ACCOUNT
    if resource_type.startswith("databricks_"):
        return Place.WORKSPACE
    raise UnverifiableError(f"no lookup for {resource_type}")


def inventory(root: str, state: Mapping[str, Any]) -> list[Managed]:
    """Managed resources of a `terraform show -json` document (data sources are read, not owned)."""
    module = state.get("values", {}).get("root_module", {})
    return [
        Managed(root, r["address"], r["type"], place_of(r["type"]), str(r["values"]["id"]))
        for r in module.get("resources", [])
        if r["mode"] == "managed"
    ]


def read_inventory(path: Path) -> list[Managed]:
    return [Managed.from_json(m) for m in json.loads(path.read_text(encoding="utf-8"))] if path.exists() else []


def write_inventory(path: Path, items: Iterable[Managed]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(m) for m in items], indent=2) + "\n", encoding="utf-8")


def merge(kept: Sequence[Managed], root: str, fresh: Sequence[Managed]) -> list[Managed]:
    """The inventory with `root`'s latest state added, one copy per address.

    A union, not a replacement: planned again after a partial destroy, the state no longer holds
    what is already gone, and those objects still have to be looked up.
    """
    seen = {m.address for m in fresh}
    return [m for m in kept if m.root != root or m.address not in seen] + list(fresh)


def arm_type(resource_id: str) -> str:
    if "/providers/" not in resource_id:
        if _RESOURCE_GROUP.fullmatch(resource_id):
            return "resourcegroups"
        raise UnverifiableError(f"not an ARM resource id: {resource_id}")
    # Extension resources (a role assignment on a storage account) are typed by their last provider.
    parts = resource_id.rsplit("/providers/", 1)[1].split("/")
    return "/".join([parts[0], *parts[1::2]]).lower()


# --- Lookups. A lookup answers whether the object still exists; 404 is the only "no".


@dataclass(frozen=True)
class Tokens:
    arm: str
    graph: str
    databricks: str


class Lookups:
    def __init__(self, http: httpx.Client, tokens: Tokens, account_id: str) -> None:
        self._http = http
        self._tokens = tokens
        self._account = f"{ACCOUNTS}/{account_id}"

    def _read(self, url: str, token: str, params: Mapping[str, str] | None) -> Any:
        # Reads only: a connection dropped by a proxy is retried, an HTTP error is not.
        for attempt in range(1, READ_ATTEMPTS + 1):
            try:
                return self._http.get(url, params=params or {}, headers={"Authorization": f"Bearer {token}"})
            except httpx.TransportError:
                if attempt == READ_ATTEMPTS:
                    raise
        raise AssertionError("unreachable")

    def _exists(self, url: str, token: str, params: Mapping[str, str] | None = None) -> bool:
        response = self._read(url, token, params)
        if response.status_code == 404:
            return False
        response.raise_for_status()
        return True

    def _get(self, url: str, token: str, params: Mapping[str, str] | None = None) -> Any:
        response = self._read(url, token, params)
        response.raise_for_status()
        return response.json()

    def azure(self, resource_id: str) -> bool:
        version = ARM_API_VERSIONS.get(arm_type(resource_id))
        if version is None:
            raise UnverifiableError(f"no API version for {arm_type(resource_id)}")
        return self._exists(f"{ARM}{resource_id}", self._tokens.arm, {"api-version": version})

    def entra(self, resource_type: str, locator: str) -> bool:
        if resource_type == "azuread_application_federated_identity_credential":
            application, _, credential = locator.partition("/federatedIdentityCredential/")
            path = f"/applications/{application}/federatedIdentityCredentials/{credential}"
        elif resource_type in ("azuread_application", "azuread_service_principal"):
            path = locator
        else:
            raise UnverifiableError(f"no lookup for {resource_type}")
        return self._exists(f"{GRAPH}{path}", self._tokens.graph)

    def account(self, resource_type: str, locator: str) -> bool:
        scim = f"{self._account}/scim/v2"
        token = self._tokens.databricks
        if resource_type == "databricks_group":
            return self._exists(f"{scim}/Groups/{locator}", token)
        if resource_type == "databricks_service_principal":
            return self._exists(f"{scim}/ServicePrincipals/{locator}", token)
        if resource_type == "databricks_user":
            return self._exists(f"{scim}/Users/{locator}", token)
        if resource_type == "databricks_group_member":
            # group|member: a membership ends with its group.
            group, _, member = locator.partition("|")
            if not self._exists(f"{scim}/Groups/{group}", token):
                return False
            members = self._get(f"{scim}/Groups/{group}", token).get("members", [])
            return any(m.get("value") == member for m in members)
        if resource_type == "databricks_mws_permission_assignment":
            # workspace|principal: an assignment ends with its principal, here always a group.
            return self._exists(f"{scim}/Groups/{locator.partition('|')[2]}", token)
        raise UnverifiableError(f"no lookup for {resource_type}")

    def resource_groups(self, subscription: str) -> list[str]:
        url = f"{ARM}/subscriptions/{subscription}/resourcegroups"
        return sorted(g["name"] for g in self._get(url, self._tokens.arm, {"api-version": "2021-04-01"})["value"])

    def resources(self, subscription: str) -> list[tuple[str, str]]:
        url = f"{ARM}/subscriptions/{subscription}/resources"
        listed = self._get(url, self._tokens.arm, {"api-version": "2021-04-01"})["value"]
        return sorted((r["type"], r["name"]) for r in listed)

    def metastores(self) -> list[Metastore]:
        listed = self._get(f"{self._account}/metastores", self._tokens.databricks).get("metastores", [])
        out = []
        for m in listed:
            assigned = self._get(f"{self._account}/metastores/{m['metastore_id']}/workspaces", self._tokens.databricks)
            out.append(Metastore(m["name"], m.get("region", ""), len(assigned.get("workspace_ids", []))))
        return sorted(out, key=lambda m: m.name)

    def daily_cost(self, subscription: str, first: date, last: date) -> tuple[str, tuple[DailyCost, ...]]:
        """Actual cost of the subscription per day (Cost Management), before the free account's credit."""
        response = self._http.post(
            f"{ARM}/subscriptions/{subscription}/providers/Microsoft.CostManagement/query",
            params={"api-version": "2023-11-01"},
            headers={"Authorization": f"Bearer {self._tokens.arm}"},
            json={
                "type": "ActualCost",
                "timeframe": "Custom",
                "timePeriod": {"from": f"{first.isoformat()}T00:00:00Z", "to": f"{last.isoformat()}T23:59:59Z"},
                "dataset": {"granularity": "Daily", "aggregation": {"cost": {"name": "Cost", "function": "Sum"}}},
            },
        )
        response.raise_for_status()
        properties = response.json()["properties"]
        names = [c["name"] for c in properties["columns"]]
        rows = [dict(zip(names, row, strict=True)) for row in properties["rows"]]
        currency = str(rows[0]["Currency"]) if rows else "USD"
        days = tuple(
            DailyCost(_usage_date(r["UsageDate"]), round(float(r["Cost"]), 6))
            for r in sorted(rows, key=lambda r: int(r["UsageDate"]))
        )
        return currency, days


def _usage_date(value: Any) -> str:
    text = str(value)
    return f"{text[:4]}-{text[4:6]}-{text[6:8]}"


# --- Report.


@dataclass(frozen=True)
class TypeCount:
    root: str
    type: str
    place: str
    destroyed: int
    remaining: int


@dataclass(frozen=True)
class Metastore:
    name: str
    region: str
    workspaces: int


@dataclass(frozen=True)
class DailyCost:
    day: str
    cost: float


@dataclass(frozen=True)
class Report:
    format_version: int
    measured_at: str
    window: tuple[str, str]
    managed: tuple[TypeCount, ...]
    resource_groups: tuple[str, ...]
    resources: tuple[tuple[str, str], ...]
    metastores: tuple[Metastore, ...]
    currency: str
    daily_cost: tuple[DailyCost, ...]

    @property
    def remaining(self) -> int:
        return sum(t.remaining for t in self.managed)

    @property
    def complete(self) -> bool:
        """Nothing the configuration managed is left, the subscription is empty, no metastore serves a workspace."""
        return (
            not self.remaining
            and not self.resource_groups
            and not self.resources
            and not any(m.workspaces for m in self.metastores)
        )

    @property
    def total_cost(self) -> float:
        return sum(d.cost for d in self.daily_cost)

    def write(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")

    @classmethod
    def read(cls, path: Path) -> Report:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            format_version=raw["format_version"],
            measured_at=raw["measured_at"],
            window=(raw["window"][0], raw["window"][1]),
            managed=tuple(TypeCount(**t) for t in raw["managed"]),
            resource_groups=tuple(raw["resource_groups"]),
            resources=tuple((r[0], r[1]) for r in raw["resources"]),
            metastores=tuple(Metastore(**m) for m in raw["metastores"]),
            currency=raw["currency"],
            daily_cost=tuple(DailyCost(**d) for d in raw["daily_cost"]),
        )


def verify(items: Sequence[Managed], lookups: Lookups) -> tuple[TypeCount, ...]:
    """Counts per root and type of the objects destroyed and of those still found."""
    workspaces = [m for m in items if arm_type_or_none(m) == "microsoft.databricks/workspaces"]
    workspace_left = any(lookups.azure(w.locator) for w in workspaces)
    if any(m.place is Place.WORKSPACE for m in items) and not workspaces:
        raise UnverifiableError("workspace objects in the inventory, but no workspace to tie them to")
    counts: dict[tuple[str, str, str], list[int]] = {}
    for m in items:
        if m.place is Place.AZURE:
            left = lookups.azure(m.locator)
        elif m.place is Place.ENTRA:
            left = lookups.entra(m.type, m.locator)
        elif m.place is Place.ACCOUNT:
            left = lookups.account(m.type, m.locator)
        else:
            left = workspace_left
        tally = counts.setdefault((m.root, m.type, m.place.value), [0, 0])
        tally[1 if left else 0] += 1
    return tuple(TypeCount(root, t, place, d, r) for (root, t, place), (d, r) in sorted(counts.items()))


def arm_type_or_none(m: Managed) -> str | None:
    return arm_type(m.locator) if m.place is Place.AZURE else None


def teardown_md(report: Report) -> str:
    total = sum(t.destroyed + t.remaining for t in report.managed)
    verdict = (
        "**Nothing is left.**"
        if report.complete
        else f"**Not complete: {report.remaining} managed objects, {len(report.resources)} resources and "
        f"{len(report.resource_groups)} resource groups remain.**"
    )
    out = [
        "# Teardown of the Azure window",
        "",
        "Generated by `make teardown` into `migration/teardown.json` (ADR 0007); do not edit.",
        "",
        f"Measured after `terraform destroy` of every root. {verdict}",
        "",
        "## Objects the Terraform states managed",
        "",
        f"Each of the {total} objects was looked up where it lived, by the identifier kept before the destroy. "
        "Objects of the prod workspace are removed with it.",
        "",
        "| Root | Type | Lives in | Destroyed | Remaining |",
        "|---|---|---|---:|---:|",
        *[f"| {t.root} | `{t.type}` | {t.place} | {t.destroyed} | {t.remaining} |" for t in report.managed],
        "",
        "## Sweep",
        "",
        f"Resource groups in the subscription: {len(report.resource_groups)}"
        + (" (" + ", ".join(f"`{g}`" for g in report.resource_groups) + ")" if report.resource_groups else "")
        + f". Resources: {len(report.resources)}"
        + (" (" + ", ".join(f"`{n}` ({t})" for t, n in report.resources) + ")" if report.resources else "")
        + ".",
        "",
        "Unity Catalog metastores of the Databricks account:",
        "",
        "| Metastore | Region | Workspaces assigned |",
        "|---|---|---:|",
        *[f"| `{m.name}` | {m.region} | {m.workspaces} |" for m in report.metastores],
        "",
        "## Cost of the window",
        "",
        f"Actual cost charged to the subscription per day by Azure Cost Management, {len(report.daily_cost)} days, "
        f"before the free account's credit: **{report.total_cost:.2f} {report.currency}**. "
        "The last day may still grow while Azure posts late usage.",
        "",
        f"| Day | Cost ({report.currency}) |",
        "|---|---:|",
        *[f"| {n} | {d.cost:.2f} |" for n, d in enumerate(report.daily_cost, 1)],
    ]
    return "\n".join(out) + "\n"
