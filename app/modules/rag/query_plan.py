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

from app.modules.ingestion.analysis.dates import (
    DATE_FRAGMENT, MONTH_YEAR, MONTHS, parse_date, parse_dates, parse_month_year,
)


class QueryClass(StrEnum):
    CURRENT = "current"
    HISTORICAL = "historical"
    SPECIFIC_VERSION = "specific_version"
    COMPARISON = "comparison"
    ACROSS_VERSIONS = "across_versions"


# "v2", "Version 6", "Version: 6", "edition 3".
_VERSION_REF = re.compile(r"\b(?:v|version\s*:?\s*|edition\s*:?\s*)(\d{1,3}(?:\.\d{1,3})?)\b", re.I)
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
    r"updated|update|last\s+year|over\s+time|edition|recently|most\s+recent)\b",
    re.I,
)
# "Has the late payment penalty increased?", "What was the lowest rate ever offered?", "Is the tenure shorter now
# than before?", "Compare the fee then and now": how something moved across the versions. The version in
# force alone says only where it is now.
_OVER_TIME = re.compile(
    r"\bover\s+(?:time|the\s+years|the\s+versions|the\s+editions)\b"
    r"|\b(?:lowest|highest|cheapest|costliest|best|worst|maximum|minimum|longest|shortest|largest|smallest)\b"
    r"[^?.]{0,40}\bever\b"
    r"|\bever\s+(?:been|offered|charged|allowed|lower|higher|cheaper)\b"
    r"|\b(?:than|compared\s+(?:to|with)|versus|vs\.?)\s+(?:before|earlier|previously|in\s+the\s+past|the\s+past|"
    r"older\s+(?:guides?|versions?|editions?)|(?:the\s+)?earlier\s+(?:guides?|versions?|editions?))\b"
    r"|\b(?:then\s+and\s+now|now\s+and\s+then|before\s+and\s+(?:after|now)|earlier\s+and\s+now|past\s+and\s+present)\b"
    r"|\b(?:has|have)\s+(?:the\s+|my\s+|our\s+|your\s+)?(?:[\w-]+\s+){0,5}?(?:gone\s+(?:up|down)|increased|"
    r"decreased|risen|fallen|dropped|reduced|changed|become\s+\w+)\b"
    r"|\bhow\s+(?:has|have)\b[^?]{0,60}\b(?:changed|moved|evolved)\b",
    re.I,
)
# Words that ask how something moved, not what it is ("gone up", "than before", "ever").
OVER_TIME_WORDS = frozenset(
    "time times ever before earlier previously past now then history historically gone up down increased "
    "increase decreased decrease risen rose fallen fell dropped reduced changed change become became stricter "
    "looser evolved moved".split()
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
    "became become becomes strict stricter strictness lenient looser loose tighter tightened relaxed evolved "
    # when ("what changed most recently?")
    "recently recent".split()
)
# "When did the 50% EMI-to-income rule start?", "Since when is Flexi-EMI offered?", "When was the fee
# raised?", "Which version first added ...?": when something appeared, changed or ended. The version in
# force cannot tell; every version, oldest first, can.
_WHEN_INTRODUCED = re.compile(
    r"\bsince\s+when\b|\bfrom\s+(?:which|what)\s+(?:version|edition|date|year)\b"
    r"|\bwhen\s+(?:did|was|were|has|have|had)\b[^?.;]*?\b(?:"
    r"start|started|begin|began|begun|commence|commenced|introduce|introduced|add|added|launch|launched|"
    r"come|came|appear|appeared|bring|brought|take\s+effect|took\s+effect|become|became|"
    r"change|changed|increase|increased|raise|raised|reduce|reduced|lower|lowered|revise|revised|"
    r"remove|removed|drop|dropped|withdraw|withdrawn|discontinue|discontinued|stop|stopped|end|ended)\b"
    r"|\b(?:which|what)\s+(?:version|edition)\s+(?:first\s+)?(?:introduced|added|brought|started|began|changed)\b"
    r"|\bfirst\s+(?:introduced|added|appeared|mentioned|offered|started|began)\b",
    re.I,
)
# "Which period had the lowest EMI for Rs 50 lakh?", "When was the processing fee highest?": a figure
# across the periods the versions were in force, one after another. Past tense only: "which period has
# the highest rate?" can ask about the tenure bands of one rate table.
_WHICH_PERIOD = re.compile(
    r"\b(?:which|what)\s+(?:period|time|year|month|date|phase)\s+(?:had|was|were|saw)\b"
    r"|\bwhen\s+(?:was|were|did)\b[^?.;]*?\b(?:lowest|highest|cheapest|costliest|largest|smallest|longest|"
    r"shortest|least|most|best|worst|peak|peaked)\b",
    re.I,
)
# What such a question calls a version: the period it was in force.
PERIOD_WORDS = frozenset("period periods time times year years month months date dates phase phases peak peaked".split())
# Words that ask when something started or changed, not what it is ("start", "since", "came in").
WHEN_INTRODUCED_WORDS = frozenset(
    "start starts started starting begin begins began begun beginning commence commences commenced since "
    "launch launched come came appear appears appeared bring brought take took effect become became raise "
    "raised reduce reduced lower lowered revise revised remove withdraw discontinue discontinued stop stopped "
    "end ended".split()
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
    # "When did X start / change?": answered from the earliest version that states it.
    since: bool = False
    # "Compare a loan sanctioned on 2026-06-15 with one on 2025-06-15": the versions in force on each date.
    as_of_dates: list[date] = field(default_factory=list)

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
    dates = [] if labels or asks_which_period(question) else dates_in_question(question, date_order, today)
    if len(dates) >= 2:
        # "Compare a loan sanctioned on 2026-06-15 with one on 2025-06-15": the version in force on each
        # date, each read on its own. One date alone stays an as-of question below.
        shown = ", ".join(d.isoformat() for d in dates[:MAX_COMPARED_VERSIONS])
        return QueryPlan(QueryClass.COMPARISON, "versions", as_of_dates=dates[:MAX_COMPARED_VERSIONS],
                         explanation=f"Question compares the versions in force on {shown}; each passage is "
                                     "labelled with its version and the period it was in force")
    if not labels and asks_when_introduced(question):
        return QueryPlan(QueryClass.ACROSS_VERSIONS, "all", since=True,
                         explanation="Question asks when something started or changed; every version is searched, "
                                     "oldest first, each passage labelled with its version and effective date")
    if not labels and _ACROSS.search(question):
        return QueryPlan(QueryClass.ACROSS_VERSIONS, "all",
                         explanation="Question asks about every version; each passage is labelled with its version")
    if not labels and asks_over_time(question) and not (_COMPARE.search(question) and _VERSION_CONTEXT.search(question)):
        return QueryPlan(QueryClass.ACROSS_VERSIONS, "all",
                         explanation="Question asks how something changed over time; every version is searched, "
                                     "oldest first, each passage labelled with its version")
    if not labels and asks_which_period(question):
        return QueryPlan(QueryClass.ACROSS_VERSIONS, "all",
                         explanation="Question asks which period had a figure; every version is searched, oldest "
                                     "first, each passage labelled with its version and the period it was in force")
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


# "... with one sanctioned today", "different from now", "vs the current version": today is the other date.
_AGAINST_NOW = re.compile(
    r"\b(?:with|to|and|from|vs\.?|versus|than)\s+(?:(?:the\s+)?(?:one|a\s+loan|loans)\s+(?:\w+\s+)?)?"
    r"(?:today|now|at\s+present|currently|(?:the\s+)?current\s+(?:one|version|policy|rules?|guide|edition))\b",
    re.I,
)


def dates_in_question(question: str, date_order: str = "DMY", today: date | None = None) -> list[date]:
    """Every date the question asks about, in the order written: full dates ("2025-06-15", "15 June 2025"),
    then months ("March 2026", taken at month end). One date set against now ("compared with today") adds
    today. Bare years are left out: "between 2020 and 2024" is a period, not two dates."""
    found = parse_dates(question, date_order)
    for match in MONTH_YEAR.finditer(re.sub(DATE_FRAGMENT, " ", question, flags=re.I)):
        year, month = int(match["year"]), MONTHS[match["month"].lower()]
        found.append(date(year, month, calendar.monthrange(year, month)[1]))
    found = list(dict.fromkeys(found))
    if len(found) == 1 and _AGAINST_NOW.search(question):
        found.append(today or datetime.now(UTC).date())
    return found


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


def asks_when_introduced(question: str) -> bool:
    """ "When did the 50% EMI-to-income rule start?", "Since when is Flexi-EMI offered?"."""
    return bool(_WHEN_INTRODUCED.search(question))


def asks_over_time(question: str) -> bool:
    """ "Has the penalty increased?", "the lowest rate ever", "shorter now than before", "then and now"."""
    return bool(_OVER_TIME.search(question))


def asks_which_period(question: str) -> bool:
    """ "Which period had the lowest EMI for Rs 50 lakh?", "When was the fee highest?"."""
    return bool(_WHICH_PERIOD.search(question))


def compares_versions(question: str) -> bool:
    """ "Has the owner changed between the versions?", "What changed in v2?", "... in each edition":
    words like "changed" and "between" then frame the question instead of naming its subject."""
    return bool(
        (_COMPARE.search(question) and (_VERSION_REF.search(question) or _VERSION_CONTEXT.search(question)))
        or _ACROSS.search(question) or _WHICH_PERIOD.search(question)
        or len(dates_in_question(question)) >= 2
    )
