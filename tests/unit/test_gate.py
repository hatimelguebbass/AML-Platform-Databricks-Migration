import pyarrow as pa
import pytest

from lakemig.export.gate import GateReport, NameIndex, mask, normalize_names, scan
from lakemig.export.policy import Exemption

NAMES = NameIndex.from_table(
    pa.table({"first_name": ["Salma", None], "last_name": ["Benjelloun", None], "legal_name": [None, "Atlas Négoce"]}),
    [["first_name", "last_name"], ["legal_name"]],
)


def _detected(*values: str | None, names: NameIndex = NAMES) -> set[str]:
    report = GateReport()
    scan("t.t", pa.table({"c": pa.array(values, pa.string())}), names, [], report)
    return {f.detector for f in report.findings}


@pytest.mark.parametrize(
    ("value", "detector"),
    [
        ("contact: salma.b@menara.ma", "email"),
        ("+212612345678", "phone"),
        ("tel 06 12 34 56 78", "phone"),
        ("00212-522-123456", "phone"),
        ("011780000012345678901234", "bank_account"),
        ("MA64 0117 8000 0012 3456 7890 1234", "bank_account"),
        ("CIN BE123456", "national_id"),
        ("be-1234", "national_id"),
        ("AB1234567", "passport"),
        ("4111 1111 1111 1111", "card_number"),
        ("SALMA BENJELLOUN", "person_name"),
        ("Benjelloun, Salma", "person_name"),
        ("atlas negoce", "person_name"),
        # A card processor's peer-to-peer descriptor (the drill of step 9, docs/postmortems/).
        ("P2P SALMA BENJELLOUN", "person_name"),
        ("Virement Atlas Négoce SARL", "person_name"),
    ],
)
def test_each_detector_finds_its_identifier(value: str, detector: str) -> None:
    assert detector in _detected(value)


@pytest.mark.parametrize(
    "value",
    [
        # A pseudonym: hexadecimal digits never stand alone as a token.
        "ce9c6d057eac723659f0e7a63ff9b960bca0e6d1627ccb858477a7bdb3864b1c",
        # A card transaction reference: 16 digits without a card scheme prefix.
        "2022090100000001",
        # A card number without a valid checksum.
        "4111111111111112",
        "TRF000000001",
        "PAYMENTS_20220901.csv",
        "M0000123X",
        "Pharmacie Atlas Rabat",
        "Salma",
        "Salma Pharmacie Benjelloun",
        "2022-09-01T10:00:00",
        # A run id whose `e585` group alone reads as a CIN (found on a real export).
        "388765eb-e585-45e7-838b-0c0f60070926",
        None,
    ],
)
def test_ordinary_values_pass(value: str | None) -> None:
    assert _detected(value) == set()


def test_text_around_a_uuid_is_still_scanned() -> None:
    assert _detected("388765eb-e585-45e7-838b-0c0f60070926 CIN BE123456") == {"national_id"}


def test_exempted_matches_are_reported_apart_and_do_not_block() -> None:
    report = GateReport()
    table = pa.table({"merchant_id": ["M1234567", "M7654321"], "note": ["ok", "AB1234567"]})

    scan("bronze.card", table, NAMES, [Exemption("bronze.card", "merchant_id", "passport", "processor ids")], report)

    assert [(f.column, f.detector, f.rows) for f in report.exempted] == [("merchant_id", "passport", 2)]
    assert [(f.column, f.detector) for f in report.findings] == [("note", "passport")]
    assert not report.passed
    assert report.rows_scanned == 2
    assert report.columns_scanned == 2


def test_findings_show_masked_distinct_examples() -> None:
    report = GateReport()
    phones = ["+212612345678"] * 5 + ["+212700000000"]

    scan("t.t", pa.table({"c": phones}), NAMES, [], report)

    (finding,) = report.findings
    assert finding.rows == 6
    assert finding.examples == ("+2*********78", "+2*********00")


def test_only_string_columns_are_scanned() -> None:
    report = GateReport()

    scan("t.t", pa.table({"n": pa.array([612345678], pa.int64())}), NAMES, [], report)

    assert report.columns_scanned == 0
    assert report.passed


def test_names_normalize_case_accents_and_punctuation() -> None:
    assert normalize_names(pa.array(["  Benjelloun-Alami, É. ", None])).to_pylist() == ["benjelloun alami e", None]


def test_a_single_word_is_not_a_known_name() -> None:
    index = NameIndex(pa.array(["Salma", "Salma Benjelloun"]))

    assert len(index) == 1


def test_mask_keeps_shape_not_value() -> None:
    assert mask("AB1234") == "A****4"
    assert mask("1234") == "****"
