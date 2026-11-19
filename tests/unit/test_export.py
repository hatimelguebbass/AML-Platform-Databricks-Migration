from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from conftest import DAY, NEXT_DAY, TABLES, Bank
from pyiceberg.types import StringType

from lakemig.export import delivery
from lakemig.export.pseudonym import pseudonym
from lakemig.export.run import GateBlockedError, SchemaDriftError, UndatedCaptureError, export_date


def _read(root: Path, manifest: delivery.Manifest, table: str) -> pa.Table:
    entry = next(t for t in manifest.tables if t.name == table)
    return pq.read_table(root / manifest.business_date / manifest.delivery_id / entry.files[0].path)


def test_delivery_holds_the_rows_of_the_date_with_the_policy_applied(bank: Bank, tmp_path: Path) -> None:
    out = tmp_path / "exports"

    manifest = export_date(DAY, bank.plan, bank.inventory, bank.source, bank.key, out, bank.context)

    transfers = _read(out, manifest, "payment_transfer")
    assert transfers.column("transfer_id").to_pylist() == ["TRF000000001", "TRF000000002"]
    assert transfers.column("originator_account").to_pylist() == [
        pseudonym("80012ACA0", bank.key, "account_number"),
        pseudonym("8000EBD30", bank.key, "account_number"),
    ]
    accounts = _read(out, manifest, "corebanking_account")
    assert "rib" not in accounts.column_names
    assert accounts.column("account_id").to_pylist() == [9, 10, 11]
    # The same namespace on both sides keeps the join of transfers to accounts exact.
    assert set(transfers.column("originator_account").to_pylist()) <= set(accounts.column("account_number").to_pylist())
    assert {t.name: t.rows for t in manifest.tables} == {
        "change_batch": 3,
        "corebanking_account": 3,
        "payment_transfer": 2,
    }


def test_the_initial_snapshot_goes_with_the_first_date_only(bank: Bank, tmp_path: Path) -> None:
    out = tmp_path / "exports"

    manifest = export_date(NEXT_DAY, bank.plan, bank.inventory, bank.source, bank.key, out, bank.context)

    assert _read(out, manifest, "corebanking_account").column("account_id").to_pylist() == [12]
    assert _read(out, manifest, "change_batch").column("run_id").to_pylist() == ["r2"]


def test_an_undated_capture_after_the_daily_runs_began_stops_the_export(bank: Bank, tmp_path: Path) -> None:
    batches = TABLES["audit.change_batch"]
    processed = [datetime(2022, 9, 3, tzinfo=UTC)] + [datetime(2022, 9, 2, tzinfo=UTC)] * 3
    late = batches.set_column(5, "processed_at", pa.array(processed, pa.timestamp("us", tz="UTC")))
    bank.replace("audit.change_batch", late)

    with pytest.raises(UndatedCaptureError, match="r0"):
        export_date(NEXT_DAY, bank.plan, bank.inventory, bank.source, bank.key, tmp_path / "exports", bank.context)


def test_manifest_seals_files_and_carries_control_totals(bank: Bank, tmp_path: Path) -> None:
    out = tmp_path / "exports"

    manifest = export_date(DAY, bank.plan, bank.inventory, bank.source, bank.key, out, bank.context)

    root = out / manifest.business_date / manifest.delivery_id
    landed = delivery.Manifest.from_json((root / delivery.MANIFEST).read_text())
    assert landed == manifest
    assert manifest.source["lakehouse_ref"] == "main@0123abcd"
    assert manifest.pseudonymization == {"algorithm": "HMAC-SHA256", "key_id": bank.key.key_id}
    assert bank.key.material.hex() not in (root / delivery.MANIFEST).read_text()
    for entry in manifest.files():
        assert delivery.sha256_of(root / entry.path) == entry.sha256
    transfers = next(t for t in manifest.tables if t.name == "payment_transfer")
    assert transfers.control_totals["amount_paid"] == {"nulls": 0, "sum": "2510.500001"}
    assert transfers.control_totals["booked_at"]["min"] == "2022-09-01T09:00:00"
    accounts = next(t for t in manifest.tables if t.name == "corebanking_account")
    account_number = next(c for c in accounts.columns if c.name == "account_number")
    assert (account_number.treatment, account_number.namespace) == ("pseudonymize", "account_number")


def test_same_data_gives_the_same_delivery(bank: Bank, tmp_path: Path) -> None:
    out = tmp_path / "exports"

    first = export_date(DAY, bank.plan, bank.inventory, bank.source, bank.key, out, bank.context)
    again = export_date(DAY, bank.plan, bank.inventory, bank.source, bank.key, out, bank.context)

    assert again.delivery_id == first.delivery_id
    assert again.exported_at == first.exported_at
    assert delivery.complete(out) == [out / DAY.isoformat() / first.delivery_id]


def test_corrected_data_gives_a_new_delivery_next_to_the_first(bank: Bank, tmp_path: Path) -> None:
    out = tmp_path / "exports"
    first = export_date(DAY, bank.plan, bank.inventory, bank.source, bank.key, out, bank.context)
    transfers = TABLES["bronze.payment_transfer"]
    bank.replace("bronze.payment_transfer", transfers.set_column(5, "payment_format", pa.array(["Wire"] * 3)))

    corrected = export_date(DAY, bank.plan, bank.inventory, bank.source, bank.key, out, bank.context)

    assert corrected.delivery_id != first.delivery_id
    assert len(delivery.complete(out)) == 2


def test_gate_blocks_a_phone_number_in_a_column_allowed_to_cross(bank: Bank, tmp_path: Path) -> None:
    transfers = TABLES["bronze.payment_transfer"]
    leaked = pa.array(["Wire", "ACH tel 0612345678", "Cheque"])
    bank.replace("bronze.payment_transfer", transfers.set_column(5, "payment_format", leaked))

    with pytest.raises(GateBlockedError) as blocked:
        export_date(DAY, bank.plan, bank.inventory, bank.source, bank.key, tmp_path / "exports", bank.context)

    (finding,) = blocked.value.report.findings
    assert (finding.relation, finding.column, finding.detector, finding.rows) == (
        "bronze.payment_transfer",
        "payment_format",
        "phone",
        1,
    )
    assert "0612345678" not in str(blocked.value)
    assert delivery.complete(tmp_path / "exports") == []


def test_gate_blocks_a_customer_name_in_a_column_allowed_to_cross(bank: Bank, tmp_path: Path) -> None:
    transfers = TABLES["bronze.payment_transfer"]
    bank.replace(
        "bronze.payment_transfer", transfers.set_column(5, "payment_format", pa.array(["EL AMRANI Youssef"] * 3))
    )

    with pytest.raises(GateBlockedError, match="person_name"):
        export_date(DAY, bank.plan, bank.inventory, bank.source, bank.key, tmp_path / "exports", bank.context)


def test_a_column_added_at_the_source_stops_the_export(bank: Bank, tmp_path: Path) -> None:
    table = bank.catalog.load_table("bronze.payment_transfer")
    with table.update_schema() as update:
        update.add_column("beneficiary_name", StringType())

    with pytest.raises(SchemaDriftError, match="beneficiary_name"):
        export_date(DAY, bank.plan, bank.inventory, bank.source, bank.key, tmp_path / "exports", bank.context)
