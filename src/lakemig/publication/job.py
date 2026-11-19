"""Entry points of the daily job's staging and publication tasks (resources/aml_daily.yml)."""

from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence
from datetime import date

from pyspark.sql import SparkSession

from lakemig.publication import wap

log = logging.getLogger(__name__)

ALL_DATES = "all"


def _parser(prog: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog)
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--layers", required=True, help="comma-separated published schemas, in build order")
    parser.add_argument("--business-date", required=True, type=_business_date, help="YYYY-MM-DD, or all")
    return parser


def _business_date(value: str) -> date | None:
    # Empty is refused, never read as `all`: a repair started without the job's parameters passes
    # an empty date, and it must fail instead of rebuilding every date.
    return None if value == ALL_DATES else date.fromisoformat(value)


def _lakehouse(catalog: str) -> wap.Lakehouse:
    spark = SparkSession.builder.getOrCreate()
    return wap.Lakehouse(lambda statement: [tuple(row) for row in spark.sql(statement).collect()], catalog)


def _layers(raw: str) -> list[str]:
    return [layer.strip() for layer in raw.split(",") if layer.strip()]


def open_staging(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = _parser("lakemig-open-staging").parse_args(argv)
    if args.business_date is None:
        # A rebuild replaces every date of the staging tables from bronze: there is nothing to reset.
        log.info("no business date: every date is rebuilt, staging left as it is")
        return 0
    reset = wap.open_staging(_lakehouse(args.catalog), _layers(args.layers))
    for table, dates in reset.items():
        log.info("%s: reset to the published slices of %s", table, ", ".join(map(str, dates)) or "nothing (emptied)")
    if not reset:
        log.info("staging already holds the published slices")
    return 0


def publish(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = _parser("lakemig-publish")
    parser.add_argument("--run-id", required=True, help="job run whose build is published")
    args = parser.parse_args(argv)
    for table in wap.publish(_lakehouse(args.catalog), _layers(args.layers), args.business_date, args.run_id):
        log.info("%s: %d rows, version %d", table.table, table.rows, table.version)
    return 0
