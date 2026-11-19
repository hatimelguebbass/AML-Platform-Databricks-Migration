"""The verdict on one slice of one table, from both sides' fingerprints and the rows of the differing buckets."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from lakemig.parity.fingerprint import Fingerprint, Layout, Row

# Rows named in the report per slice; the counts cover every row of the inspected buckets.
EXAMPLES = 10


@dataclass(frozen=True)
class ProfileMismatch:
    column: str
    metric: str
    source: str | None
    target: str | None


@dataclass(frozen=True)
class SliceResult:
    slice: str | None
    source_rows: int
    target_rows: int
    duplicate_keys: int = 0
    profile_mismatches: tuple[ProfileMismatch, ...] = ()
    buckets: int = 0
    buckets_differing: int = 0
    buckets_inspected: int = 0
    missing_in_target: int = 0
    unexpected_in_target: int = 0
    # Rows found on both sides with a column that differs beyond its tolerance, by column.
    different: Mapping[str, int] = field(default_factory=dict)
    # Rows whose only differences are within the columns' tolerances, by column.
    within_tolerance: Mapping[str, int] = field(default_factory=dict)
    examples: tuple[str, ...] = ()

    @property
    def equal(self) -> bool:
        """No difference beyond a written tolerance; a difference left uninspected counts as one."""
        return (
            self.source_rows == self.target_rows
            and not self.duplicate_keys
            and not self.profile_mismatches
            and self.buckets_inspected == self.buckets_differing
            and not self.missing_in_target
            and not self.unexpected_in_target
            and not self.different
        )


def profile_mismatches(source: Fingerprint, target: Fingerprint, layout: Layout) -> list[ProfileMismatch]:
    mismatches = []
    for column in layout.columns:
        left, right = source.columns[column], target.columns[column]
        for metric in ("nulls", "distinct", "total", "low", "high"):
            a, b = getattr(left, metric), getattr(right, metric)
            if a != b:
                mismatches.append(ProfileMismatch(column, metric, _text(a), _text(b)))
    return mismatches


def _text(value: object) -> str | None:
    return None if value is None else str(value)


def differing_buckets(source: Mapping[str, tuple[int, int]], target: Mapping[str, tuple[int, int]]) -> list[str]:
    return sorted(b for b in set(source) | set(target) if source.get(b) != target.get(b))


@dataclass(frozen=True)
class RowDifferences:
    missing: list[str]
    unexpected: list[str]
    different: Counter[str]
    within_tolerance: Counter[str]
    changed: list[str]


def compare_rows(source: Mapping[str, Row], target: Mapping[str, Row], layout: Layout) -> RowDifferences:
    """Rows of the inspected buckets, matched by key; a changed row is judged column by column."""
    missing = sorted(set(source) - set(target))
    unexpected = sorted(set(target) - set(source))
    different: Counter[str] = Counter()
    within: Counter[str] = Counter()
    changed = []
    for key in sorted(set(source) & set(target)):
        left, right = source[key], target[key]
        if left.digest == right.digest:
            continue
        beyond = []
        for index, column in enumerate(layout.columns):
            a, b = left.values[index], right.values[index]
            if a == b:
                continue
            if column in layout.tolerance and _within(a, b, layout.tolerance[column]):
                within[column] += 1
            else:
                beyond.append(column)
        different.update(beyond)
        if beyond:
            changed.append(key)
    return RowDifferences(missing, unexpected, different, within, changed)


def _within(a: str | None, b: str | None, tolerance: float) -> bool:
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) <= tolerance


def judge(
    slice_value: str | None,
    source: Fingerprint,
    target: Fingerprint,
    layout: Layout,
    inspected: Sequence[str],
    rows: RowDifferences | None,
) -> SliceResult:
    differing = differing_buckets(source.buckets, target.buckets)
    duplicates = (source.rows - source.keys) + (target.rows - target.keys)
    examples: list[str] = []
    if rows is not None:
        for label, keys in (("missing", rows.missing), ("unexpected", rows.unexpected), ("changed", rows.changed)):
            examples += [f"{label}: {_readable(k)}" for k in keys[: EXAMPLES - len(examples)]]
    return SliceResult(
        slice=slice_value,
        source_rows=source.rows,
        target_rows=target.rows,
        duplicate_keys=duplicates,
        profile_mismatches=tuple(profile_mismatches(source, target, layout)),
        buckets=len(set(source.buckets) | set(target.buckets)),
        buckets_differing=len(differing),
        buckets_inspected=len(inspected),
        missing_in_target=len(rows.missing) if rows else 0,
        unexpected_in_target=len(rows.unexpected) if rows else 0,
        different=dict(sorted(rows.different.items())) if rows else {},
        within_tolerance=dict(sorted(rows.within_tolerance.items())) if rows else {},
        examples=tuple(examples),
    )


def _readable(key: str) -> str:
    return " | ".join(key.split("\x1f"))
