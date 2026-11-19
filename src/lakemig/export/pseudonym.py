"""Keyed pseudonyms for identifiers that mean something outside the bank (account and card numbers).

HMAC-SHA256 under a key derived per namespace from one master key. The master key is generated on
premises and never leaves: without it, a pseudonym cannot be reversed or recomputed from a guessed
account number, which a plain hash would allow (account numbers are short and structured). Equal
values of one namespace give equal pseudonyms in every table and every delivery, so the cloud joins
on them exactly as the source joins on the clear values. Rotating the key changes every pseudonym:
the manifest carries a key identifier so the cloud can refuse a delivery made with another key.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc

ALGORITHM = "HMAC-SHA256"
KEY_BYTES = 32
_DERIVATION = b"lakemig/pseudonym/v1/"


class KeyFileError(ValueError):
    pass


@dataclass(frozen=True)
class Key:
    material: bytes

    def __repr__(self) -> str:
        return f"Key(id={self.key_id})"

    @property
    def key_id(self) -> str:
        """Identifies the key in manifests; reveals nothing about it."""
        return hmac.new(self.material, b"lakemig/key-id", hashlib.sha256).hexdigest()[:16]

    def namespace(self, name: str) -> bytes:
        return hmac.new(self.material, _DERIVATION + name.encode(), hashlib.sha256).digest()

    @classmethod
    def generate(cls, path: Path) -> Key:
        if path.exists():
            raise KeyFileError(f"{path} exists: a new key would change every pseudonym already delivered")
        path.parent.mkdir(parents=True, exist_ok=True)
        material = secrets.token_bytes(KEY_BYTES)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(material.hex() + "\n")
        return cls(material)

    @classmethod
    def load(cls, path: Path) -> Key:
        if os.name == "posix" and path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise KeyFileError(f"{path} is readable by other users: chmod 600 it")
        try:
            material = bytes.fromhex(path.read_text(encoding="ascii").strip())
        except ValueError as error:
            raise KeyFileError(f"{path}: not a hexadecimal key") from error
        if len(material) != KEY_BYTES:
            raise KeyFileError(f"{path}: expected {KEY_BYTES} bytes, got {len(material)}")
        return cls(material)


def pseudonymize(values: pa.Array | pa.ChunkedArray, key: Key, namespace: str) -> pa.Array:
    """Pseudonym of each value as 64 lowercase hex characters; nulls stay null.

    Each distinct value is hashed once: a day of transfers repeats the same accounts many times.
    """
    if isinstance(values, pa.ChunkedArray):
        values = values.combine_chunks()
    encoded = pc.dictionary_encode(values.cast(pa.string()))
    namespace_key = key.namespace(namespace)
    digests = pa.array(
        [hmac.new(namespace_key, v.encode(), hashlib.sha256).hexdigest() for v in encoded.dictionary.to_pylist()],
        type=pa.string(),
    )
    return pc.take(digests, encoded.indices)


def pseudonym(value: str, key: Key, namespace: str) -> str:
    """Pseudonym of one value, for the parity checks that compare with the source on premises."""
    return hmac.new(key.namespace(namespace), value.encode(), hashlib.sha256).hexdigest()
