"""The transfer against the dev workspace's Files API, in a scratch volume emptied after each test."""

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import DAY, Bank
from databricks.sdk import WorkspaceClient

from lakemig.export import delivery
from lakemig.export.run import export_date
from lakemig.transfer.landing import Outcome, VolumeLanding, deliver

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def client() -> WorkspaceClient:
    return WorkspaceClient()


@pytest.fixture
def root(client: WorkspaceClient) -> Iterator[str]:
    catalog = os.environ.get("LAKEMIG_CATALOG", "aml_dev")
    path = f"/Volumes/{catalog}/migration/transfer_checks/{uuid.uuid4().hex}"
    yield path
    _remove(client, path)


def _remove(client: WorkspaceClient, directory: str) -> None:
    for entry in client.files.list_directory_contents(directory):
        if entry.path is None:
            continue
        if entry.is_directory:
            _remove(client, entry.path)
        else:
            client.files.delete(entry.path)
    client.files.delete_directory(directory)


def test_delivery_lands_once_then_resumes_after_an_interruption(
    bank: Bank, tmp_path: Path, client: WorkspaceClient, root: str
) -> None:
    manifest = export_date(DAY, bank.plan, bank.inventory, bank.source, bank.key, tmp_path, bank.context)
    local = tmp_path / manifest.business_date / manifest.delivery_id
    landing = VolumeLanding(client)
    remote = f"{root}/{manifest.business_date}/{manifest.delivery_id}"

    assert deliver(local, landing, root) is Outcome.DELIVERED
    assert deliver(local, landing, root) is Outcome.ALREADY_DELIVERED
    assert delivery.Manifest.from_json(landing.read_text(f"{remote}/{delivery.MANIFEST}")) == manifest

    client.files.delete(f"{remote}/{delivery.MANIFEST}")
    client.files.delete(f"{remote}/{manifest.files()[0].path}")
    assert deliver(local, landing, root) is Outcome.RESUMED
    for entry in manifest.files():
        assert landing.size(f"{remote}/{entry.path}") == entry.bytes
