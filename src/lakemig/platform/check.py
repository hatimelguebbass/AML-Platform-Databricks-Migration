"""Platform check, run as a job of the bundle after each deployment.

Proves on the target itself what the later steps rely on: the catalog and its schemas exist, the
job can write and read a managed table, and the runtime versions are the ones the code is tested
with. One row per run is appended to `<catalog>.migration.platform_check`, which keeps the history
of what each environment ran.
"""

from __future__ import annotations

import argparse
import logging
import platform
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from importlib import metadata

from pyspark.sql import SparkSession

log = logging.getLogger(__name__)

CHECK_TABLE = "migration.platform_check"
# Installed from PyPI into the serverless environment of the job: proves the package index is
# reachable from the workspace, which the transformations depend on.
TRANSFORMATION_PACKAGE = "dbt-databricks"


class PlatformCheckError(Exception):
    pass


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(prog="lakemig-platform-check")
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--schemas", required=True, help="comma-separated schemas the catalog must hold")
    parser.add_argument("--run-id", default="", help="job run id, for the history table")
    parser.add_argument("--min-python", default="3.12")
    args = parser.parse_args(argv)
    run(
        SparkSession.builder.getOrCreate(),
        catalog=args.catalog,
        schemas=[s.strip() for s in args.schemas.split(",") if s.strip()],
        run_id=args.run_id,
        min_python=tuple(int(p) for p in args.min_python.split(".")),
    )
    return 0


def run(spark: SparkSession, *, catalog: str, schemas: Sequence[str], run_id: str, min_python: tuple[int, ...]) -> None:
    present = {row[0] for row in spark.sql(f"SHOW SCHEMAS IN `{catalog}`").collect()}
    missing = sorted(set(schemas) - present)
    if missing:
        raise PlatformCheckError(f"catalog {catalog} lacks schemas {', '.join(missing)}")
    if sys.version_info[: len(min_python)] < min_python:
        raise PlatformCheckError(f"Python {platform.python_version()} is older than {'.'.join(map(str, min_python))}")

    row = {
        "checked_at": datetime.now(UTC).replace(tzinfo=None),
        "run_id": run_id,
        "python_version": platform.python_version(),
        "spark_version": spark.version,
        "transformation_package": f"{TRANSFORMATION_PACKAGE}=={_version(TRANSFORMATION_PACKAGE)}",
    }
    table = f"`{catalog}`.{CHECK_TABLE}"
    spark.sql(
        f"CREATE TABLE IF NOT EXISTS {table} (checked_at TIMESTAMP_NTZ, run_id STRING, python_version STRING, "
        "spark_version STRING, transformation_package STRING)"
    )
    spark.createDataFrame([row], schema=spark.table(table).schema).write.mode("append").saveAsTable(table)
    if spark.table(table).where(f"run_id = '{_literal(run_id)}'").count() < 1:
        raise PlatformCheckError(f"the row written to {table} cannot be read back")
    log.info("platform check passed: %s", row)


def _version(package: str) -> str:
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError as error:
        raise PlatformCheckError(f"{package} is not installed in the job environment") from error


def _literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


if __name__ == "__main__":
    sys.exit(main())
