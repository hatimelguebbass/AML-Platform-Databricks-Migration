from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx
import pytest

from lakemig.operations import teardown
from lakemig.operations.teardown import Managed, Place

SUB = "/subscriptions/0000"
RG = f"{SUB}/resourceGroups/rg-prod"
ACCOUNT = "acc"
WORKSPACE = f"{RG}/providers/Microsoft.Databricks/workspaces/dbw-prod"
STORAGE = f"{RG}/providers/Microsoft.Storage/storageAccounts/stprod"
SCIM = f"{teardown.ACCOUNTS}/{ACCOUNT}/scim/v2"


def _resource(address: str, type_: str, id_: str, mode: str = "managed") -> dict[str, object]:
    return {"address": address, "type": type_, "mode": mode, "values": {"id": id_}}


STATE = {
    "values": {
        "root_module": {
            "resources": [
                _resource("azapi_resource.workspace", "azapi_resource", WORKSPACE),
                _resource("azurerm_resource_group.platform", "azurerm_resource_group", RG),
                _resource(
                    'azurerm_storage_container.lake["catalog"]',
                    "azurerm_storage_container",
                    f"{STORAGE}/blobServices/default/containers/catalog",
                ),
                _resource("azuread_application.deployer", "azuread_application", "/applications/app1"),
                _resource(
                    "azuread_application_federated_identity_credential.github",
                    "azuread_application_federated_identity_credential",
                    "app1/federatedIdentityCredential/cred1",
                ),
                _resource("databricks_group.admins", "databricks_group", "g1"),
                _resource("databricks_group_member.deployer", "databricks_group_member", "g1|sp1"),
                _resource("databricks_sql_endpoint.aml", "databricks_sql_endpoint", "wh1"),
                _resource("data.azurerm_client_config.current", "azurerm_client_config", "x", mode="data"),
            ]
        }
    }
}


def test_inventory_keeps_managed_resources_by_place() -> None:
    items = teardown.inventory("prod", STATE)
    assert [m.address for m in items if m.type == "azurerm_client_config"] == []
    places = {m.type: m.place for m in items}
    assert places["azapi_resource"] is Place.AZURE
    assert places["azuread_application_federated_identity_credential"] is Place.ENTRA
    assert places["databricks_group_member"] is Place.ACCOUNT
    assert places["databricks_sql_endpoint"] is Place.WORKSPACE


def test_a_type_without_lookup_is_refused() -> None:
    with pytest.raises(teardown.UnverifiableError):
        teardown.place_of("google_storage_bucket")


@pytest.mark.parametrize(
    ("resource_id", "expected"),
    [
        (RG, "resourcegroups"),
        (WORKSPACE, "microsoft.databricks/workspaces"),
        (
            f"{STORAGE}/blobServices/default/containers/catalog",
            "microsoft.storage/storageaccounts/blobservices/containers",
        ),
        (f"{STORAGE}/providers/Microsoft.Authorization/roleAssignments/r1", "microsoft.authorization/roleassignments"),
        (f"{SUB}/providers/Microsoft.Consumption/budgets/b1", "microsoft.consumption/budgets"),
    ],
)
def test_arm_type(resource_id: str, expected: str) -> None:
    assert teardown.arm_type(resource_id) == expected


def test_merge_keeps_what_a_partial_destroy_removed_from_the_state() -> None:
    gone = Managed("prod", "a", "azurerm_storage_account", Place.AZURE, STORAGE)
    stale = Managed("prod", "c", "azurerm_resource_group", Place.AZURE, f"{SUB}/resourceGroups/old")
    bootstrap = [Managed("bootstrap", "b", "azurerm_resource_group", Place.AZURE, f"{SUB}/resourceGroups/rg-state")]
    fresh = [Managed("prod", "c", "azurerm_resource_group", Place.AZURE, RG)]
    merged = teardown.merge([gone, stale, *bootstrap], "prod", fresh)
    assert [(m.address, m.locator) for m in merged] == [("a", STORAGE), ("b", bootstrap[0].locator), ("c", RG)]


def test_inventory_round_trip(tmp_path: Path) -> None:
    items = teardown.inventory("prod", STATE)
    path = tmp_path / "inventory.json"
    teardown.write_inventory(path, items)
    assert teardown.read_inventory(path) == items
    assert teardown.read_inventory(tmp_path / "absent.json") == []


def _lookups(found: dict[str, object], *, flaky: int = 0) -> teardown.Lookups:
    """Lookups against a fake cloud where `found` maps each existing URL path to its JSON body."""
    failures = [flaky]

    def handler(request: httpx.Request) -> httpx.Response:
        if failures[0]:
            failures[0] -= 1
            raise httpx.RemoteProtocolError("server disconnected", request=request)
        url = str(request.url.copy_with(query=None))
        if url in found:
            return httpx.Response(200, json=found[url])
        return httpx.Response(404, json={})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return teardown.Lookups(client, teardown.Tokens("arm", "graph", "dbx"), ACCOUNT)


def test_everything_gone_is_destroyed_including_the_workspace_objects() -> None:
    counts = teardown.verify(teardown.inventory("prod", STATE), _lookups({}))
    assert all(c.remaining == 0 for c in counts)
    assert sum(c.destroyed for c in counts) == 8


def test_workspace_objects_remain_while_the_workspace_does() -> None:
    lookups = _lookups({f"{teardown.ARM}{WORKSPACE}": {}})
    counts = {c.type: c for c in teardown.verify(teardown.inventory("prod", STATE), lookups)}
    assert counts["azapi_resource"].remaining == 1
    assert counts["databricks_sql_endpoint"].remaining == 1
    assert counts["azurerm_resource_group"].remaining == 0


def test_a_membership_ends_with_its_group_or_its_member() -> None:
    member_gone = _lookups({f"{SCIM}/Groups/g1": {"members": [{"value": "someone-else"}]}})
    counts = {c.type: c for c in teardown.verify(teardown.inventory("prod", STATE), member_gone)}
    assert counts["databricks_group"].remaining == 1
    assert counts["databricks_group_member"].remaining == 0
    member_left = _lookups({f"{SCIM}/Groups/g1": {"members": [{"value": "sp1"}]}})
    counts = {c.type: c for c in teardown.verify(teardown.inventory("prod", STATE), member_left)}
    assert counts["databricks_group_member"].remaining == 1


def test_federated_credential_is_looked_up_under_its_application() -> None:
    path = f"{teardown.GRAPH}/applications/app1/federatedIdentityCredentials/cred1"
    counts = {c.type: c for c in teardown.verify(teardown.inventory("prod", STATE), _lookups({path: {}}))}
    assert counts["azuread_application_federated_identity_credential"].remaining == 1
    assert counts["azuread_application"].remaining == 0


def test_a_dropped_connection_is_retried_an_error_is_not() -> None:
    assert not _lookups({}, flaky=teardown.READ_ATTEMPTS - 1).azure(RG)
    with pytest.raises(httpx.RemoteProtocolError):
        _lookups({}, flaky=teardown.READ_ATTEMPTS).azure(RG)

    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={})

    denied = teardown.Lookups(httpx.Client(transport=httpx.MockTransport(refuse)), teardown.Tokens("", "", ""), ACCOUNT)
    with pytest.raises(httpx.HTTPStatusError):
        denied.azure(RG)


def test_daily_cost_and_report() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        return httpx.Response(
            200,
            json={
                "properties": {
                    "columns": [{"name": "Cost"}, {"name": "UsageDate"}, {"name": "Currency"}],
                    "rows": [[3.5, 20261117, "USD"], [0.25, 20261116, "USD"]],
                }
            },
        )

    lookups = teardown.Lookups(httpx.Client(transport=httpx.MockTransport(handler)), teardown.Tokens("a", "", ""), "x")
    currency, days = lookups.daily_cost("0000", date(2026, 11, 16), date(2026, 11, 17))
    assert currency == "USD"
    assert [d.day for d in days] == ["2026-11-16", "2026-11-17"]

    report = teardown.Report(
        format_version=teardown.FORMAT_VERSION,
        measured_at="2026-11-18T00:00:00+00:00",
        window=("2026-11-16", "2026-11-17"),
        managed=(teardown.TypeCount("prod", "azurerm_resource_group", Place.AZURE.value, 1, 0),),
        resource_groups=(),
        resources=(),
        metastores=(teardown.Metastore("metastore_azure_francecentral", "francecentral", 0),),
        currency=currency,
        daily_cost=days,
    )
    assert report.complete
    assert report.total_cost == pytest.approx(3.75)
    rendered = teardown.teardown_md(report)
    assert "**Nothing is left.**" in rendered
    assert "**3.75 USD**" in rendered


def test_report_is_incomplete_while_a_metastore_serves_a_workspace(tmp_path: Path) -> None:
    report = teardown.Report(
        format_version=teardown.FORMAT_VERSION,
        measured_at="t",
        window=("2026-11-16", "2026-11-17"),
        managed=(),
        resource_groups=(),
        resources=(),
        metastores=(teardown.Metastore("m", "francecentral", 1),),
        currency="USD",
        daily_cost=(),
    )
    assert not report.complete
    path = tmp_path / "teardown.json"
    report.write(path)
    assert teardown.Report.read(path) == report
