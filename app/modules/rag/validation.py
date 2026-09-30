"""Post-generation validation. The LLM's output is untrusted until it passes.

For every claim:
  1. Citation validation  - it cites at least one evidence id that exists.
  2. Numeric validation   - every rate, amount, percentage, date, tenure or
                            number in it appears in the cited evidence.
  3. Support validation   - its content words overlap the cited evidence.
  4. Self-reference        - it states document content, not "E2 says ...".
Claims that fail are removed (never silently "fixed"); if nothing survives,
the answer becomes a no-answer.
"""

import re
from dataclasses import dataclass, field

from app.modules.citations.numerics import extract_numeric_facts

# Fraction of a claim's content words that must appear in the cited evidence.
# 0.35 rather than 0.5: a faithful paraphrase shares most but not all of its
# vocabulary with the source, and the numeric check below is the load-bearing
# guard against fabrication. Content-word overlap is a coarse safety net, not
# the primary one.
MIN_SUPPORT = 0.35
# Claims are single sentences by contract; a claim much longer than any source
# sentence is padding and is judged on support alone rather than trusted.
_EVIDENCE_REF = re.compile(r"\b[EGD]\d{1,2}\b")
_WORD = re.compile(r"[a-z][a-z0-9]{2,}")
_STOP = frozenset(
    "the and for with that this from shall will are was were have has not any all per such under "
    "which their there been being into than then also only must may can should its bank policy".split()
)
# Reporting and framing words a faithful claim adds around a quoted fact
# ("The document is dated ...", "KPTCL suggested ..."). They carry no fact of
# their own, so they neither support nor weaken a claim.
_FRAMING = frozenset(
    "document documents titled title dated published publishes publication issued states stated says said "
    "mentions mentioned notes noted suggested suggests suggestion commented comment comments requested "
    "replied reply response responded remarks according listed lists shows shown described describes "
    "includes included contains".split()
)


@dataclass
class EvidenceText:
    id: str
    text: str  # evidence text including expanded context
    allowed_numbers: set[tuple[str, str]] = field(default_factory=set)  # provenance: section no., version, pages


@dataclass
class ClaimResult:
    text: str
    evidence_ids: list[str]
    valid: bool
    problems: list[str] = field(default_factory=list)
    numbers_checked: int = 0


def _content_words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP and w not in _FRAMING}


def validate_claims(raw_claims: list[dict], evidence: dict[str, EvidenceText]) -> list[ClaimResult]:
    results = []
    for raw in raw_claims:
        text = " ".join(str(raw.get("text", "")).split())
        cited = [e for e in dict.fromkeys(raw.get("evidence_ids") or []) if isinstance(e, str)]
        result = ClaimResult(text=text, evidence_ids=[], valid=True)
        if not text:
            continue

        known = [e for e in cited if e in evidence]
        unknown = [e for e in cited if e not in evidence]
        if unknown:
            result.problems.append(f"invented citation(s) {', '.join(unknown)}")
        if not known:
            result.valid = False
            result.problems.append("no valid citation")
            results.append(result)
            continue
        result.evidence_ids = known

        cited_text = "\n".join(evidence[e].text for e in known)
        # "E2 states 'Chapter : Contents'" describes the evidence, not the document.
        talk = [ref for ref in _EVIDENCE_REF.findall(text) if ref not in cited_text]
        if talk:
            result.valid = False
            result.problems.append(f"refers to evidence id(s) {', '.join(talk)} instead of the document")
            results.append(result)
            continue
        available = {f.key for f in extract_numeric_facts(cited_text)}
        allowed = set().union(*(evidence[e].allowed_numbers for e in known))
        # A bare number may legitimately restate a figure the evidence expresses
        # in another unit or with a unit attached ("75" for "75%" or "75 lakh"),
        # so any kind of fact also matches on value alone.
        available_values = {value for _kind, value in available | allowed}
        for fact in extract_numeric_facts(text):
            result.numbers_checked += 1
            if fact.key in available or fact.key in allowed or fact.value in available_values:
                continue
            result.valid = False
            result.problems.append(f"'{fact.raw}' is not in the cited evidence")

        claim_words = _content_words(text)
        if claim_words:
            support = len(claim_words & _content_words(cited_text)) / len(claim_words)
            if support < MIN_SUPPORT:
                result.valid = False
                result.problems.append(f"weak support ({support:.0%} of terms found in evidence)")
        results.append(result)
    return results
