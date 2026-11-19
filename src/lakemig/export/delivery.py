"""A delivery on disk: one Parquet file per table and the manifest that describes and seals them.

Layout, identical on premises and in the landing volume:

    <business_date>/<delivery_id>/<table>/part-00000.parquet
    <business_date>/<delivery_id>/_manifest.json

The delivery id is the business date and a digest of the content, so exporting the same data again
gives the same id (and is skipped), and a corrected export of a date gets a new id instead of
overwriting files a consumer may be reading. The manifest is written last, here and on upload: a
delivery without one is incomplete and consumers ignore it.

The manifest's control totals (rows, nulls, sums, ranges per column) are what the cloud recomputes
after loading, as a bank reconciles a file against its trailer: they hold across Parquet readers,
where a byte checksum only proves the transfer.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from lakemig.export.policy import TablePlan
from lakemig.manifest import (
    DATA_FILE,
    FORMAT_VERSION,
    MANIFEST,
    ColumnEntry,
    FileEntry,
    Manifest,
    TableEntry,
    sha256_of,
)

__all__ = [
    "MANIFEST",
    "DeliveryConflictError",
    "Header",
    "Manifest",
    "complete",
    "content_digest",
    "sha256_of",
    "write",
]


@dataclass(frozen=True)
class Header:
    """What the manifest says about the export run rather than about the data."""

    source: dict[str, str]
    policy_digest: str
    pseudonymization: dict[str, str]
    gate: dict[str, Any]


class DeliveryConflictError(RuntimeError):
    pass


def write(out: Path, business_date: date, tables: Sequence[tuple[TablePlan, pa.Table]], header: Header) -> Manifest:
    """Writes the delivery under `out`, or keeps the identical one already there."""
    day = out / business_date.isoformat()
    day.mkdir(parents=True, exist_ok=True)
    partial = day / f".partial-{uuid.uuid4().hex}"
    try:
        entries = tuple(_write_table(partial, plan, table) for plan, table in tables)
        digest = content_digest(business_date, header.pseudonymization["key_id"], entries)
        manifest = Manifest(
            format_version=FORMAT_VERSION,
            delivery_id=f"{business_date.isoformat()}-{digest[:12]}",
            business_date=business_date.isoformat(),
            content_digest=digest,
            exported_at=datetime.now(UTC).isoformat(timespec="seconds"),
            source=header.source,
            policy_digest=header.policy_digest,
            pseudonymization=header.pseudonymization,
            gate=header.gate,
            tables=entries,
        )
        final = day / manifest.delivery_id
        if (final / MANIFEST).exists():
            existing = Manifest.from_json((final / MANIFEST).read_text(encoding="utf-8"))
            if existing.content_digest != digest:
                raise DeliveryConflictError(f"{final} holds digest {existing.content_digest}, not {digest}")
            return existing
        if final.exists():
            # A delivery directory without its manifest: an interrupted rename cannot leave one, a person can.
            raise DeliveryConflictError(f"{final} exists without {MANIFEST}: remove it and export again")
        (partial / MANIFEST).write_text(manifest.to_json(), encoding="utf-8")
        os.replace(partial, final)
        return manifest
    finally:
        shutil.rmtree(partial, ignore_errors=True)


def content_digest(business_date: date, key_id: str, tables: Sequence[TableEntry]) -> str:
    """Digest of what the data is, not of when or from which commit it was exported."""
    identity = {
        "business_date": business_date.isoformat(),
        "key_id": key_id,
        "tables": [
            {"name": t.name, "columns": [asdict(c) for c in t.columns], "files": [f.sha256 for f in t.files]}
            for t in tables
        ],
    }
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def _write_table(root: Path, plan: TablePlan, table: pa.Table) -> TableEntry:
    relative = f"{plan.name}/{DATA_FILE}"
    path = root / relative
    path.parent.mkdir(parents=True)
    # One file and fixed writer settings: the same rows in the same order give the same bytes.
    pq.write_table(table, path, compression="zstd", write_statistics=True)
    by_output = {c.output: c for c in plan.crossing()}
    columns = tuple(
        ColumnEntry(
            name=f.name,
            type=str(f.type),
            nullable=f.nullable,
            source=by_output[f.name].source,
            treatment=by_output[f.name].treatment.value,
            namespace=by_output[f.name].namespace,
        )
        for f in table.schema
    )
    return TableEntry(
        name=plan.name,
        source_relation=plan.relation,
        selection=plan.selection.kind.value,
        rows=table.num_rows,
        columns=columns,
        control_totals={c: control_totals(table.column(c)) for c in table.column_names},
        files=(FileEntry(relative, path.stat().st_size, sha256_of(path), table.num_rows),),
    )


def control_totals(values: pa.ChunkedArray) -> dict[str, Any]:
    totals: dict[str, Any] = {"nulls": values.null_count}
    kind = values.type
    if pa.types.is_integer(kind) or pa.types.is_decimal(kind):
        total = pc.sum(values).as_py()
        totals["sum"] = None if total is None else str(total)
    elif pa.types.is_temporal(kind):
        bounds: Mapping[str, Any] = pc.min_max(values).as_py()
        totals.update({k: None if v is None else v.isoformat() for k, v in bounds.items()})
    elif pa.types.is_string(kind) or pa.types.is_boolean(kind):
        totals["distinct"] = pc.count_distinct(values, mode="only_valid").as_py()
    return totals


def complete(out: Path) -> list[Path]:
    """Every delivery directory under `out` that has its manifest, oldest business date first."""
    return sorted(p.parent for p in out.glob(f"*/*/{MANIFEST}") if not p.parent.name.startswith("."))
