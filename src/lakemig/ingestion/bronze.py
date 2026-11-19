"""Bronze ingestion job: sealed deliveries of the landing volume into Delta tables, one date at a time.

A delivery is accepted whole or not at all. Its files are checked against the manifest and the
landing contract, read by Spark with the contract's schema, and their control totals recomputed;
only then is each table's slice for the business date replaced (`replaceWhere` on the delivery
date). A rerun after a failure between two tables rewrites the same slices, and a correction of a
date replaces the slice of the delivery before it. Downstream reads a date only once the delivery
log holds an accepted row for it (step 5).

A rejected delivery is logged with every reason and stays in the landing volume untouched: the
volume is the quarantine, the log its register. The job fails when it rejected anything, after
ingesting every other date, so that a refusal is never a green run.

Deliveries under another pseudonymization key than the accepted one are refused, except in a key
rotation (`--rekey`): every accepted date must then come again under the new key, and the run
writes nothing unless all of them pass, since a half-rotated bronze would not join across dates.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from pyspark.sql import Column as SparkColumn
from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F

from lakemig.ingestion import register
from lakemig.ingestion.contract import (
    DELIVERY_DATE,
    DELIVERY_FILE,
    DELIVERY_ID,
    LINEAGE,
    LOADED_AT,
    Column,
    Contract,
    totals_kind,
)
from lakemig.ingestion.register import Sealed, Status
from lakemig.ingestion.verify import DeliveryRejectedError, check_files, compare_totals
from lakemig.manifest import Manifest, TableEntry

log = logging.getLogger(__name__)

CATALOG_NAME = re.compile(r"[a-z][a-z0-9_]*")
LANDING_VOLUME = "landing/deliveries"
DELIVERY_LOG = "landing.delivery_log"
BRONZE = "bronze"
_LOG_COLUMNS = (
    "delivery_id STRING NOT NULL, business_date DATE NOT NULL, exported_at TIMESTAMP, content_digest STRING, "
    "key_id STRING, status STRING NOT NULL, reasons ARRAY<STRING> NOT NULL, rows BIGINT, summary STRING, "
    "run_id STRING NOT NULL, logged_at TIMESTAMP NOT NULL"
)


class BronzeSchemaError(RuntimeError):
    """A bronze table no longer matches the contract the code ships: a reviewed migration must align them."""


class RejectedDeliveriesError(RuntimeError):
    pass


class IncompleteRekeyError(RuntimeError):
    """A key rotation that would leave dates under the old key: nothing was written."""


@dataclass(frozen=True)
class Outcome:
    accepted: list[str]
    rejected: list[str]
    superseded: list[str]


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(prog="lakemig-ingest")
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--run-id", required=True, help="job run id, recorded in the delivery log")
    parser.add_argument("--retry", default="", help="comma-separated delivery ids to decide again")
    parser.add_argument("--rekey", default="", help="key id of a rotation: every accepted date comes again under it")
    args = parser.parse_args(argv)
    if not CATALOG_NAME.fullmatch(args.catalog):
        # The catalog is interpolated into SQL as an identifier: only plain names are accepted.
        parser.error(f"--catalog {args.catalog!r} is not a plain catalog name")
    outcome = run(
        SparkSession.builder.getOrCreate(),
        catalog=args.catalog,
        run_id=args.run_id,
        retry={r.strip() for r in args.retry.split(",") if r.strip()},
        rekey=args.rekey.strip() or None,
    )
    log.info(
        "%d accepted, %d rejected, %d superseded", len(outcome.accepted), len(outcome.rejected), len(outcome.superseded)
    )
    if outcome.rejected:
        # Raised, not returned: a wheel task ignores its entry point's return value, and a rejection
        # must fail the run (a refused delivery with a green run is the failure mode this job prevents).
        raise RejectedDeliveriesError(
            f"rejected deliveries, see {args.catalog}.{DELIVERY_LOG}: {', '.join(outcome.rejected)}"
        )
    return 0


def run(
    spark: SparkSession,
    *,
    catalog: str,
    run_id: str,
    retry: set[str],
    rekey: str | None = None,
    contract: Contract | None = None,
) -> Outcome:
    # Zoned columns are rendered for the control totals in the session zone: UTC keeps them comparable.
    spark.conf.set("spark.sql.session.timeZone", "UTC")
    contract = contract or Contract.read()
    journal = DeliveryLog(spark, f"`{catalog}`.{DELIVERY_LOG}", run_id)
    tables = {name: f"`{catalog}`.{BRONZE}.`{name}`" for name in contract.tables}
    for name, columns in contract.tables.items():
        _ensure_table(spark, tables[name], columns)

    root = Path("/Volumes", catalog, LANDING_VOLUME)
    sealed, decided = register.scan(root), journal.decided()
    if rekey is not None:
        retry = retry | register.refused_under(sealed, decided, rekey)
    steps = register.plan(sealed, decided, retry)
    if rekey is not None:
        problems = register.rekey_problems(steps, journal.accepted_dates(), rekey)
        if problems:
            raise IncompleteRekeyError(f"key rotation to {rekey} refused, nothing ingested: " + "; ".join(problems))
    outcome = Outcome([], [], [])
    verified: list[tuple[Sealed, Manifest, dict[str, DataFrame]]] = []
    for step in steps:
        for stale in step.superseded:
            journal.append(stale, Status.SUPERSEDED, [f"a later export of {step.business_date} exists"])
            outcome.superseded.append(stale.delivery_id)
        if step.ingest is None:
            continue
        delivery = step.ingest
        try:
            if delivery.manifest is None:
                raise DeliveryRejectedError([delivery.error or "unreadable manifest"])
            check_files(delivery.directory, delivery.manifest, contract, rekey or journal.accepted_key_id())
            loaded = _load_and_reconcile(spark, delivery.directory, delivery.manifest, contract)
        except DeliveryRejectedError as rejected:
            log.error("%s rejected: %s", delivery.delivery_id, rejected)
            journal.append(delivery, Status.REJECTED, rejected.reasons)
            outcome.rejected.append(delivery.delivery_id)
            continue
        verified.append((delivery, delivery.manifest, loaded))
        if rekey is None:
            _write(delivery, delivery.manifest, loaded, tables, contract, journal, outcome)
    if rekey is not None:
        if outcome.rejected:
            raise IncompleteRekeyError(
                f"key rotation to {rekey} stopped before writing: {', '.join(outcome.rejected)} rejected"
            )
        for delivery, manifest, loaded in verified:
            _write(delivery, manifest, loaded, tables, contract, journal, outcome)
    return outcome


def _write(
    delivery: Sealed,
    manifest: Manifest,
    loaded: dict[str, DataFrame],
    tables: dict[str, str],
    contract: Contract,
    journal: DeliveryLog,
    outcome: Outcome,
) -> None:
    for name, frame in loaded.items():
        _replace_slice(frame, tables[name], manifest, contract.tables[name])
    journal.append(delivery, Status.ACCEPTED, [])
    outcome.accepted.append(delivery.delivery_id)
    log.info("%s accepted: %d rows", delivery.delivery_id, sum(t.rows for t in manifest.tables))


def _load_and_reconcile(
    spark: SparkSession, directory: Path, manifest: Manifest, contract: Contract
) -> dict[str, DataFrame]:
    """Every table of the delivery read with the contract's schema, its control totals recomputed."""
    frames, reasons = {}, []
    for table in manifest.tables:
        columns = contract.tables[table.name]
        schema = ", ".join(f"`{c.name}` {c.spark_type}" for c in columns)
        frame = spark.read.schema(schema).parquet(*(str(directory / f.path) for f in table.files))
        reasons += _reconcile(frame, table, columns)
        frames[table.name] = frame
    if reasons:
        raise DeliveryRejectedError(reasons)
    return frames


def _reconcile(frame: DataFrame, table: TableEntry, columns: Sequence[Column]) -> list[str]:
    expressions: list[SparkColumn] = [F.count(F.lit(1)).alias("rows")]
    for i, column in enumerate(columns):
        value = F.col(f"`{column.name}`")
        expressions.append(F.count_if(value.isNull()).alias(f"c{i}_nulls"))
        match totals_kind(column.type):
            case "sum":
                expressions.append(F.sum(value).cast("string").alias(f"c{i}_sum"))
            case "range":
                expressions.append(F.min(value).cast("string").alias(f"c{i}_min"))
                expressions.append(F.max(value).cast("string").alias(f"c{i}_max"))
            case "distinct":
                expressions.append(F.count_distinct(value).alias(f"c{i}_distinct"))
    row = frame.agg(*expressions).first()
    if row is None:
        raise RuntimeError(f"{table.name}: an aggregate without grouping returned no row")
    values = row.asDict()
    actual: dict[str, dict[str, Any]] = {
        c.name: {k.removeprefix(f"c{i}_"): v for k, v in values.items() if k.startswith(f"c{i}_")}
        for i, c in enumerate(columns)
    }
    found = [] if values["rows"] == table.rows else [f"{table.name}: {values['rows']} rows read, manifest {table.rows}"]
    return found + compare_totals(table.name, {c.name: c.type for c in columns}, table.control_totals, actual)


def _replace_slice(frame: DataFrame, table: str, manifest: Manifest, columns: Sequence[Column]) -> None:
    (
        frame.select(
            *(F.col(f"`{c.name}`") for c in columns),
            F.lit(manifest.delivery_id).alias(DELIVERY_ID),
            F.lit(manifest.business_date).cast("date").alias(DELIVERY_DATE),
            F.col("_metadata.file_path").alias(DELIVERY_FILE),
            F.current_timestamp().alias(LOADED_AT),
        )
        .write.format("delta")
        .mode("overwrite")
        .option("replaceWhere", f"{DELIVERY_DATE} = DATE'{manifest.business_date}'")
        .saveAsTable(table)
    )


def _ensure_table(spark: SparkSession, table: str, columns: Sequence[Column]) -> None:
    expected = [(c.name, c.spark_type.lower(), c.nullable) for c in columns]
    expected += [(name, kind.lower(), name == DELIVERY_FILE) for name, kind in LINEAGE]
    definition = ", ".join(f"`{name}` {kind}{'' if nullable else ' NOT NULL'}" for name, kind, nullable in expected)
    spark.sql(
        f"CREATE TABLE IF NOT EXISTS {table} ({definition}) USING DELTA "
        "COMMENT 'Deliveries of the on-premises export, one slice per business date (lakemig-ingest).'"
    )
    actual = [(f.name, f.dataType.simpleString(), f.nullable) for f in spark.table(table).schema.fields]
    if actual != expected:
        raise BronzeSchemaError(f"{table} has columns {actual}, the landing contract gives {expected}")


class DeliveryLog:
    """Append-only `landing.delivery_log`: one row per decision; a delivery's status is its latest row."""

    def __init__(self, spark: SparkSession, table: str, run_id: str) -> None:
        self._spark, self._table, self._run_id = spark, table, run_id
        spark.sql(
            f"CREATE TABLE IF NOT EXISTS {table} ({_LOG_COLUMNS}) USING DELTA "
            "COMMENT 'Decision on each sealed delivery of the landing volume (lakemig-ingest).'"
        )
        # Ordered in SQL so that timestamps are never compared across Python time zone conversions.
        self._decided = {
            r["delivery_id"]: Status(r["status"])
            for r in spark.sql(
                f"SELECT delivery_id, max_by(status, logged_at) AS status FROM {table} GROUP BY delivery_id"  # noqa: S608
            ).collect()
        }
        accepted = f"SELECT max_by(key_id, logged_at) FROM {table} WHERE status = 'accepted'"  # noqa: S608
        key = spark.sql(accepted).first()
        self._accepted_key_id: str | None = key[0] if key else None

    def decided(self) -> dict[str, Status]:
        return dict(self._decided)

    def accepted_key_id(self) -> str | None:
        return self._accepted_key_id

    def accepted_dates(self) -> set[str]:
        rows = self._spark.sql(
            f"SELECT DISTINCT cast(business_date AS STRING) AS d FROM (SELECT business_date, "  # noqa: S608
            f"max_by(status, logged_at) AS status FROM {self._table} GROUP BY delivery_id, business_date) "
            "WHERE status = 'accepted'"
        ).collect()
        return {r["d"] for r in rows}

    def append(self, delivery: Sealed, status: Status, reasons: list[str]) -> None:
        manifest = delivery.manifest
        row = {
            "delivery_id": delivery.delivery_id,
            "business_date": date.fromisoformat(delivery.business_date),
            "exported_at": datetime.fromisoformat(manifest.exported_at) if manifest else None,
            "content_digest": manifest.content_digest if manifest else None,
            "key_id": manifest.pseudonymization.get("key_id") if manifest else None,
            "status": status.value,
            "reasons": reasons,
            "rows": sum(t.rows for t in manifest.tables) if manifest else None,
            "summary": register.manifest_summary(manifest) if manifest else None,
            "run_id": self._run_id,
            "logged_at": datetime.now(UTC),
        }
        schema = self._spark.table(self._table).schema
        self._spark.createDataFrame([row], schema=schema).write.mode("append").saveAsTable(self._table)
        self._decided[delivery.delivery_id] = status
        if status is Status.ACCEPTED and manifest:
            self._accepted_key_id = manifest.pseudonymization.get("key_id")


if __name__ == "__main__":
    sys.exit(main())
