from pathlib import Path

import pytest

from lakemig.assessment.classification import Policy
from lakemig.assessment.governance import PSEUDONYM_MASK, check, classify_models, policy_md, read_models

POLICY = Policy.from_dict(
    {
        "rules": [
            {"pattern": "full_name", "level": "direct", "reason": "names a person"},
            {"pattern": "account_number|account_id|city", "level": "indirect", "reason": "quasi-identifier"},
            {"pattern": "_.*|amount", "level": "none", "reason": "measure or lineage"},
        ]
    }
)
PSEUDONYMIZED = {"account_number"}
MASK_LINE = "        column_mask: {function: governance.mask_pseudonym}\n"
TAG_LINE = "        databricks_tags: {classification: indirect}\n"
ACCOUNT_ID = "      - name: account_id\n        data_type: bigint\n"
AMOUNT = "      - name: amount\n        data_type: decimal(22,2)\n"

ACCOUNTS = """
models:
  - name: account
    columns:
      - name: account_id
        data_type: bigint
        databricks_tags: {classification: indirect}
      - name: account_number
        data_type: string
        databricks_tags: {classification: indirect}
        column_mask: {function: governance.mask_pseudonym}
      - name: amount
        data_type: decimal(22,2)
      - name: _loaded_at
        data_type: timestamp_ntz
unit_tests:
  - name: ignored
    model: account
"""


def _project(tmp_path: Path, yml: str) -> Path:
    (tmp_path / "models" / "silver" / "accounts").mkdir(parents=True)
    (tmp_path / "models" / "silver" / "accounts" / "_accounts.yml").write_text(yml, encoding="utf-8")
    (tmp_path / "models" / "gold" / "rules").mkdir(parents=True)
    (tmp_path / "models" / "gold" / "rules" / "_rules.yml").write_text("unit_tests: []\n", encoding="utf-8")
    return tmp_path


def _problems(tmp_path: Path, yml: str) -> list[str]:
    models = read_models(_project(tmp_path, yml))
    return check(models, classify_models(models, POLICY), PSEUDONYMIZED)


def test_schema_is_the_first_directory_and_models_without_columns_are_skipped(tmp_path: Path) -> None:
    models = read_models(_project(tmp_path, ACCOUNTS))

    assert [m.relation for m in models] == ["silver.account"]
    assert models[0].columns[1].mask == PSEUDONYM_MASK
    assert dict(models[0].columns[0].tags) == {"classification": "indirect"}


def test_declared_tags_and_masks_match_the_policy(tmp_path: Path) -> None:
    assert _problems(tmp_path, ACCOUNTS) == []


@pytest.mark.parametrize(
    ("edit", "expected"),
    [
        (
            (MASK_LINE, ""),
            "silver.account.account_number: holds pseudonyms, needs column_mask",
        ),
        (
            (ACCOUNT_ID + TAG_LINE, ACCOUNT_ID),
            "silver.account.account_id: databricks_tags {}, the policy requires {'classification': 'indirect'}",
        ),
        (
            (AMOUNT, AMOUNT + TAG_LINE),
            "silver.account.amount: databricks_tags {'classification': 'indirect'}, the policy requires {}",
        ),
        (
            (AMOUNT, AMOUNT + MASK_LINE),
            "silver.account.amount: column_mask governance.mask_pseudonym on a column that holds no pseudonym",
        ),
        (
            ("      - name: amount\n", "      - name: full_name\n        data_type: string\n      - name: amount\n"),
            "silver.account.full_name: direct identifier in a cloud model",
        ),
        (
            ("      - name: amount\n", "      - name: iban\n        data_type: string\n      - name: amount\n"),
            "silver.account.iban: no classification rule matches",
        ),
    ],
    ids=["missing-mask", "missing-tag", "tag-on-unrelated", "mask-on-unrelated", "direct", "unclassified"],
)
def test_each_departure_from_the_policy_fails(tmp_path: Path, edit: tuple[str, str], expected: str) -> None:
    old, new = edit
    assert old in ACCOUNTS

    problems = _problems(tmp_path, ACCOUNTS.replace(old, new))

    assert len(problems) == 1
    assert expected in problems[0]


def test_policy_document_lists_the_columns_relating_to_a_person(tmp_path: Path) -> None:
    models = read_models(_project(tmp_path, ACCOUNTS))

    rendered = policy_md(models, classify_models(models, POLICY))

    assert "| 1 | 4 | 2 | 1 |" in rendered
    assert "| `silver.account` | `account_number` | indirect | `governance.mask_pseudonym` |" in rendered
    assert "`amount`" not in rendered
