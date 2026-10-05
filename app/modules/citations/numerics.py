"""Numeric fact extraction and normalisation.

Used for deterministic version diffs ("LTV 80% -> 75%") and for validating
that every number in an AI answer appears in the cited evidence. Values are
normalised so "Rs. 75 lakh", "₹75,00,000" and "INR 7.5 million" compare equal.
"""

import re
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.modules.ingestion.analysis.dates import DATE_FRAGMENT, parse_date
from app.modules.ingestion.text import sentence_bounds

MULTIPLIERS = {
    "thousand": Decimal(1_000), "k": Decimal(1_000),
    "lakh": Decimal(100_000), "lakhs": Decimal(100_000), "lac": Decimal(100_000), "lacs": Decimal(100_000),
    "million": Decimal(1_000_000), "mn": Decimal(1_000_000),
    "crore": Decimal(10_000_000), "crores": Decimal(10_000_000), "cr": Decimal(10_000_000),
    "billion": Decimal(1_000_000_000), "bn": Decimal(1_000_000_000),
}
DURATION_UNITS = {
    "year": "year", "years": "year", "yr": "year", "yrs": "year",
    "month": "month", "months": "month",
    "day": "day", "days": "day",
    "week": "week", "weeks": "week",
    "bps": "bps", "basis points": "bps", "basis point": "bps",
}
_NUM = r"\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_MULT = r"(?:thousand|lakhs?|lacs?|million|mn|crores?|cr|billion|bn|k)\b"

PATTERNS = [
    ("date", re.compile(DATE_FRAGMENT, re.I)),
    ("amount", re.compile(
        # "Rs" starts a word: "years 5" and "Officers 3" are not rupee amounts.
        rf"(?:₹|\b(?:rs\.?|inr|rupees))\s*(?P<num>{_NUM})\s*(?P<mult>{_MULT})?"
        rf"|(?P<num2>{_NUM})\s*(?P<mult2>{_MULT})?\s*(?:rupees|inr)\b",
        re.I,
    )),
    ("percent", re.compile(rf"(?P<num>{_NUM})\s*(?:%|per\s*cent\b|percent\b|p\.?a\.?\s*%)", re.I)),
    ("duration", re.compile(
        rf"(?P<num>{_NUM})\s*(?P<unit>basis\s+points?|bps|years?|yrs?|months?|days?|weeks?)\b", re.I
    )),
    ("quantity", re.compile(rf"(?P<num>{_NUM})\s*(?P<mult>{_MULT})", re.I)),
    # Not from inside a grouped figure: "00,000" of "4,00,000" is not a number of its own.
    ("number", re.compile(rf"(?<![\w.])(?<!\d,)(?P<num>{_NUM})(?![\w%])")),
    # A grouped figure glued to a letter: PDF fonts often draw "₹" as "I" or "`" ("I3,50,000").
    ("number", re.compile(r"(?<=[A-Za-z`])(?P<num>\d{1,3}(?:,\d{2,3})+(?:\.\d+)?)(?![\w%])")),
]


@dataclass(frozen=True)
class NumericFact:
    kind: str
    value: str  # canonical, comparable string
    raw: str
    start: int
    end: int

    @property
    def key(self) -> tuple[str, str]:
        return self.kind, self.value


def _decimal(raw: str) -> Decimal | None:
    try:
        return Decimal(raw.replace(",", ""))
    except InvalidOperation:
        return None


def _canonical(value: Decimal) -> str:
    normalized = value.normalize()
    text = format(normalized, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def extract_numeric_facts(text: str, date_order: str = "DMY") -> list[NumericFact]:
    """All numeric facts, most specific interpretation first; spans never overlap."""
    taken: list[tuple[int, int]] = []
    facts: list[NumericFact] = []
    # Scans set figures off with hyphens ("a minimum period of -7- days"); blank them out
    # without moving any offset, so "7 days" is read as a duration.
    text = re.sub(r"(?<![\w-])-(\d+)-(?=\s)", lambda m: f" {m.group(1)} ", text)

    def free(start: int, end: int) -> bool:
        return all(end <= s or start >= e for s, e in taken)

    for kind, pattern in PATTERNS:
        for match in pattern.finditer(text):
            start, end = match.span()
            if not free(start, end):
                continue
            value = _normalize(kind, match, date_order)
            if value is None:
                continue
            taken.append((start, end))
            facts.append(NumericFact(kind, value, match.group(0).strip(), start, end))
    facts.sort(key=lambda f: f.start)
    return facts


def _normalize(kind: str, match: re.Match, date_order: str) -> str | None:
    groups = match.groupdict()
    if kind == "date":
        parsed = parse_date(match.group(0), date_order)
        return parsed.isoformat() if parsed else None
    number = _decimal(groups.get("num") or groups.get("num2") or "")
    if number is None:
        return None
    if kind in ("amount", "quantity"):
        multiplier = (groups.get("mult") or groups.get("mult2") or "").lower()
        return _canonical(number * MULTIPLIERS.get(multiplier, Decimal(1)))
    if kind == "duration":
        unit = DURATION_UNITS.get(" ".join(groups["unit"].lower().split()), groups["unit"].lower())
        return f"{_canonical(number)} {unit}"
    return _canonical(number)


def sentence_at(text: str, start: int, end: int, limit: int = 300) -> str:
    left, right = sentence_bounds(text, start, end)
    return " ".join(text[left:right].split())[:limit]


def numeric_changes(old_text: str, new_text: str) -> dict[str, list[dict]]:
    """Pair numeric facts that changed between two texts (by kind, in reading order)."""
    old_facts = [f for f in extract_numeric_facts(old_text) if f.kind != "number"]
    new_facts = [f for f in extract_numeric_facts(new_text) if f.kind != "number"]
    old_counts, new_counts = Counter(f.key for f in old_facts), Counter(f.key for f in new_facts)
    removed = _unmatched(old_facts, new_counts)
    added = _unmatched(new_facts, old_counts)

    changed, still_removed, still_added = [], [], list(added)
    for old in removed:
        partner = next((n for n in still_added if n.kind == old.kind), None)
        if partner is None:
            still_removed.append(old)
            continue
        still_added.remove(partner)
        changed.append({
            "kind": old.kind,
            "old": old.raw,
            "new": partner.raw,
            "old_value": old.value,
            "new_value": partner.value,
            "old_context": sentence_at(old_text, old.start, old.end),
            "new_context": sentence_at(new_text, partner.start, partner.end),
        })

    def describe(facts, text):
        return [{"kind": f.kind, "value": f.raw, "context": sentence_at(text, f.start, f.end)} for f in facts]

    return {"changed": changed, "added": describe(still_added, new_text), "removed": describe(still_removed, old_text)}


def _unmatched(facts: list[NumericFact], other_counts: Counter) -> list[NumericFact]:
    remaining = Counter(other_counts)
    result = []
    for fact in facts:
        if remaining[fact.key] > 0:
            remaining[fact.key] -= 1
        else:
            result.append(fact)
    return result
