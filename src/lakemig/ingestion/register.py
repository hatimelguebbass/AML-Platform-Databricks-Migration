"""Which deliveries of the landing volume to ingest, given the delivery log.

The unit of ingestion is a delivery, not a file: the files of a delivery only count once its
manifest seals them, and a business date is ingested from one delivery at a time. For each date the
most recent sealed delivery (by `exported_at`) is the candidate: a correction exported later wins,
a stale one uploaded late does not. The log decides what is done: a delivery logged as accepted or
rejected is not looked at again unless an operator asks for a retry; older undecided deliveries of
the date are logged as superseded so that the log accounts for every sealed delivery.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from pathlib import Path

from lakemig.manifest import MANIFEST, Manifest


class Status(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


@dataclass(frozen=True)
class Sealed:
    """A delivery directory with its manifest; `manifest` is None when the manifest cannot be read."""

    directory: Path
    manifest: Manifest | None
    error: str | None = None

    @property
    def delivery_id(self) -> str:
        return self.directory.name

    @property
    def business_date(self) -> str:
        return self.directory.parent.name

    @property
    def exported_at(self) -> str:
        # An unreadable manifest sorts first: it never displaces a readable delivery of its date.
        return self.manifest.exported_at if self.manifest else ""


@dataclass(frozen=True)
class Step:
    business_date: str
    ingest: Sealed | None
    superseded: tuple[Sealed, ...] = field(default=())


def scan(root: Path) -> list[Sealed]:
    """Every sealed delivery under `root` (<business_date>/<delivery_id>/_manifest.json)."""
    sealed = []
    for path in sorted(root.glob(f"*/*/{MANIFEST}")):
        directory = path.parent
        if directory.name.startswith(".") or not _is_date(directory.parent.name):
            continue
        try:
            sealed.append(Sealed(directory, Manifest.from_json(path.read_text(encoding="utf-8"))))
        except (ValueError, KeyError, TypeError) as error:
            # json.JSONDecodeError is a ValueError; a manifest missing fields raises KeyError or TypeError.
            sealed.append(Sealed(directory, None, f"unreadable manifest: {type(error).__name__}: {error}"))
    return sealed


def _is_date(name: str) -> bool:
    try:
        date.fromisoformat(name)
    except ValueError:
        return False
    return len(name) == 10


def plan(sealed: Iterable[Sealed], decided: Mapping[str, Status], retry: Collection[str] = ()) -> list[Step]:
    """Steps in business date order; only dates with something to ingest or to log appear."""
    by_date: dict[str, list[Sealed]] = defaultdict(list)
    for delivery in sealed:
        by_date[delivery.business_date].append(delivery)
    steps = []
    for business_date in sorted(by_date):
        deliveries = sorted(by_date[business_date], key=lambda d: (d.exported_at, d.delivery_id))
        latest = deliveries[-1]
        undecided = [d for d in deliveries if d.delivery_id not in decided or d.delivery_id in retry]
        ingest = latest if latest in undecided else None
        superseded = tuple(d for d in undecided if d is not latest)
        if ingest or superseded:
            steps.append(Step(business_date, ingest, superseded))
    return steps


def refused_under(sealed: Iterable[Sealed], decided: Mapping[str, Status], key_id: str) -> set[str]:
    """Deliveries under `key_id` that an ordinary run refused: a rotation to that key decides them again.

    The new key's deliveries usually land before the rotation, and an ingestion in between rejects
    every one of them for their key, the very reason the rotation exists.
    """
    return {
        d.delivery_id
        for d in sealed
        if d.manifest is not None
        and d.manifest.pseudonymization.get("key_id") == key_id
        and decided.get(d.delivery_id) is Status.REJECTED
    }


def rekey_problems(steps: Iterable[Step], accepted_dates: Collection[str], key_id: str) -> list[str]:
    """Why a rotation to `key_id` cannot proceed: it must re-key every accepted date in one run.

    Pseudonyms of two keys never join, so a rotation that stopped half-way would split each
    account across the dates of the old and the new key.
    """
    planned = {s.business_date: s.ingest for s in steps if s.ingest is not None}
    problems = [
        f"{day}: no new delivery under key {key_id} to replace the accepted one"
        for day in sorted(set(accepted_dates) - set(planned))
    ]
    for day, delivery in sorted(planned.items()):
        found = delivery.manifest.pseudonymization.get("key_id") if delivery.manifest else None
        if found != key_id:
            problems.append(f"{day}: delivery {delivery.delivery_id} is under key {found}, not {key_id}")
    if not planned:
        problems.append(f"no delivery under key {key_id} to ingest")
    return problems


def manifest_summary(manifest: Manifest) -> str:
    """The manifest without its column lists and totals, for the log."""
    return json.dumps(
        {
            "source": manifest.source,
            "policy_digest": manifest.policy_digest,
            "tables": {t.name: t.rows for t in manifest.tables},
        },
        sort_keys=True,
    )
