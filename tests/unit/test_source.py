from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pyiceberg.io.pyarrow as iceberg_arrow
from pyiceberg.io.pyarrow import PyArrowFileIO
from pyiceberg.manifest import FileFormat

from lakemig.export import source


def test_position_deletes_written_by_trino_are_readable(tmp_path: Path) -> None:
    path = tmp_path / "delete.parquet"
    deletes = pa.table({"file_path": ["s3://a/1.parquet", "s3://a/2.parquet", "s3://a/1.parquet"], "pos": [3, 0, 7]})
    # Trino's writer: no dictionary, delta length byte array for the paths.
    pq.write_table(deletes, path, use_dictionary=False, column_encoding={"file_path": "DELTA_LENGTH_BYTE_ARRAY"})
    delete_file = SimpleNamespace(file_format=FileFormat.PARQUET, file_path=str(path))

    read = iceberg_arrow._read_deletes(PyArrowFileIO(), delete_file)  # type: ignore[arg-type]

    assert iceberg_arrow._read_deletes is source._read_position_deletes
    assert {k: v.to_pylist() for k, v in read.items()} == {"s3://a/1.parquet": [3, 7], "s3://a/2.parquet": [0]}
