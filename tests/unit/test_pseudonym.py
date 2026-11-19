import os
from pathlib import Path

import pyarrow as pa
import pytest

from lakemig.export.pseudonym import Key, KeyFileError, pseudonym, pseudonymize

KEY = Key(bytes(range(32)))


def test_equal_values_give_equal_pseudonyms_and_nulls_stay_null() -> None:
    values = pa.chunked_array([["8000EBD30", None], ["8000EBD30", "80012ACA0"]])

    result = pseudonymize(values, KEY, "account_number").to_pylist()

    assert result[0] == result[2] == pseudonym("8000EBD30", KEY, "account_number")
    assert result[1] is None
    assert result[3] != result[0]
    assert all(len(v) == 64 for v in result if v is not None)


def test_namespaces_and_keys_separate_pseudonyms() -> None:
    other = Key(bytes(32))

    assert pseudonym("123", KEY, "account_number") != pseudonym("123", KEY, "card_token")
    assert pseudonym("123", KEY, "account_number") != pseudonym("123", other, "account_number")
    assert KEY.key_id != other.key_id


def test_key_identifier_and_repr_do_not_reveal_the_key() -> None:
    assert KEY.material.hex() not in repr(KEY)
    assert KEY.key_id not in KEY.material.hex()


def test_generated_key_is_private_and_never_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "secrets" / "pseudonymization.key"

    key = Key.generate(path)

    assert Key.load(path) == key
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(KeyFileError, match="exists"):
        Key.generate(path)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_a_key_readable_by_others_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "key"
    path.write_text("00" * 32)
    path.chmod(0o644)

    with pytest.raises(KeyFileError, match="chmod 600"):
        Key.load(path)


def test_a_short_key_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "key"
    path.write_text("00" * 16)
    path.chmod(0o600)

    with pytest.raises(KeyFileError, match="32 bytes"):
        Key.load(path)
