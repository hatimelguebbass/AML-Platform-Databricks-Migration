import copy
import shutil
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import DAY, NEXT_DAY, Bank

from lakemig.export.pseudonym import Key
from lakemig.export.run import export_date
from lakemig.ingestion.contract import Column, Contract, ContractError, spark_type
from lakemig.ingestion.register import Sealed, Status, plan, refused_under, rekey_problems, scan
from lakemig.ingestion.verify import DeliveryRejectedError, check_files, compare_totals
from lakemig.manifest import MANIFEST, Manifest


def _deliver(bank: Bank, root: Path, day: Any = DAY) -> tuple[Path, Manifest]:
    manifest = export_date(day, bank.plan, bank.inventory, bank.source, bank.key, root, bank.context)
    return root / manifest.business_date / manifest.delivery_id, manifest


def _reasons(directory: Path, manifest: Manifest, contract: Contract, key_id: str | None = None) -> list[str]:
    with pytest.raises(DeliveryRejectedError) as rejected:
        check_files(directory, manifest, contract, key_id)
    return rejected.value.reasons


@pytest.mark.parametrize(
    ("arrow", "spark"),
    [
        ("string", "STRING"),
        ("large_string", "STRING"),
        ("int32", "INT"),
        ("int64", "BIGINT"),
        ("bool", "BOOLEAN"),
        ("date32[day]", "DATE"),
        ("decimal128(22, 6)", "DECIMAL(22,6)"),
        ("timestamp[us]", "TIMESTAMP_NTZ"),
        ("timestamp[us, tz=UTC]", "TIMESTAMP"),
    ],
)
def test_arrow_types_map_to_the_spark_type_that_reads_them(arrow: str, spark: str) -> None:
    assert spark_type(arrow) == spark


def test_nanosecond_timestamps_have_no_mapping() -> None:
    with pytest.raises(ContractError):
        spark_type("timestamp[ns]")


def test_contract_is_frozen_from_deliveries_and_read_back(bank: Bank, tmp_path: Path) -> None:
    _, manifest = _deliver(bank, tmp_path / "landing")
    contract = Contract.from_manifests([manifest])
    contract.write(tmp_path / "contract.json")

    assert Contract.read(tmp_path / "contract.json") == contract
    assert [c.name for c in contract.tables["payment_transfer"]][:2] == ["transfer_id", "booked_at"]
    assert contract.deviations(manifest) == []


def test_deliveries_that_disagree_on_a_table_give_no_contract(bank: Bank, tmp_path: Path) -> None:
    _, manifest = _deliver(bank, tmp_path / "landing")
    table = manifest.tables[0]
    renamed = replace(table, columns=(replace(table.columns[0], name="renamed"), *table.columns[1:]))
    other = replace(manifest, delivery_id="other", tables=(renamed, *manifest.tables[1:]))

    with pytest.raises(ContractError, match="disagree"):
        Contract.from_manifests([manifest, other])


def test_an_exported_column_cannot_take_a_lineage_name() -> None:
    assert Contract({"t": (Column("_delivery_id", "string", True),)}).problems() == [
        "t._delivery_id: reserved for the ingestion lineage"
    ]


def test_a_delivery_as_exported_is_accepted(bank: Bank, tmp_path: Path) -> None:
    directory, manifest = _deliver(bank, tmp_path / "landing")

    check_files(directory, manifest, Contract.from_manifests([manifest]), bank.key.key_id)


def test_a_file_altered_after_export_is_rejected(bank: Bank, tmp_path: Path) -> None:
    directory, manifest = _deliver(bank, tmp_path / "landing")
    contract = Contract.from_manifests([manifest])
    target = directory / manifest.files()[0].path
    altered = bytearray(target.read_bytes())
    altered[-20] ^= 0xFF
    target.write_bytes(bytes(altered))

    assert _reasons(directory, manifest, contract) == [f"{manifest.files()[0].path}: SHA-256 differs from the manifest"]


def test_a_missing_file_is_rejected(bank: Bank, tmp_path: Path) -> None:
    directory, manifest = _deliver(bank, tmp_path / "landing")
    contract = Contract.from_manifests([manifest])
    (directory / manifest.files()[-1].path).unlink()

    assert _reasons(directory, manifest, contract) == [f"{manifest.files()[-1].path}: missing"]


def test_a_delivery_under_another_key_is_rejected(bank: Bank, tmp_path: Path) -> None:
    directory, manifest = _deliver(bank, tmp_path / "landing")
    other_key = Key(bytes(reversed(range(32)))).key_id

    reasons = _reasons(directory, manifest, Contract.from_manifests([manifest]), other_key)

    assert reasons == [f"pseudonymization key {bank.key.key_id} is not {other_key}, the key of the accepted deliveries"]


def test_a_column_outside_the_contract_is_rejected_not_rescued(bank: Bank, tmp_path: Path) -> None:
    directory, manifest = _deliver(bank, tmp_path / "landing")
    full = Contract.from_manifests([manifest])
    narrower = Contract({**full.tables, "payment_transfer": full.tables["payment_transfer"][:-1]})
    dropped = full.tables["payment_transfer"][-1].name

    assert _reasons(directory, manifest, narrower) == [
        f"payment_transfer.{dropped}: column is not in the landing contract"
    ]


def test_a_type_change_is_rejected(bank: Bank, tmp_path: Path) -> None:
    directory, manifest = _deliver(bank, tmp_path / "landing")
    full = Contract.from_manifests([manifest])
    first, *rest = full.tables["payment_transfer"]
    changed = Contract({**full.tables, "payment_transfer": (replace(first, type="int64"), *rest)})

    assert _reasons(directory, manifest, changed) == [
        f"payment_transfer.{first.name}: {first.type} nullable={first.nullable}, "
        f"contract int64 nullable={first.nullable}"
    ]


def test_a_manifest_copied_under_another_date_is_rejected(bank: Bank, tmp_path: Path) -> None:
    directory, manifest = _deliver(bank, tmp_path / "landing")
    moved = tmp_path / "landing" / NEXT_DAY.isoformat() / manifest.delivery_id
    shutil.copytree(directory, moved)

    reasons = _reasons(moved, manifest, Contract.from_manifests([manifest]))

    assert reasons == [
        f"manifest of {manifest.business_date}/{manifest.delivery_id} lies in {NEXT_DAY}/{manifest.delivery_id}"
    ]


def test_control_totals_compare_across_renderings(bank: Bank, tmp_path: Path) -> None:
    _, manifest = _deliver(bank, tmp_path / "landing")
    table = next(t for t in manifest.tables if t.name == "payment_transfer")
    columns = {c.name: c.type for c in table.columns}
    # As Spark renders them: decimals at their scale, instants in the UTC session zone, without one.
    spark = copy.deepcopy(table.control_totals)
    spark["amount_paid"]["sum"] = "2510.500001"
    spark["_ingested_at"].update(min="2022-09-02 00:00:00", max="2022-09-02 00:00:00")
    spark["booked_at"].update(min="2022-09-01 09:00:00", max="2022-09-01 10:00:00")

    assert compare_totals(table.name, columns, table.control_totals, spark) == []

    spark["amount_paid"]["sum"] = "2510.500002"
    spark["booked_at"]["max"] = "2022-09-01 11:00:00"
    assert compare_totals(table.name, columns, table.control_totals, spark) == [
        "payment_transfer.booked_at: max 2022-09-01 11:00:00, manifest 2022-09-01T10:00:00",
        "payment_transfer.amount_paid: sum 2510.500002, manifest 2510.500001",
    ]


def _sealed(day: str, delivery_id: str, exported_at: str, bank: Bank, tmp_path: Path) -> Sealed:
    _, manifest = _deliver(bank, tmp_path / delivery_id)
    return Sealed(tmp_path / day / delivery_id, replace(manifest, exported_at=exported_at))


def test_the_latest_export_of_a_date_is_ingested_and_older_ones_superseded(bank: Bank, tmp_path: Path) -> None:
    first = _sealed("2022-09-01", "2022-09-01-aaa", "2026-11-15T10:00:00+00:00", bank, tmp_path)
    correction = _sealed("2022-09-01", "2022-09-01-bbb", "2026-11-15T11:00:00+00:00", bank, tmp_path)
    other_day = _sealed("2022-09-02", "2022-09-02-ccc", "2026-11-15T10:00:00+00:00", bank, tmp_path)

    steps = plan([other_day, correction, first], decided={})

    assert [(s.business_date, s.ingest, s.superseded) for s in steps] == [
        ("2022-09-01", correction, (first,)),
        ("2022-09-02", other_day, ()),
    ]


def test_decided_deliveries_are_left_alone_unless_retried(bank: Bank, tmp_path: Path) -> None:
    accepted = _sealed("2022-09-01", "2022-09-01-aaa", "2026-11-15T10:00:00+00:00", bank, tmp_path)
    rejected = _sealed("2022-09-02", "2022-09-02-bbb", "2026-11-15T10:00:00+00:00", bank, tmp_path)
    decided = {accepted.delivery_id: Status.ACCEPTED, rejected.delivery_id: Status.REJECTED}

    assert plan([accepted, rejected], decided) == []
    assert [s.ingest for s in plan([accepted, rejected], decided, retry={rejected.delivery_id})] == [rejected]


def test_a_stale_export_uploaded_late_does_not_replace_the_accepted_one(bank: Bank, tmp_path: Path) -> None:
    current = _sealed("2022-09-01", "2022-09-01-bbb", "2026-11-15T11:00:00+00:00", bank, tmp_path)
    stale = _sealed("2022-09-01", "2022-09-01-aaa", "2026-11-15T10:00:00+00:00", bank, tmp_path)

    steps = plan([current, stale], decided={current.delivery_id: Status.ACCEPTED})

    assert [(s.ingest, s.superseded) for s in steps] == [(None, (stale,))]


def _under_key(sealed: Sealed, key_id: str) -> Sealed:
    assert sealed.manifest is not None
    return replace(sealed, manifest=replace(sealed.manifest, pseudonymization={"key_id": key_id}))


def test_a_key_rotation_must_re_key_every_accepted_date(bank: Bank, tmp_path: Path) -> None:
    day1 = _under_key(_sealed("2022-09-01", "2022-09-01-new", "2026-11-16T10:00:00+00:00", bank, tmp_path), "k2")
    day2 = _under_key(_sealed("2022-09-02", "2022-09-02-new", "2026-11-16T10:00:00+00:00", bank, tmp_path), "k1")
    steps = plan([day1, day2], decided={})

    assert rekey_problems(steps, {"2022-09-01", "2022-09-02", "2022-09-03"}, "k2") == [
        "2022-09-03: no new delivery under key k2 to replace the accepted one",
        "2022-09-02: delivery 2022-09-02-new is under key k1, not k2",
    ]
    assert rekey_problems(plan([day1], decided={}), {"2022-09-01"}, "k2") == []
    assert rekey_problems([], set(), "k2") == ["no delivery under key k2 to ingest"]


def test_a_rotation_decides_again_what_an_ordinary_run_refused_for_the_new_key(bank: Bank, tmp_path: Path) -> None:
    refused = _under_key(_sealed("2022-09-01", "2022-09-01-new", "2026-11-16T10:00:00+00:00", bank, tmp_path), "k2")
    old = _under_key(_sealed("2022-09-02", "2022-09-02-old", "2026-11-15T10:00:00+00:00", bank, tmp_path), "k1")
    decided = {refused.delivery_id: Status.REJECTED, old.delivery_id: Status.REJECTED}

    assert refused_under([refused, old], decided, "k2") == {refused.delivery_id}
    assert refused_under([refused], {refused.delivery_id: Status.ACCEPTED}, "k2") == set()


def test_scan_finds_sealed_deliveries_only(bank: Bank, tmp_path: Path) -> None:
    root = tmp_path / "landing"
    directory, manifest = _deliver(bank, root)
    unsealed = root / "2022-09-02" / "2022-09-02-0123456789ab"
    shutil.copytree(directory, unsealed)
    (unsealed / MANIFEST).unlink()
    stray = root / "notes" / "x"
    stray.mkdir(parents=True)
    (stray / MANIFEST).write_text("{}")
    broken = root / "2022-09-03" / "2022-09-03-0123456789ab"
    broken.mkdir(parents=True)
    (broken / MANIFEST).write_text("{not json")

    found = scan(root)

    assert [(s.delivery_id, s.manifest == manifest) for s in found] == [
        (manifest.delivery_id, True),
        (broken.name, False),
    ]
    assert (found[1].error or "").startswith("unreadable manifest")


def test_a_rejection_fails_the_job_run(monkeypatch: pytest.MonkeyPatch) -> None:
    # The job runner ignores the entry point's return value: only an exception fails the task.
    from lakemig.ingestion import bronze

    monkeypatch.setattr(bronze, "SparkSession", SimpleNamespace(builder=SimpleNamespace(getOrCreate=lambda: None)))
    monkeypatch.setattr(bronze, "run", lambda *_, **__: bronze.Outcome(["a"], ["2022-09-05-1fd9885fbd64"], []))

    with pytest.raises(bronze.RejectedDeliveriesError, match="2022-09-05-1fd9885fbd64"):
        bronze.main(["--catalog=aml_dev", "--run-id=1"])


def test_the_catalog_must_be_a_plain_name() -> None:
    from lakemig.ingestion import bronze

    with pytest.raises(SystemExit):
        bronze.main(["--catalog=aml_dev`; DROP", "--run-id=1"])
