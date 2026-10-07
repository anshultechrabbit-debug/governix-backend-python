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
    ACROSS_VERSIONS = "across_versions"


_VERSION_REF = re.compile(r"\b(?:v|version\s*|edition\s*)(\d{1,3}(?:\.\d{1,3})?)\b", re.I)
# A comparison names at most this many versions ("v1, v2 and v3").
MAX_COMPARED_VERSIONS = 5
_COMPARE = re.compile(
    r"\b(compare|comparison|difference|differences|differ|change|changed|changes|what'?s new|what is new|"
    r"vs\.?|versus|introduced|added|removed|deleted|dropped|withdrawn|inserted)\b", re.I
)
# "Difference", "changes" and "compare" are about versions only when the question says so:
# "the unreconciled position difference" or "changes in address" are subjects of a rule.
_VERSION_CONTEXT = re.compile(
    r"\b(?:versions?|latest|previous|earlier|older|newer|current|revised|revision|amended|amendment|"
    r"updated|update|last\s+year|over\s+time|edition)\b",
    re.I,
)
# "... in each edition, and which edition sets the higher figure?": one subject, looked up in
# every version. The version in force alone can only ever answer for one of them.
_ACROSS = re.compile(
    # "all three versions", "each of the 3 versions", "all the versions", "every version".
    r"\b(?:each|every|both|all|either|the\s+two)\s+(?:of\s+)?(?:the\s+)?(?:(?:two|three|four|five|six|\d{1,2})\s+)?"
    r"(?:editions?|versions?)\b"
    r"|\bacross\s+(?:the\s+|all\s+)?(?:editions|versions)\b"
    r"|\bwhich\s+(?:edition|version)\s+(?:sets|has|gives|allows|requires|is|offers|charges)\s+(?:the\s+|a\s+)?"
    r"(?:higher|lower|larger|smaller|greater|stricter|longer|shorter|more|less|"
    r"highest|lowest|largest|smallest|greatest|longest|shortest|most|least|cheapest|"
    r"later|earlier|newer|older|latest|earliest|newest|oldest|more\s+recent)\b"
    # "In which versions is the maximum tenure 30 years?"
    r"|\bwhich\s+(?:editions|versions)\b",
    re.I,
)
# Words that ask for a comparison, or say which versions, rather than name what to compare.
COMPARISON_WORDS = frozenset(
    "compare compared comparing comparison difference differences differ differs different change changed "
    "changes changing new between versus latest previous earlier older newer current revised revision "
    "amended amendment amendments updated update updates over time year years two both same "
    # what happened to a rule ("introduced", "removed"), and how the policy moved ("became stricter")
    "introduced introduce added removed deleted dropped withdrawn inserted original originally "
    "became become becomes strict stricter strictness lenient looser loose tighter tightened relaxed evolved".split()
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
    mode: str  # current | as_of | versions | all
    as_of: date | None = None
    version_labels: list[str] = field(default_factory=list)
    version_ids: list[uuid.UUID] = field(default_factory=list)
    explanation: str = ""
    # A comparison with no subject ("what changed in v2?") is answered from the section diff.
    diff: bool = False

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
    if len(labels) >= 2:
        # "Compare the owner in v1 and v2", "Who is the owner in Version 1.0 and Version 2.0?":
        # every version named, each answered from its own text.
        return QueryPlan(QueryClass.COMPARISON, "versions", version_labels=labels[:MAX_COMPARED_VERSIONS],
                         explanation="Question compares versions " + ", ".join(labels[:MAX_COMPARED_VERSIONS]))
    if labels and _COMPARE.search(question):
        # "What changed in version 2.0?": that version and the one it is compared with.
        return QueryPlan(QueryClass.COMPARISON, "versions", version_labels=labels,
                         explanation=f"Question asks how version {labels[0]} differs from another version")
    if not labels and _ACROSS.search(question):
        return QueryPlan(QueryClass.ACROSS_VERSIONS, "all",
                         explanation="Question asks about every version; each passage is labelled with its version")
    if _COMPARE.search(question) and not labels and _VERSION_CONTEXT.search(question):
        return QueryPlan(QueryClass.COMPARISON, "versions",
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


def without_version_refs(text: str) -> str:
    """The question without "Version 1.0", "v2" or "edition 3": those choose the versions to
    search, and a passage seldom repeats its own version number."""
    return _VERSION_REF.sub(" ", text)


def version_mentions(text: str) -> list[str]:
    """The version references as written ("Version 1.0", "v2"), in order, without repeats."""
    return list(dict.fromkeys(m.group(0).strip() for m in _VERSION_REF.finditer(text)))


def normal_label(label: str) -> str:
    """One spelling per version label: "v1", "1", "1.0" and "01.00" are the same version,
    while "2.1" and "2.10" are not."""
    parts = label.strip().lower().removeprefix("version").strip().removeprefix("v").strip().split(".")
    while len(parts) > 1 and not parts[-1].strip("0"):
        parts.pop()
    return ".".join(part.lstrip("0") or "0" for part in parts)


def compares_versions(question: str) -> bool:
    """ "Has the owner changed between the versions?", "What changed in v2?", "... in each edition":
    words like "changed" and "between" then frame the question instead of naming its subject."""
    return bool(
        (_COMPARE.search(question) and (_VERSION_REF.search(question) or _VERSION_CONTEXT.search(question)))
        or _ACROSS.search(question)
    )
