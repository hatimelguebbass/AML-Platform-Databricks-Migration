"""The pinned commit of the source platform (`source/pin.toml`)."""

from __future__ import annotations

import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

PIN_FILE = Path("source/pin.toml")


@dataclass(frozen=True)
class SourcePin:
    repository: str
    commit: str

    @classmethod
    def read(cls, path: Path = PIN_FILE) -> SourcePin:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        return cls(repository=raw["repository"], commit=raw["commit"])

    def verify_checkout(self, checkout: Path) -> None:
        """Refuses to assess a working copy other than the pinned commit, or one with local changes."""
        head = _git(checkout, "rev-parse", "HEAD")
        if head != self.commit:
            raise SourceMismatchError(f"{checkout} is at {head}, the pin is {self.commit}")
        if _git(checkout, "status", "--porcelain", "--untracked-files=no"):
            raise SourceMismatchError(f"{checkout} has local changes")


class SourceMismatchError(Exception):
    pass


def _git(checkout: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(checkout), *args],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
