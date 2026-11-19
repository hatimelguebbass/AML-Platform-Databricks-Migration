from pathlib import Path

import pytest
from conftest import DAY, Bank

from lakemig.export import delivery
from lakemig.export.run import export_date
from lakemig.transfer.landing import DirectoryLanding, Outcome, TransferError, deliver

ROOT = "/Volumes/aml_dev/landing/deliveries"


def _export(bank: Bank, tmp_path: Path) -> tuple[Path, delivery.Manifest]:
    manifest = export_date(DAY, bank.plan, bank.inventory, bank.source, bank.key, tmp_path / "exports", bank.context)
    return tmp_path / "exports" / manifest.business_date / manifest.delivery_id, manifest


def test_a_delivery_lands_once(bank: Bank, tmp_path: Path) -> None:
    local, manifest = _export(bank, tmp_path)
    landing_dir = tmp_path / "landing"
    landing = DirectoryLanding(landing_dir)

    assert deliver(local, landing, ROOT) is Outcome.DELIVERED
    assert deliver(local, landing, ROOT) is Outcome.ALREADY_DELIVERED

    remote = landing_dir / ROOT.lstrip("/") / manifest.business_date / manifest.delivery_id
    assert sorted(p.relative_to(remote).as_posix() for p in remote.rglob("*") if p.is_file()) == sorted(
        [delivery.MANIFEST, *(f.path for f in manifest.files())]
    )


def test_an_interrupted_upload_resumes_with_the_missing_files(bank: Bank, tmp_path: Path) -> None:
    local, manifest = _export(bank, tmp_path)
    landing_dir = tmp_path / "landing"
    landing = DirectoryLanding(landing_dir)
    deliver(local, landing, ROOT)
    remote = landing_dir / ROOT.lstrip("/") / manifest.business_date / manifest.delivery_id
    (remote / delivery.MANIFEST).unlink()
    truncated = remote / manifest.files()[0].path
    truncated.write_bytes(truncated.read_bytes()[:10])

    assert deliver(local, landing, ROOT) is Outcome.RESUMED
    assert delivery.sha256_of(truncated) == manifest.files()[0].sha256


def test_a_different_manifest_under_the_same_id_is_refused(bank: Bank, tmp_path: Path) -> None:
    local, manifest = _export(bank, tmp_path)
    landing_dir = tmp_path / "landing"
    deliver(local, DirectoryLanding(landing_dir), ROOT)
    remote = landing_dir / ROOT.lstrip("/") / manifest.business_date / manifest.delivery_id / delivery.MANIFEST
    remote.write_text(remote.read_text().replace(manifest.content_digest, "0" * 64))

    with pytest.raises(TransferError, match="differs"):
        deliver(local, DirectoryLanding(landing_dir), ROOT)
