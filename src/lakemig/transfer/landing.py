"""Idempotent upload of a delivery to the landing zone.

The landing zone is a Unity Catalog volume in both environments: managed on Free Edition, external
on ADLS Gen2 in Azure. The sender therefore needs one privilege, WRITE VOLUME on that volume, and no
storage account key; every write is in the Unity Catalog audit log. The Files API caps a file at
5 GiB, far above one table of one business date.

Order makes the upload safe to interrupt and to repeat: data files first, each checked by size
after upload, the manifest last and never overwritten. A delivery whose manifest is in place is
complete; a rerun finds it and does nothing. A rerun after an interruption uploads only the files
missing or truncated, then the manifest.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import AlreadyExists, NotFound

from lakemig.manifest import MANIFEST, Manifest

log = logging.getLogger(__name__)


class Landing(Protocol):
    def size(self, path: str) -> int | None:
        """Size in bytes of the file at `path`, or None if there is none."""
        ...

    def upload(self, path: str, local: Path, *, overwrite: bool) -> bool:
        """Uploads `local` to `path`; False if the file exists and `overwrite` is False."""
        ...

    def read_text(self, path: str) -> str: ...


class VolumeLanding:
    """A Unity Catalog volume through the Files API; `root` is a /Volumes/<catalog>/<schema>/<volume> path."""

    def __init__(self, client: WorkspaceClient) -> None:
        self._files = client.files

    def size(self, path: str) -> int | None:
        try:
            return self._files.get_metadata(path).content_length
        except NotFound:
            return None

    def upload(self, path: str, local: Path, *, overwrite: bool) -> bool:
        try:
            with local.open("rb") as contents:
                self._files.upload(path, contents, overwrite=overwrite)
        except AlreadyExists:
            return False
        return True

    def read_text(self, path: str) -> str:
        contents = self._files.download(path).contents
        if contents is None:
            raise NotFound(f"{path}: empty response")
        return contents.read().decode("utf-8")


class DirectoryLanding:
    """A directory standing in for the volume: tests, and a share mounted on the sending host."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def _local(self, path: str) -> Path:
        return self._root / path.lstrip("/")

    def size(self, path: str) -> int | None:
        target = self._local(path)
        return target.stat().st_size if target.is_file() else None

    def upload(self, path: str, local: Path, *, overwrite: bool) -> bool:
        target = self._local(path)
        if target.exists() and not overwrite:
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(local.read_bytes())
        return True

    def read_text(self, path: str) -> str:
        return self._local(path).read_text(encoding="utf-8")


class Outcome(StrEnum):
    DELIVERED = "delivered"
    # Some files were already in place from an interrupted upload.
    RESUMED = "resumed"
    ALREADY_DELIVERED = "already delivered"


class TransferError(RuntimeError):
    pass


def deliver(local: Path, landing: Landing, root: str) -> Outcome:
    manifest = Manifest.from_json((local / MANIFEST).read_text(encoding="utf-8"))
    remote = f"{root.rstrip('/')}/{manifest.business_date}/{manifest.delivery_id}"
    if landing.size(f"{remote}/{MANIFEST}") is not None:
        _check_same(landing, remote, manifest)
        return Outcome.ALREADY_DELIVERED

    in_place = 0
    for entry in manifest.files():
        path = f"{remote}/{entry.path}"
        if landing.size(path) == entry.bytes:
            in_place += 1
            continue
        landing.upload(path, local / entry.path, overwrite=True)
        uploaded = landing.size(path)
        if uploaded != entry.bytes:
            raise TransferError(f"{path}: {uploaded} bytes in the landing zone, {entry.bytes} sent")
    if not landing.upload(f"{remote}/{MANIFEST}", local / MANIFEST, overwrite=False):
        # Another sender completed the same delivery meanwhile.
        _check_same(landing, remote, manifest)
        return Outcome.ALREADY_DELIVERED
    log.info("%s: %d files, %d already in place", remote, len(manifest.files()), in_place)
    return Outcome.RESUMED if in_place else Outcome.DELIVERED


def _check_same(landing: Landing, remote: str, manifest: Manifest) -> None:
    landed = Manifest.from_json(landing.read_text(f"{remote}/{MANIFEST}"))
    if landed.content_digest != manifest.content_digest:
        raise TransferError(f"{remote}: landed digest {landed.content_digest} differs from {manifest.content_digest}")
