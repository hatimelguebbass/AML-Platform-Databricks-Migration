"""Reads the source lakehouse at one commit, so that every table of a delivery is one consistent cut.

The source is the Iceberg catalog of the on-premises platform, served by Nessie over the Iceberg
REST protocol. Nessie versions the whole catalog: reading every table at the same commit of `main`
gives the tables as they were published together, even if the daily pipeline publishes again while
the export runs. The commit is recorded in the manifest, which makes any delivery reproducible.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote

import httpx
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import pyiceberg.io.pyarrow as iceberg_arrow
from pyiceberg.catalog import Catalog, load_catalog
from pyiceberg.expressions import AlwaysTrue, BooleanExpression
from pyiceberg.io import FileIO
from pyiceberg.io.pyarrow import PyArrowFileIO
from pyiceberg.manifest import DataFile, FileFormat
from pyiceberg.table import Table

_read_deletes = iceberg_arrow._read_deletes


def _read_position_deletes(io: FileIO, data_file: DataFile) -> dict[str, pa.ChunkedArray]:
    """Position deletes of a Parquet delete file, by data file path.

    PyIceberg 0.12 reads the `file_path` column of a delete file as a dictionary, which Arrow cannot
    decode from DELTA_LENGTH_BYTE_ARRAY pages: the encoding Trino writes for that column on every
    UPDATE, DELETE or MERGE. Any correction made through Trino at the source would stop the export.
    Read as plain strings instead; tests/unit/test_source.py reproduces Trino's encoding.
    """
    if data_file.file_format != FileFormat.PARQUET:
        return _read_deletes(io, data_file)
    with io.new_input(data_file.file_path).open() as handle:
        table = pq.read_table(handle, columns=["file_path", "pos"])
    paths = table.column("file_path")
    return {path: table.filter(pc.equal(paths, path)).column("pos") for path in pc.unique(paths).to_pylist()}


iceberg_arrow._read_deletes = _read_position_deletes


class Source(Protocol):
    @property
    def ref(self) -> str:
        """The commit every read is made at."""
        ...

    def columns(self, relation: str) -> tuple[str, ...]: ...

    def read(self, relation: str, columns: Sequence[str], row_filter: BooleanExpression | None = None) -> pa.Table:
        """The listed columns of the rows that match `row_filter` (every row if None)."""
        ...


@dataclass(frozen=True)
class S3Settings:
    endpoint: str
    region: str
    access_key_id: str
    secret_access_key: str


class IcebergSource:
    def __init__(self, catalog: Catalog, ref: str, io: FileIO | None = None) -> None:
        self._catalog = catalog
        self._ref = ref
        self._io = io

    @property
    def ref(self) -> str:
        return self._ref

    @classmethod
    def nessie(cls, uri: str, branch: str, s3: S3Settings, client: httpx.Client) -> IcebergSource:
        """The source at the current head of `branch`, pinned to that commit for every later read."""
        response = client.get(f"{uri}/api/v2/trees/{quote(branch, safe='')}")
        response.raise_for_status()
        ref = f"{branch}@{response.json()['reference']['hash']}"
        catalog = load_catalog(
            "source",
            type="rest",
            uri=f"{uri}/iceberg/{quote(ref, safe='')}",
        )
        io = PyArrowFileIO(
            {
                "s3.endpoint": s3.endpoint,
                "s3.region": s3.region,
                "s3.access-key-id": s3.access_key_id,
                "s3.secret-access-key": s3.secret_access_key,
                "s3.force-virtual-addressing": "false",
            }
        )
        return cls(catalog, ref, io)

    def _table(self, relation: str) -> Table:
        table = self._catalog.load_table(relation)
        if self._io is None:
            return table
        # The catalog advertises the storage endpoint and file IO of the engines inside the platform's
        # network; this process reads through the endpoint it was given, with Arrow's S3 client.
        return Table(table.name(), table.metadata, table.metadata_location, self._io, self._catalog)

    def columns(self, relation: str) -> tuple[str, ...]:
        return tuple(f.name for f in self._table(relation).schema().fields)

    def read(self, relation: str, columns: Sequence[str], row_filter: BooleanExpression | None = None) -> pa.Table:
        # Only the listed columns are read from storage: a column that does not cross is never loaded.
        scan = self._table(relation).scan(row_filter=row_filter or AlwaysTrue(), selected_fields=tuple(columns))
        return scan.to_arrow().select(list(columns))
