"""Deterministic query routing: which versions must answer this question?

An explicit UI choice (Current / Historical / Specific version / Compare) always
wins; otherwise the question's wording decides. A historical question is never
answered from the current version, and vice versa.
"""

import calendar
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum

from app.modules.ingestion.analysis.dates import parse_date, parse_month_year


class QueryClass(StrEnum):
    CURRENT = "current"
    HISTORICAL = "historical"
    SPECIFIC_VERSION = "specific_version"
    COMPARISON = "comparison"


_VERSION_REF = re.compile(r"\b(?:v|version\s*)(\d{1,3}(?:\.\d{1,3})?)\b", re.I)
_COMPARE = re.compile(
    r"\b(compare|comparison|difference|differences|differ|changed|changes|what'?s new|what is new|vs\.?|versus)\b", re.I
)
_PAST = re.compile(r"\b(was|were|used to|previously|earlier|before|prior to|as of|as on|at that time|back in)\b", re.I)
_YEAR = re.compile(r"\b(?:in|during|for)\s+((?:19|20)\d{2})\b", re.I)
# A year range ("during 2022-27", "2017-22", "FY 2022-23") names a period the
# document *talks about*, not the date the answer must be in force.  Treating it
# as an as-of date silently filters out every version and yields NO_RELEVANT_DOCUMENTS.
_YEAR_RANGE = re.compile(
    r"\b(?:19|20)\d{2}\s*(?:[-–—]|\bto\b|\bthrough\b)\s*(?:\d{2}|\d{4})\b|\bFY\s*(?:19|20)\d{2}\s*[-–—]\s*\d{2}\b",
    re.I,
)


@dataclass
class QueryPlan:
    query_class: QueryClass
    mode: str  # current | as_of | versions
    as_of: date | None = None
    version_labels: list[str] = field(default_factory=list)
    version_ids: list[uuid.UUID] = field(default_factory=list)
    explanation: str = ""

    def describe(self) -> dict:
        return {
            "query_class": self.query_class,
            "mode": self.mode,
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "version_labels": self.version_labels,
            "version_ids": [str(v) for v in self.version_ids],
            "explanation": self.explanation,
        }


def plan_query(
    question: str,
    *,
    ui_mode: str | None = None,
    as_of: date | None = None,
    version_ids: list[uuid.UUID] | None = None,
    date_order: str = "DMY",
    today: date | None = None,
) -> QueryPlan:
    today = today or datetime.now(UTC).date()
    version_ids = version_ids or []

    if ui_mode == "compare" and len(version_ids) == 2:
        return QueryPlan(QueryClass.COMPARISON, "versions", version_ids=version_ids, explanation="Comparison selected")
    if ui_mode == "version" and version_ids:
        return QueryPlan(QueryClass.SPECIFIC_VERSION, "versions", version_ids=version_ids, explanation="Version selected")
    if ui_mode == "historical" and as_of:
        return QueryPlan(QueryClass.HISTORICAL, "as_of", as_of=as_of, explanation=f"As in force on {as_of.isoformat()}")
    if ui_mode == "current":
        return QueryPlan(QueryClass.CURRENT, "as_of", as_of=today, explanation="Current version selected")

    labels = list(dict.fromkeys(_VERSION_REF.findall(question)))
    if _COMPARE.search(question) and (len(labels) >= 2 or not labels):
        return QueryPlan(QueryClass.COMPARISON, "versions", version_labels=labels[:2],
                         explanation="Question asks what changed between versions")
    if labels:
        return QueryPlan(QueryClass.SPECIFIC_VERSION, "versions", version_labels=labels[:1],
                         explanation=f"Question names version {labels[0]}")

    detected = _date_in_question(question, date_order)
    if detected is not None:
        state = "Historical" if detected < today else "Current"
        return QueryPlan(
            QueryClass.HISTORICAL if detected < today else QueryClass.CURRENT, "as_of", as_of=detected,
            explanation=f"{state}: as in force on {detected.isoformat()}",
        )
    if _PAST.search(question):
        # Past tense without a date is ambiguous: answer from the current version, and say so.
        return QueryPlan(QueryClass.CURRENT, "as_of", as_of=today,
                         explanation="No date given; using the version currently in force")
    return QueryPlan(QueryClass.CURRENT, "as_of", as_of=today, explanation="Using the version currently in force")


def _date_in_question(question: str, date_order: str) -> date | None:
    if parsed := parse_date(question, date_order):
        return parsed
    if month_year := parse_month_year(question):
        year, month = month_year
        return date(year, month, calendar.monthrange(year, month)[1])  # in force at month end
    # A bare year only scopes the version when it stands alone; "during 2022-27"
    # and "FY 2022-23" describe a period covered by the document instead.
    if _YEAR_RANGE.search(question):
        return None
    if match := _YEAR.search(question):
        return date(int(match[1]), 12, 31)
    return None
