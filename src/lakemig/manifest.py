"""Format of a delivery's manifest, shared by the on-premises export and the cloud ingestion.

Standard library only: the ingestion runs it on Databricks, where the wheel brings no dependencies.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

FORMAT_VERSION = 1
MANIFEST = "_manifest.json"
DATA_FILE = "part-00000.parquet"


@dataclass(frozen=True)
class ColumnEntry:
    name: str
    type: str
    nullable: bool
    source: str
    treatment: str
    namespace: str | None


@dataclass(frozen=True)
class FileEntry:
    path: str
    bytes: int
    sha256: str
    rows: int


@dataclass(frozen=True)
class TableEntry:
    name: str
    source_relation: str
    selection: str
    rows: int
    columns: tuple[ColumnEntry, ...]
    control_totals: dict[str, dict[str, Any]]
    files: tuple[FileEntry, ...]


@dataclass(frozen=True)
class Manifest:
    format_version: int
    delivery_id: str
    business_date: str
    content_digest: str
    exported_at: str
    source: dict[str, str]
    policy_digest: str
    pseudonymization: dict[str, str]
    gate: dict[str, Any]
    tables: tuple[TableEntry, ...]

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=False) + "\n"

    @classmethod
    def from_json(cls, text: str) -> Manifest:
        raw = json.loads(text)
        if raw.get("format_version") != FORMAT_VERSION:
            raise ValueError(f"manifest format {raw.get('format_version')} is not {FORMAT_VERSION}")
        tables = tuple(
            TableEntry(
                **{
                    **t,
                    "columns": tuple(ColumnEntry(**c) for c in t["columns"]),
                    "files": tuple(FileEntry(**f) for f in t["files"]),
                }
            )
            for t in raw["tables"]
        )
        return cls(**{**raw, "tables": tables})

    def files(self) -> tuple[FileEntry, ...]:
        return tuple(f for t in self.tables for f in t.files)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()
