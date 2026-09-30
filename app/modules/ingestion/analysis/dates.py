"""Deterministic date parsing for banking documents.

Numeric dates are ambiguous (01/07/2026); Indian and UK documents use
day-first order, so DMY is the default and configurable.
"""

import re
from datetime import date

MONTHS = {
    name: index
    for index, names in enumerate(
        [
            ("january", "jan"), ("february", "feb"), ("march", "mar"), ("april", "apr"),
            ("may",), ("june", "jun"), ("july", "jul"), ("august", "aug"),
            ("september", "sep", "sept"), ("october", "oct"), ("november", "nov"),
            ("december", "dec"),
        ],
        start=1,
    )
    for name in names
}
_MONTH = r"(?P<month>" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\.?"
_DAY = r"(?P<day>\d{1,2})(?:st|nd|rd|th)?"
_YEAR = r"(?P<year>\d{4})"

PATTERNS = [
    re.compile(rf"\b{_YEAR}-(?P<m>\d{{1,2}})-(?P<d>\d{{1,2}})\b"),  # ISO
    re.compile(rf"\b(?P<a>\d{{1,2}})[/.\-](?P<b>\d{{1,2}})[/.\-]{_YEAR}\b"),  # 01/07/2026
    re.compile(rf"\b{_DAY}[\s\-]+(?:of\s+)?{_MONTH}[\s,\-]+{_YEAR}\b", re.I),  # 1st July, 2026
    re.compile(rf"\b{_MONTH}[\s\-]+{_DAY}[\s,]+{_YEAR}\b", re.I),  # July 1, 2026
]
# Any date-looking fragment, for locating dates in text.
DATE_FRAGMENT = (
    r"(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[/.\-]\d{1,2}[/.\-]\d{4}"
    rf"|\d{{1,2}}(?:st|nd|rd|th)?[\s\-]+(?:of\s+)?(?:{'|'.join(MONTHS)})\.?[\s,\-]+\d{{4}}"
    rf"|(?:{'|'.join(MONTHS)})\.?[\s\-]+\d{{1,2}}(?:st|nd|rd|th)?[\s,]+\d{{4}})"
)
MONTH_YEAR = re.compile(rf"\b{_MONTH}[\s,\-]+{_YEAR}\b", re.I)


def parse_date(text: str, order: str = "DMY") -> date | None:
    """Parse the first complete date in `text`."""
    best: tuple[int, date] | None = None
    for pattern in PATTERNS:
        for match in pattern.finditer(text):
            parsed = _build(match, order)
            if parsed and (best is None or match.start() < best[0]):
                best = (match.start(), parsed)
            if parsed:
                break
    return best[1] if best else None


def parse_month_year(text: str) -> tuple[int, int] | None:
    """'March 2025' -> (2025, 3). Used for historical questions."""
    match = MONTH_YEAR.search(text)
    if not match:
        return None
    return int(match["year"]), MONTHS[match["month"].lower()]


def _build(match: re.Match, order: str) -> date | None:
    groups = match.groupdict()
    try:
        year = int(groups["year"])
        if groups.get("m"):
            month, day = int(groups["m"]), int(groups["d"])
        elif groups.get("a"):
            first, second = int(groups["a"]), int(groups["b"])
            if order == "MDY":
                month, day = first, second
            else:
                day, month = first, second
            if month > 12 and day <= 12:  # unambiguous the other way round
                day, month = month, day
        else:
            month, day = MONTHS[groups["month"].lower()], int(groups["day"])
        if not 1900 <= year <= 2200:
            return None
        return date(year, month, day)
    except (ValueError, KeyError):
        return None
