"""PII gate: the last check before a delivery is written, on the values that would cross.

The export policy already keeps every `direct` column on premises; the gate catches what a column
list cannot: a direct identifier inside a column that is allowed to cross, such as a phone number
typed into a free-text field, or a customer's name in a merchant name. Every string column of every
table is scanned after the policy's treatments. One unexempted match blocks the whole delivery.

Exemptions name one column and one detector, with the reviewed reason the match is not personal
data; their matches are counted in the manifest, never hidden. Findings show masked examples only:
the gate report is read by people who must not see the values either.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

import pyarrow as pa
import pyarrow.compute as pc

from lakemig.export.policy import Exemption

# RE2 has no look-behind: a token boundary is the start or end of the value, or any character that
# is neither a letter nor a digit. It keeps a 10-digit run inside a longer reference from matching.
_START = r"(?:^|[^\pL\pN])"
_END = r"(?:$|[^\pL\pN])"
EXAMPLES = 3
# A value that is a UUID and nothing else carries no identifier; its hex groups read as a CIN often
# enough (`...-e585-...`) to block a delivery on a run id. Anything around the UUID is still scanned.
_UUID = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"


@dataclass(frozen=True)
class Detector:
    name: str
    description: str
    pattern: str
    # Second opinion on a matching value, for patterns too broad on their own (checksums, prefixes).
    confirm: Callable[[str], bool] | None = None


def _luhn(digits: str) -> bool:
    total = 0
    for position, char in enumerate(reversed(digits)):
        value = int(char)
        if position % 2:
            value = value * 2 - 9 if value > 4 else value * 2
        total += value
    return total % 10 == 0


# Issuer prefixes of the schemes accepted in Morocco (Visa, Mastercard, American Express, Discover, CMI).
_CARD = re.compile(r"(?<![0-9A-Za-z])\d(?:[ -]?\d){12,18}(?![0-9A-Za-z])")
_ISSUER = re.compile(r"4|5[1-5]|2(?:2[2-9]|[3-6]|7[01]|720)|3[47]|6(?:011|5)")


def _is_card_number(value: str) -> bool:
    for match in _CARD.finditer(value):
        digits = re.sub(r"[ -]", "", match.group())
        if _ISSUER.match(digits) and _luhn(digits):
            return True
    return False


DETECTORS: tuple[Detector, ...] = (
    Detector("email", "E-mail address", r"[a-z0-9._%+-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}"),
    Detector(
        "phone",
        "Moroccan phone number, national or international form",
        _START + r"(?:(?:\+|00)212[ .-]?|0)[5-8](?:[ .-]?\d){8}" + _END,
    ),
    Detector(
        "bank_account",
        "Moroccan RIB (24 digits) or IBAN (MA and 26 digits)",
        _START + r"(?:ma\d{2} ?)?\d(?: ?\d){23}" + _END,
    ),
    Detector(
        "national_id",
        "Moroccan national identity card number (CIN): one or two letters and up to six digits",
        _START + r"[a-z]{1,2}[ -]?\d{3,6}" + _END,
    ),
    Detector(
        "passport",
        "Passport number: one or two letters and seven or eight digits",
        _START + r"[a-z]{1,2}\d{7,8}" + _END,
    ),
    Detector(
        "card_number",
        "Payment card number: 13 to 19 digits with a card scheme prefix and a valid Luhn checksum",
        _START + r"\d(?:[ -]?\d){12,18}" + _END,
        _is_card_number,
    ),
)
PERSON_NAME = "person_name"
DETECTOR_NAMES: tuple[str, ...] = (*(d.name for d in DETECTORS), PERSON_NAME)


def normalize_names(values: pa.Array | pa.ChunkedArray) -> pa.Array | pa.ChunkedArray:
    """Lower case, accents and punctuation removed, single spaces: `Benjelloun-Alami, É.` -> `benjelloun alami e`."""
    decomposed = pc.utf8_normalize(values.cast(pa.string()), form="NFKD")
    unaccented = pc.replace_substring_regex(decomposed, pattern=r"\p{Mn}+", replacement="")
    spaced = pc.replace_substring_regex(pc.utf8_lower(unaccented), pattern=r"[^\pL\pN]+", replacement=" ")
    return pc.utf8_trim_whitespace(spaced)


class NameIndex:
    """Full names of the bank's customers, from the identity table that stays on premises.

    A value matches when, once normalized, it holds a known full name as whole consecutive words:
    `P2P SALMA BENJELLOUN` matches `Salma Benjelloun`. A single first or last name is too common a
    word (a street, a merchant, a city) to block a delivery on, and is not indexed.
    """

    def __init__(self, names: pa.Array | pa.ChunkedArray) -> None:
        normalized = normalize_names(names)
        self._names = pc.unique(pc.filter(normalized, pc.match_substring(normalized, " ")))
        self._known = frozenset(str(n) for n in self._names.to_pylist())
        self._sizes = sorted({n.count(" ") + 1 for n in self._known})

    def __len__(self) -> int:
        return len(self._names)

    @classmethod
    def from_table(cls, table: pa.Table, groups: Sequence[Sequence[str]]) -> NameIndex:
        names: list[pa.Array | pa.ChunkedArray] = []
        for group in groups:
            parts = [table.column(c).cast(pa.string()) for c in group]
            names.append(pc.binary_join_element_wise(*parts, " "))
            if len(parts) > 1:
                names.append(pc.binary_join_element_wise(*reversed(parts), " "))
        return cls(pa.chunked_array(names).combine_chunks().drop_null())

    def matches(self, values: pa.Array | pa.ChunkedArray) -> pa.Array | pa.ChunkedArray:
        normalized = normalize_names(values)
        # Distinct values only: descriptors and codes repeat across a day's rows.
        holding = [v for v in pc.unique(normalized).to_pylist() if v and self._holds_name(v)]
        return pc.fill_null(pc.is_in(normalized, value_set=pa.array(holding, pa.string())), False)

    def _holds_name(self, value: str) -> bool:
        words = value.split(" ")
        return any(
            " ".join(words[start : start + size]) in self._known
            for size in self._sizes
            for start in range(len(words) - size + 1)
        )


@dataclass(frozen=True)
class Finding:
    relation: str
    column: str
    detector: str
    rows: int
    examples: tuple[str, ...]


@dataclass
class GateReport:
    rows_scanned: int = 0
    columns_scanned: int = 0
    findings: list[Finding] = field(default_factory=list)
    exempted: list[Finding] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.findings


def mask(value: str) -> str:
    """Keeps the shape of a value for a reviewer, not the value: `+212612345678` -> `+2********78`."""
    if len(value) <= 4:
        return "*" * len(value)
    keep = 2 if len(value) >= 8 else 1
    return value[:keep] + "*" * (len(value) - 2 * keep) + value[-keep:]


def scan(relation: str, table: pa.Table, names: NameIndex, exemptions: Iterable[Exemption], report: GateReport) -> None:
    exempt = {(e.column, e.detector) for e in exemptions if e.relation == relation}
    report.rows_scanned += table.num_rows
    for column in table.column_names:
        values = table.column(column)
        if not (pa.types.is_string(values.type) or pa.types.is_large_string(values.type)):
            continue
        report.columns_scanned += 1
        for detector, hits in _detect(values, names):
            if not hits:
                continue
            examples = tuple(mask(v) for v in list(dict.fromkeys(hits))[:EXAMPLES])
            finding = Finding(relation, column, detector, len(hits), examples)
            (report.exempted if (column, detector) in exempt else report.findings).append(finding)


def _detect(values: pa.Array | pa.ChunkedArray, names: NameIndex) -> Iterable[tuple[str, list[str]]]:
    values = pc.filter(
        values, pc.invert(pc.fill_null(pc.match_substring_regex(values, _UUID, ignore_case=True), False))
    )
    for detector in DETECTORS:
        found = pc.match_substring_regex(values, detector.pattern, ignore_case=True)
        matched = pc.filter(values, pc.fill_null(found, False))
        hits = [v for v in matched.to_pylist() if detector.confirm is None or detector.confirm(v)]
        yield detector.name, hits
    yield PERSON_NAME, pc.filter(values, names.matches(values)).to_pylist()
