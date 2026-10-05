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
from app.modules.rag.query_plan import normal_label

# Fraction of a claim's content words that must appear in the cited evidence.
# 0.35 rather than 0.5: a faithful paraphrase shares most but not all of its
# vocabulary with the source, and the numeric check below is the load-bearing
# guard against fabrication. Content-word overlap is a coarse safety net, not
# the primary one.
MIN_SUPPORT = 0.35
# Claims are single sentences by contract; a claim much longer than any source
# sentence is padding and is judged on support alone rather than trusted.
_EVIDENCE_REF = re.compile(r"\b[EGD]\d{1,2}\b")
# "The document does not provide the repo rate": a statement about what the evidence lacks,
# not a fact from it. It is reported once as a note instead of cited to every passage.
# "explicitly", "specifically", ...: "is not explicitly stated in the policy" is the same statement.
_HEDGE = r"(?:(?:explicitly|specifically|clearly|directly|expressly)\s+)?"
_ABSENCE = re.compile(
    r"\b(?:document|documents|evidence|policy|policies|text|source|sources|records?|version\s*[\d.]+)\s+"
    rf"(?:does|do|did)\s*n[o']t\s+{_HEDGE}"
    r"(?:provide|mention|specify|state|contain|include|cover|say|address|give|list|define)"
    rf"|\bnot\s+{_HEDGE}(?:provided|mentioned|specified|stated|available|covered|included|addressed|defined)\s+in\s+the\b"
    r"|\bno\s+(?:information|mention|details?)\s+(?:is\s+|are\s+)?(?:provided|given|available)\b"
    r"|\bthere\s+is\s+no\s+(?:stated|specified|explicit|mentioned|defined|documented)\b"
    r"|\b(?:in|from)\s+the\s+(?:provided|available|cited|given)\s+(?:evidence|documents?|text|sources?)\b",
    re.IGNORECASE,
)
ABSENCE_PROBLEM = "says what the documents do not contain"
# A variant the question pins down ("Tier 2", "Level 3", "Grade B"): rule books repeat the same
# rule for every variant with a different figure, so a claim about another variant is not an
# answer. (Section and clause numbers are not variants: "5.2" belongs to "Section 5".)
_QUALIFIER = re.compile(
    r"\b(tier|level|grade|band|category|class|phase|stage|slab|bucket)\s+(\d+(?:\.\d+)?|[A-Z](?![a-z]))",
    re.IGNORECASE,
)


_ZONE = re.compile(r"\b([A-Z][a-z]+(?:-[A-Z][a-z]+)?)\s+Zone\b")


def _variants(text: str) -> list[tuple[str, str]]:
    """("tier", "2"), ("zone", "north-east") ... as the text names them."""
    found = [(word.lower(), value.lower()) for word, value in _QUALIFIER.findall(text)]
    return found + [("zone", name.lower()) for name in _ZONE.findall(text)]


def _variant_name(word: str, value: str) -> str:
    if word == "zone":
        return f"{value.title()} Zone"
    return f"{word.title()} {value.upper() if value.isalpha() else value}"


def qualifiers(question: str) -> dict[str, str]:
    """The variants the question names, one value each ("Tier 2 / North Zone" -> {"tier": "2", "zone": "north"})."""
    seen: dict[str, set[str]] = {}
    for word, value in _variants(question):
        seen.setdefault(word, set()).add(value)
    return {word: values.pop() for word, values in seen.items() if len(values) == 1}
# Evidence ids the model appended as citation marks ("... frozen (E1, E2).", "... frozen. [E3]"):
# the citation is already in evidence_ids, so the marks are dropped rather than the claim.
_ID = r"[EGD]\d{1,2}"
# A citation the model started and did not finish: "... microfinance loans [".
_DANGLING_MARK = re.compile(r"\s*[\[\(]\s*$")
_CITATION_MARKS = re.compile(
    rf"\s*[\(\[]\s*{_ID}(?:\s*(?:,|;|and|&)\s*{_ID})*\s*[\)\]]"
    rf"|(?:\s*,?\s*\b{_ID}\b)+(?=\s*[.;!?]?\s*$)"
)
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
# ... and at least this share of the shorter stem: "custo" alone does not make
# "custodian" the same word as "customer".
TERM_PREFIX_SHARE = 0.7
# Words a policy uses for the same thing a question asks about in other words. A question
# about who "owns" a policy is answered by its "Policy owner"; "preserved" by "retained".
# Each group is matched by stem, both ways, and only decides whether a term is *mentioned*:
# it never puts a number or a name into an answer.
SYNONYM_GROUPS = (
    "own owns owned owner owners ownership custodian custodians",
    "retain retains retained retaining retention preserve preserves preserved preservation keep keeps kept",
    "threshold thresholds benchmark benchmarks cutoff",
    "maximum max cap caps capped ceiling exceed exceeds exceeding upto",
    "minimum min floor least",
    "approval approvals approve approves approved authorization authorisation authorize authorise "
    "authorized authorised sanction sanctioned",
    "staff employee employees personnel",
    "customer customers borrower borrowers client clients",
    "branch branches office offices",
    "review reviews reviewed verification verify verified",
    "frequency frequent often periodicity daily weekly fortnightly monthly quarterly annually yearly",
    "penalty penalties penal fine fines",
    "period periods duration tenure",
)
_SYNONYMS: dict[str, frozenset[str]] = {}
for _group in SYNONYM_GROUPS:
    _stems = frozenset(stem(_w) for _w in _group.split())
    for _s in _stems:
        _SYNONYMS[_s] = _SYNONYMS.get(_s, frozenset()) | _stems
# Characters either side of a figure that count as "beside" it: about one table row.
NUMBER_WINDOW = 200
# A subject term beside at most this share of the evidence's figures identifies a row;
# one beside more of them is a column heading.
SPECIFIC_SHARE = 0.5
# Fewer figures than this: a clause, not a table; there are no rows to mix up.
MIN_FIGURES_FOR_ROWS = 3
# A claim this close to one source sentence is a restatement, so its negation must match.
RESTATEMENT_OVERLAP = 0.7
_TERM_WORD = re.compile(r"[a-z0-9]+(?:\.[0-9]+)*")
_HAS_DIGIT = re.compile(r"\d")
# A version a claim is about ("Version 1.0", "v2", "edition 3"), and a claim about all of them.
_CLAIM_VERSION = re.compile(r"\b(?:version|v|edition)\s*(\d{1,3}(?:\.\d{1,3})?)\b", re.I)
# "... but not in Version 2.0": a version the claim says lacks something. A citation cannot show
# an absence; that version's passages are searched for the subject instead.
_NOT_IN_VERSION = re.compile(
    r"\b(?:not|never|neither|nor|absent|missing|omitted|dropped|removed)\b[^.;:]{0,40}?"
    r"\b(?:version|v|edition)\s*(\d{1,3}(?:\.\d{1,3})?)\b",
    re.I,
)
# Words of a presence claim that are not its subject ("... is present in Version 1.0 but").
_PRESENCE_WORDS = frozenset(
    "chapter chapters section sections present appears appear included includes include listed lists list "
    "contains contain version versions edition editions only but and also while whereas however although "
    "both each every all either neither two".split()
)
_EVERY_VERSION = re.compile(
    r"\b(?:both|each|every|all|either|neither)\s+(?:of\s+the\s+)?(?:two\s+)?(?:versions?|editions?)\b"
    r"|\bthe\s+two\s+(?:versions|editions)\b",
    re.I,
)
_NEGATION = re.compile(r"\b(?:not|no|never|cannot|nor|neither|none|without)\b|n[\u2019']t\b", re.I)
# "Sl. No.: 65", "No. of accounts", "No Change": the word "no" that negates nothing.
_NOT_NEGATION = re.compile(r"\b(?:sl|s)\.?\s*no\b\.?|\bno\.?\s*[:#]?\s*\d|\bno\.\s*of\b|\bno\s+change\b", re.I)
_SENTENCE_END = re.compile(r"(?<=[.?!;:])\s+|\n+|\s\|\s|\s*[•▪●]\s*")


def term_parts(term: str) -> list[str]:
    """ "fiu-ind" -> ["fiu", "ind"], "map/sir" -> ["map", "sir"]: text is indexed word by word."""
    return [p for p in re.split(r"[-/]", term.lower()) if p]


# An abbreviation is answered by the words it abbreviates: "NPA" by "Non-Performing Asset", "FIU"
# by "Financial Intelligence Unit", "LTV" by "Loan-to-Value". Only runs of capitalised words count
# (an expansion is a name), so three ordinary words that happen to start with S, T and R are not
# "STR". Connectors may sit inside a run; numbers and punctuation end it.
MIN_ABBREVIATION, MAX_ABBREVIATION = 2, 6
_INITIAL_CONNECTORS = frozenset("of and to for the in on &".split())
_RUN_BREAK = re.compile(r"[.,;:()\[\]|/\n]")
_RUN_TOKEN = re.compile(r"[A-Za-z][A-Za-z'’]*|\d+")


def capitalised_initials(text: str) -> set[str]:
    """Initials (lower case) of every run of 2-6 capitalised words: "Non-Performing Asset" -> {"np", "pa", "npa"}.

    Both with and without the connectors inside the run: "Loan-to-Value" is LTV, "Fixed Obligation
    to Income Ratio" is FOIR."""
    found: set[str] = set()

    def add(letters: list[str]) -> None:
        for start in range(len(letters)):
            for end in range(start + MIN_ABBREVIATION, min(len(letters), start + MAX_ABBREVIATION) + 1):
                found.add("".join(letters[start:end]))

    def flush(run: list[tuple[str, bool]]) -> None:
        while run and run[-1][1]:
            run = run[:-1]  # "Officer and staff": the run ends at "Officer"
        add([letter for letter, _ in run])
        add([letter for letter, connector in run if not connector])

    for segment in _RUN_BREAK.split(text):
        run: list[tuple[str, bool]] = []
        for token in _RUN_TOKEN.findall(segment.replace("-", " ")):
            if token[0].isupper():
                run.append((token[0].lower(), False))
            elif run and token.lower() in _INITIAL_CONNECTORS:
                run.append((token[0].lower(), True))
            else:
                flush(run)
                run = []
        flush(run)
    return found


def _looks_abbreviated(part: str) -> bool:
    return part.isalpha() and MIN_ABBREVIATION <= len(part) <= MAX_ABBREVIATION


class TermIndex:
    """Stemmed words of a text, for asking whether it mentions a term."""

    def __init__(self, text: str) -> None:
        self._source = text  # as written: capitalisation marks the expansions of abbreviations
        self._initials: set[str] | None = None
        # "FIU- IND" (a line broken at the hyphen) is the word "FIU-IND"; "75%" says "percent".
        text = re.sub(r"(?<=\w)-\s+(?=\w)", "-", text.lower()).replace("%", " percent ")
        # Clause "4.25.9" is one word; it also counts for its section, "4.25".
        words = [part for word in _TERM_WORD.findall(text) for part in _dotted(word)]
        self.counts: dict[str, int] = {}
        for word in words:
            reduced = stem(word)
            self.counts[reduced] = self.counts.get(reduced, 0) + 1
        self.prefixes: dict[str, list[str]] = {}
        for reduced in self.counts:
            if len(reduced) >= TERM_PREFIX and not _HAS_DIGIT.search(reduced):
                self.prefixes.setdefault(reduced[:TERM_PREFIX], []).append(reduced)

    def _count_stem(self, reduced: str) -> int:
        if reduced in self.counts:
            return self.counts[reduced]
        # A shared prefix joins word forms ("generate"/"generation"), never numbers: 4.25.1 is not 4.25.19.
        if len(reduced) >= TERM_PREFIX and not _HAS_DIGIT.search(reduced):
            return sum(self.counts[other] for other in self.prefixes.get(reduced[:TERM_PREFIX], [])
                       if _same_word_form(reduced, other))
        return 0

    def _count_part(self, part: str) -> int:
        return self._count_stem(stem(part))

    def count(self, term: str) -> int:
        """How often the term occurs; a multi-part term counts as its rarest part."""
        return min((self._count_part(p) for p in term_parts(term)), default=0)

    def mentions(self, term: str) -> bool:
        """The text uses the term, a form of it, a word the policy uses for the same thing, or (for an
        abbreviation) the capitalised words it abbreviates."""
        if self.count(term) > 0:
            return True
        parts = term_parts(term)
        return bool(parts) and all(
            self._count_part(part) > 0
            or any(self._count_stem(other) for other in _SYNONYMS.get(stem(part), ()))
            or (_looks_abbreviated(part) and part in self.initials)
            for part in parts
        )

    @property
    def initials(self) -> set[str]:
        if self._initials is None:
            self._initials = capitalised_initials(self._source)
        return self._initials


def _same_word_form(a: str, b: str) -> bool:
    """Two stems with a long enough shared beginning are forms of one word."""
    shared = 0
    for x, y in zip(a, b):
        if x != y:
            break
        shared += 1
    return shared >= max(TERM_PREFIX, round(TERM_PREFIX_SHARE * min(len(a), len(b)) + 0.49))


def _dotted(word: str) -> list[str]:
    """ "4.25.9" -> ["4.25.9", "4.25"]; any other word as it is."""
    parts = word.split(".")
    return [".".join(parts[:n]) for n in range(len(parts), 1, -1)] if len(parts) > 2 else [word]


def _unsupported_versions(
    text: str, cited_versions: set[str], cited_text: str, evidence: dict, cited_passages: list,
) -> str | None:
    """A claim about "Version 1.0" must cite Version 1.0 (or a passage that names it); one about
    "both versions" must cite more than one. One that says a version lacks something ("in
    Version 1.0 but not in Version 2.0") is checked against that version's passages instead."""
    cited = {normal_label(v) for v in cited_versions}
    lacking = {normal_label(v): v for v in _NOT_IN_VERSION.findall(text)}
    positive = _NOT_IN_VERSION.sub(" ", text)
    named = {normal_label(v): v for v in _CLAIM_VERSION.findall(positive)}
    stated = {normal_label(v) for v in _CLAIM_VERSION.findall(cited_text)}
    shown = ", ".join(sorted(cited_versions))
    if missing := [named[v] for v in named if v not in cited and v not in stated]:
        return f"is about version {', '.join(missing)} but cites only version {shown}"
    every = _EVERY_VERSION.search(positive)
    if len(cited) < 2 and every:
        return f"is about every version but cites only version {shown}"
    subject = {w for w in _content_words(positive) if w not in _PRESENCE_WORDS and not w.isdigit()}
    if every and subject:
        # "Capital Adequacy appears in both versions": each version's cited passage must have it.
        for version in sorted(cited_versions):
            own = [e for e in cited_passages if normal_label(version) in {normal_label(v) for v in e.versions}]
            if own and not any(all(TermIndex(e.text).mentions(w) for w in subject) for e in own):
                return f"says every version has it, but the version {version} passage does not"
    for version, label in lacking.items():
        passages = [e for e in evidence.values() if version in {normal_label(v) for v in e.versions} and len(e.versions) == 1]
        if subject and (found := next((e for e in passages if all(TermIndex(e.text).mentions(w) for w in subject)), None)):
            return f"says version {label} lacks it, but {found.id} (version {label}) has it"
    return None


@dataclass
class EvidenceText:
    id: str
    text: str  # evidence text including expanded context
    allowed_numbers: set[tuple[str, str]] = field(default_factory=set)  # provenance: section no., version, pages
    # The policy name, document title and section heading: a passage is about its policy even
    # where it does not repeat the name ("This policy is applicable to all employees").
    label: str = ""
    # The version(s) the passage is from (both, for a diff of two versions).
    versions: frozenset[str] = frozenset()


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
    if claim_negated and not source_negated and _negations_quoted(claim, cited_text):
        return False  # its negation comes from another sentence it combines ("... does not have updated address")
    if claim_negated and not source_negated:
        # The next sentences may carry the short answer of a question-and-answer pair ("... income? Ans. No.").
        return not _negated(" ".join(sentences[best_index + 1:best_index + 3]))
    return source_negated and not claim_negated and _restates_negated_part(claim, sentences[best_index])


def _restates_negated_part(claim: str, sentence: str) -> bool:
    """The claim repeats what the sentence negates ("No prepayment charges apply" ->
    "Prepayment charges apply"), not another part of it ("... shall be submitted by day 6,
    and must include remittances without evidence of import" -> "shall be submitted by day 6")."""
    said = _content_words(claim)
    cleaned = _NOT_NEGATION.sub(" ", sentence)
    for match in _NEGATION.finditer(cleaned):
        following = [w for w in _WORD.findall(cleaned[match.end():match.end() + 60].lower())
                     if w not in _STOP and w not in _FRAMING][:3]
        if following and sum(w in said for w in following) >= min(2, len(following)):
            return True
    return False


def _normal(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def _negations_quoted(claim: str, cited_text: str) -> bool:
    """Every negation in the claim appears, with the words after it, in the cited text."""
    source = _normal(cited_text)
    for match in _NEGATION.finditer(_NOT_NEGATION.sub(" ", claim)):
        phrase = _normal(claim[match.start():match.end() + 40]).split()[:4]
        if len(phrase) < 3 or " ".join(phrase) not in source:
            return False
    return True


def _negated(text: str) -> bool:
    return bool(_NEGATION.search(_NOT_NEGATION.sub(" ", text)))


_FULL_STOP = re.compile(r"[.!?](?=\s)")


def _window(text: str, start: int, end: int) -> str:
    """The text beside a figure, within its line (a table row): its whole sentence, and at
    least NUMBER_WINDOW characters either side. A tiered clause ("Approval authority for X
    ... up to INR 8 lakh, ...; above INR 24 lakh, the Committee.") is one sentence."""
    line_start = text.rfind("\n", 0, start) + 1
    newline = text.find("\n", end)
    line_end = newline if newline != -1 else len(text)
    stops = [m.end() for m in _FULL_STOP.finditer(text, line_start, start)]
    sentence_start = stops[-1] if stops else line_start
    following = _FULL_STOP.search(text, end, line_end)
    sentence_end = following.end() if following else line_end
    return text[max(line_start, min(sentence_start, start - NUMBER_WINDOW)):min(line_end, max(sentence_end, end + NUMBER_WINDOW))]


# A bare number that is a measured value rather than a clause reference: "against threshold 17.8".
_MEASURE = re.compile(r"\b(?:threshold|ratio|score|index|rating|factor|weight|multiple|coefficient)\s+(?:of\s+)?$", re.I)


def _values(text: str) -> list:
    """Figures a subject can own: rates, amounts, dates, tenures, and measured values. Other
    bare numbers are section and clause references."""
    return [f for f in extract_numeric_facts(text) if f.kind != "number" or _MEASURE.search(text[max(0, f.start - 30):f.start])]


def _unbound_numbers(text: str, cited_text: str, subjects: list[str], skip: set[tuple[str, str]]) -> list[str]:
    """Figures in the claim that the evidence never states beside the claim's subject.

    "Beside the subject" means within the same table row or sentence as one of the claim's
    specific terms: those found near few of the evidence's figures (the row's subject, "MAP/SIR
    Reports", "Xpress Credit"), not those near most of them (a column heading repeated on every
    row, "Proposed retention period"). A figure taken from another row is then caught.
    """
    present = [t for t in subjects if TermIndex(cited_text).mentions(t)]
    figures = _values(cited_text)
    if not present or len(figures) < MIN_FIGURES_FOR_ROWS:
        return []  # a clause with one or two figures has no rows to confuse
    windows = [TermIndex(_window(cited_text, o.start, o.end)) for o in figures]
    near = {t: sum(w.mentions(t) for w in windows) for t in present}
    specific = [t for t in present if 0 < near[t] <= len(windows) * SPECIFIC_SHARE]
    if not specific:
        return []  # every subject term sits beside most figures: nothing to tell rows apart by
    unbound = []
    for fact in _values(text):
        if fact.key in skip:
            continue  # provenance numbers (version, pages) are metadata
        near = [w for o, w in zip(figures, windows, strict=True) if o.value == fact.value]
        if near and not any(w.mentions(t) for w in near for t in specific):
            unbound.append(fact.raw)
    return unbound


def validate_claims(
    raw_claims: list[dict], evidence: dict[str, EvidenceText], subjects: list[str] | None = None,
    question: str = "",
) -> list[ClaimResult]:
    """`subjects` are the question's key terms: what the claims must be about; `question` is
    read for the variants it pins down ("Tier 2")."""
    asked = qualifiers(question)
    results = []
    for raw in raw_claims:
        text = _DANGLING_MARK.sub("", " ".join(_CITATION_MARKS.sub("", str(raw.get("text", ""))).split()))
        cited = [e for e in dict.fromkeys(raw.get("evidence_ids") or []) if isinstance(e, str)]
        result = ClaimResult(text=text, evidence_ids=[], valid=True)
        if not text:
            continue
        if _ABSENCE.search(text):
            result.valid = False
            result.problems.append(ABSENCE_PROBLEM)
            results.append(result)
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
        # "Version 2.0" names a version of the evidence; it is a reference, not a figure.
        versions = {normal_label(v) for e in evidence.values() for v in e.versions}
        references = [m.span(1) for m in _CLAIM_VERSION.finditer(text) if normal_label(m.group(1)) in versions]
        for fact in extract_numeric_facts(text):
            if any(start <= fact.start and fact.end <= end for start, end in references):
                continue
            result.numbers_checked += 1
            if fact.key in available or fact.key in allowed or fact.value in available_values:
                continue
            result.valid = False
            result.problems.append(f"'{fact.raw}' is not in the cited evidence")

        claim_words = _content_words(text)
        if claim_words:
            labels = " ".join(evidence[e].label for e in known)
            support = len(claim_words & _content_words(f"{cited_text}\n{labels}")) / len(claim_words)
            if support < MIN_SUPPORT:
                result.valid = False
                result.problems.append(f"weak support ({support:.0%} of terms found in evidence)")

        claim_index = TermIndex(text)
        named = [t for t in subjects or [] if _is_subject(t) and claim_index.mentions(t)]
        evidence_index = TermIndex(cited_text + "\n" + "\n".join(evidence[e].label for e in known))
        if absent := [t for t in named if not evidence_index.mentions(t)]:
            result.valid = False
            result.problems.append(f"the cited evidence does not mention {', '.join(absent)}")
        elif unbound := _unbound_numbers(text, cited_text, named, allowed):
            result.valid = False
            result.problems.append(f"{', '.join(unbound)} is not stated for {', '.join(named)} in the cited evidence")
        mismatched = [(w, v) for w, v in _variants(text) if w in asked and v != asked[w]]
        other = [_variant_name(w, v) for w, v in mismatched]
        if other:
            result.valid = False
            wanted = ", ".join(dict.fromkeys(_variant_name(w, asked[w]) for w, _v in mismatched))
            result.problems.append(f"is about {', '.join(dict.fromkeys(other))}, not {wanted}")
        cited_versions = set().union(*(evidence[e].versions for e in known))
        if cited_versions and (wrong := _unsupported_versions(
                text, cited_versions, cited_text, evidence, [evidence[e] for e in known])):
            result.valid = False
            result.problems.append(wrong)
        if _flips_polarity(text, cited_text):
            result.valid = False
            result.problems.append("reverses the negation of the source sentence")
        results.append(result)
    return results
