"""Acceptance checks of a delivery, before and after Spark reads it.

Before: the manifest belongs where it lies, matches the landing contract, keeps the pseudonymization
key of the deliveries already accepted, and every file it lists is present with its size and SHA-256.
After: the control totals recomputed from the rows Spark read equal the ones the export wrote. The
byte checks prove the transfer; the totals prove that what Spark reads is what the source held.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from lakemig.ingestion.contract import Contract, totals_kind
from lakemig.manifest import Manifest, sha256_of


class DeliveryRejectedError(Exception):
    def __init__(self, reasons: list[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = reasons


def check_files(directory: Path, manifest: Manifest, contract: Contract, accepted_key_id: str | None) -> None:
    """Raises DeliveryRejectedError with every reason found, cheapest checks first."""
    reasons: list[str] = []
    if directory.parent.name != manifest.business_date or directory.name != manifest.delivery_id:
        reasons.append(
            f"manifest of {manifest.business_date}/{manifest.delivery_id} lies in "
            f"{directory.parent.name}/{directory.name}"
        )
    if not manifest.delivery_id.startswith(f"{manifest.business_date}-{manifest.content_digest[:12]}"):
        reasons.append(f"delivery id {manifest.delivery_id} does not derive from its content digest")
    key_id = manifest.pseudonymization.get("key_id")
    if accepted_key_id is not None and key_id != accepted_key_id:
        # Pseudonyms of two keys never join: accepting it would silently split every customer in two.
        reasons.append(f"pseudonymization key {key_id} is not {accepted_key_id}, the key of the accepted deliveries")
    if manifest.gate.get("findings") != 0:
        reasons.append(f"the export gate reported {manifest.gate.get('findings')} findings")
    reasons += contract.deviations(manifest)
    for table in manifest.tables:
        if sum(f.rows for f in table.files) != table.rows:
            reasons.append(f"{table.name}: files hold {sum(f.rows for f in table.files)} rows, table {table.rows}")
    if reasons:
        raise DeliveryRejectedError(reasons)

    for entry in manifest.files():
        path = directory / entry.path
        if not path.is_file():
            reasons.append(f"{entry.path}: missing")
        elif path.stat().st_size != entry.bytes:
            reasons.append(f"{entry.path}: {path.stat().st_size} bytes, manifest {entry.bytes}")
        elif sha256_of(path) != entry.sha256:
            reasons.append(f"{entry.path}: SHA-256 differs from the manifest")
    if reasons:
        raise DeliveryRejectedError(reasons)


def compare_totals(
    table: str,
    columns: Mapping[str, str],
    expected: Mapping[str, Mapping[str, Any]],
    actual: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    """Differences between the manifest's control totals and the recomputed ones, per column.

    `columns` maps each column to its Arrow type; `actual` holds what totals_expressions computed:
    nulls and distinct as integers, sums and range bounds as Spark's string rendering.
    """
    found = []
    for column, arrow_type in columns.items():
        want, got = expected.get(column, {}), actual[column]
        if want.get("nulls") != got["nulls"]:
            found.append(f"{table}.{column}: {got['nulls']} nulls, manifest {want.get('nulls')}")
        match totals_kind(arrow_type):
            case "sum":
                if _decimal(want.get("sum")) != _decimal(got["sum"]):
                    found.append(f"{table}.{column}: sum {got['sum']}, manifest {want.get('sum')}")
            case "range":
                for bound in ("min", "max"):
                    if _instant(want.get(bound), arrow_type) != _instant(got[bound], arrow_type):
                        found.append(f"{table}.{column}: {bound} {got[bound]}, manifest {want.get(bound)}")
            case "distinct":
                if want.get("distinct") != got["distinct"]:
                    found.append(f"{table}.{column}: {got['distinct']} distinct, manifest {want.get('distinct')}")
    return found


def _decimal(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def _instant(value: str | None, arrow_type: str) -> date | datetime | None:
    """A bound as a comparable value: dates as dates, instants in UTC without zone, wall-clock times as is.

    The export writes ISO 8601 with the zone for zoned columns; Spark renders them in the session
    zone, which the ingestion sets to UTC, without one.
    """
    if value is None:
        return None
    if arrow_type.startswith("date"):
        return date.fromisoformat(value[:10])
    parsed = datetime.fromisoformat(value)
    return parsed.astimezone(UTC).replace(tzinfo=None) if parsed.tzinfo else parsed
