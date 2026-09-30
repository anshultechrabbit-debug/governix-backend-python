"""Post-generation validation. The LLM's output is untrusted until it passes.

For every claim:
  1. Citation validation  - it cites at least one evidence id that exists.
  2. Numeric validation   - every rate, amount, percentage, date, tenure or
                            number in it appears in the cited evidence.
  3. Support validation   - its content words overlap the cited evidence.
  4. Self-reference        - it states document content, not "E2 says ...".
  5. Subject validation    - a subject the question names ("MAP/SIR Reports") is
                            only claimed about when the cited evidence names it.
  6. Number binding        - a figure is stated beside that subject in the evidence,
                            not taken from another row ("10 Years" of a
                            different record in the same retention table).
  7. Polarity              - a claim that restates a source sentence keeps its
                            negation ("No prepayment charges apply" is not
                            "Prepayment charges apply").
Claims that fail are removed (never silently "fixed"); if nothing survives,
the answer becomes a no-answer.
"""

import re
from dataclasses import dataclass, field

from app.core.stemming import stem
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


# A stem that shares this many leading characters still counts as the same term
# ("generate"/"generation").
TERM_PREFIX = 5
# Characters either side of a figure that count as "beside" it: about one table row.
NUMBER_WINDOW = 200
# A claim this close to one source sentence is a restatement, so its negation must match.
RESTATEMENT_OVERLAP = 0.7
_TERM_WORD = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?")
_NEGATION = re.compile(r"\b(?:not|no|never|cannot|nor|neither|none|without)\b|n[\u2019']t\b", re.I)
# "Sl. No.: 65", "No. of accounts", "No Change": the word "no" that negates nothing.
_NOT_NEGATION = re.compile(r"\b(?:sl|s)\.?\s*no\b\.?|\bno\.?\s*[:#]?\s*\d|\bno\.\s*of\b|\bno\s+change\b", re.I)
_SENTENCE_END = re.compile(r"(?<=[.?!;:])\s+|\n+|\s\|\s|\s*[•▪●]\s*")


def term_parts(term: str) -> list[str]:
    """ "fiu-ind" -> ["fiu", "ind"], "map/sir" -> ["map", "sir"]: text is indexed word by word."""
    return [p for p in re.split(r"[-/]", term.lower()) if p]


class TermIndex:
    """Stemmed words of a text, for asking whether it mentions a term."""

    def __init__(self, text: str) -> None:
        # "FIU- IND" (a line broken at the hyphen) is the word "FIU-IND".
        words = _TERM_WORD.findall(re.sub(r"(?<=\w)-\s+(?=\w)", "-", text.lower()))
        self.counts: dict[str, int] = {}
        for word in words:
            reduced = stem(word)
            self.counts[reduced] = self.counts.get(reduced, 0) + 1
        self.prefixes: dict[str, int] = {}
        for reduced, count in self.counts.items():
            if len(reduced) >= TERM_PREFIX:
                self.prefixes[reduced[:TERM_PREFIX]] = self.prefixes.get(reduced[:TERM_PREFIX], 0) + count

    def _count_part(self, part: str) -> int:
        reduced = stem(part)
        if reduced in self.counts:
            return self.counts[reduced]
        if len(reduced) >= TERM_PREFIX:
            return self.prefixes.get(reduced[:TERM_PREFIX], 0)
        return 0

    def count(self, term: str) -> int:
        """How often the term occurs; a multi-part term counts as its rarest part."""
        return min((self._count_part(p) for p in term_parts(term)), default=0)

    def mentions(self, term: str) -> bool:
        return self.count(term) > 0


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


def _is_subject(term: str) -> bool:
    """A content term ("map/sir", "prepayment"), not a function word or a figure ("under", "2030")."""
    parts = term_parts(term)
    return bool(parts) and all(_content_words(part) == {part} for part in parts)


def _sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE_END.split(text) if s and s.strip()]


def _flips_polarity(claim: str, cited_text: str) -> bool:
    """The claim restates one source sentence but drops or adds its negation."""
    words = _content_words(claim)
    if not words:
        return False
    sentences = _sentences(cited_text)
    best, best_index = 0.0, -1
    for index, sentence in enumerate(sentences):
        overlap = len(words & _content_words(sentence)) / len(words)
        if overlap > best:
            best, best_index = overlap, index
    if best < RESTATEMENT_OVERLAP:
        return False  # a paraphrase ("maximum 70%" for "shall not exceed 70%"): polarity is not comparable
    claim_negated, source_negated = _negated(claim), _negated(sentences[best_index])
    if claim_negated and not source_negated:
        # The next sentences may carry the short answer of a question-and-answer pair ("... income? Ans. No.").
        return not _negated(" ".join(sentences[best_index + 1:best_index + 3]))
    return source_negated and not claim_negated


def _negated(text: str) -> bool:
    return bool(_NEGATION.search(_NOT_NEGATION.sub(" ", text)))


def _window(text: str, start: int, end: int) -> str:
    """The text beside a figure: up to NUMBER_WINDOW characters either side, within its line (a table row)."""
    left = max(text.rfind("\n", 0, start) + 1, start - NUMBER_WINDOW)
    newline = text.find("\n", end)
    right = min(newline if newline != -1 else len(text), end + NUMBER_WINDOW)
    return text[left:right]


def _unbound_numbers(text: str, cited_text: str, subjects: list[str], skip: set[tuple[str, str]]) -> list[str]:
    """Figures in the claim that the evidence never states beside the claim's subject."""
    index = TermIndex(cited_text)
    present = [t for t in subjects if index.mentions(t)]
    if not present:
        return []
    rarest = min(index.count(t) for t in present)
    anchors = [t for t in present if index.count(t) == rarest]
    occurrences = extract_numeric_facts(cited_text)
    unbound = []
    for fact in extract_numeric_facts(text):
        if fact.kind == "number" or fact.key in skip:
            continue  # bare numbers are section/clause references; provenance numbers are metadata
        windows = [_window(cited_text, o.start, o.end) for o in occurrences if o.value == fact.value]
        if windows and not any(TermIndex(w).mentions(t) for w in windows for t in anchors):
            unbound.append(fact.raw)
    return unbound


def validate_claims(
    raw_claims: list[dict], evidence: dict[str, EvidenceText], subjects: list[str] | None = None,
) -> list[ClaimResult]:
    """`subjects` are the question's key terms: what the claims must be about."""
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

        claim_index = TermIndex(text)
        named = [t for t in subjects or [] if _is_subject(t) and claim_index.mentions(t)]
        evidence_index = TermIndex(cited_text)
        if absent := [t for t in named if not evidence_index.mentions(t)]:
            result.valid = False
            result.problems.append(f"the cited evidence does not mention {', '.join(absent)}")
        elif unbound := _unbound_numbers(text, cited_text, named, allowed):
            result.valid = False
            result.problems.append(f"{', '.join(unbound)} is not stated for {', '.join(named)} in the cited evidence")
        if _flips_polarity(text, cited_text):
            result.valid = False
            result.problems.append("reverses the negation of the source sentence")
        results.append(result)
    return results
