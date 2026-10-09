"""Post-generation validation. The LLM's output is untrusted until it passes.

For every claim:
  1. Citation validation  - it cites at least one evidence id that exists.
  2. Numeric validation   - every rate, amount, percentage, date, tenure or
                            number in it appears in the cited evidence (or is
                            the reader's figure, or arithmetic recomputed in
                            app.modules.rag.calculate).
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
from decimal import Decimal, InvalidOperation

from app.core.stemming import stem
from app.modules.citations.numerics import extract_numeric_facts
from app.modules.rag.calculate import Calculation, figures_in, matches, near, written_arithmetic
from app.modules.rag.query_plan import normal_label, without_version_refs

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
    "includes included contains "
    # The outcome of a rule applied to the reader's case ("so you do not qualify", "meets the minimum").
    "qualify qualifies qualified eligible ineligible meet meets met satisfy satisfies satisfied "
    # A figure placed in a table's row ("650 falls in the 650 - 699 band"): the table only lists the row.
    "falls fall lies belongs band bands bracket brackets slab slabs tier tiers bucket buckets".split()
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
    "maximum max cap caps capped ceiling exceed exceeds exceeding upto atmost",
    "minimum min floor least",
    "approval approvals approve approves approved authorization authorisation authorize authorise "
    "authorized authorised sanction sanctioned",
    "staff employee employees personnel",
    "customer customers borrower borrowers client clients",
    "branch branches office offices",
    "review reviews reviewed verification verify verified",
    "frequency frequent often periodicity daily weekly fortnightly monthly quarterly annually yearly",
    "penalty penalties penal fine fines fined",
    "period periods duration tenure term terms",
    # Lending vocabulary a reader and a policy word differently ("how much does the lender finance?" /
    # "we fund up to 65%", "a fee for closing early" / "foreclosure charge").
    "fund funds funded funding finance finances financed financing lend lends lending lent lender lenders financier "
    "borrow borrows borrowed borrowing",
    "fee fees charge charges charged levy levied",
    "close closes closed closing closure foreclose foreclosed foreclosure preclose preclosure",
    "income incomes earning earnings salary salaries",
    "require requires required requirement requirements need needs needed necessary mandatory compulsory must",
    "late overdue delay delayed arrear arrears",
    "emi emis instalment instalments installment installments",
    "pay pays paid paying payment payments repay repays repaid repaying repayment repayments",
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


_LIMIT_WORDS = {"up to": "upto", "at most": "atmost", "not more than": "atmost", "no more than": "atmost",
                "not less than": "least", "no less than": "least"}
_LIMIT_PHRASES = re.compile(r"\b(?:up\s+to|at\s+most|not\s+more\s+than|no\s+more\s+than|not\s+less\s+than|no\s+less\s+than)\b")


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
        # Limits written as phrases are the words for them: "up to 15 years" states a maximum.
        text = _LIMIT_PHRASES.sub(lambda m: _LIMIT_WORDS[" ".join(m.group(0).split())], text)
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
    # Failed only on wording: words the passage does not use ("bracket", "criterion"), or the reader's
    # own figure phrased in a way these patterns do not recognise. Its citations, figures, versions,
    # variants and negations all passed, so its meaning can be judged instead (RAGService._judge).
    wording_only: bool = False


def _content_words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP and w not in _FRAMING}


def _is_subject(term: str) -> bool:
    """A content term ("map/sir", "prepayment"), not a function word or a figure ("under", "2030")."""
    parts = term_parts(term)
    return bool(parts) and all(_content_words(part) == {part} for part in parts)


def _sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE_END.split(text) if s and s.strip()]


# ", not 90 days", "rather than 1%": the claim corrects a figure, it does not negate the rule.
_CONTRAST = re.compile(
    r",?\s*(?:and\s+)?(?:not|rather\s+than|instead\s+of)\s+(?:the\s+|a\s+|an\s+)?(?:₹|rs\.?|inr)?\s*\d[\d,.]*\s*"
    r"(?:%|percent|per\s+cent|lakhs?|crores?|years?|months?|days?|weeks?)?", re.I)


def _flips_polarity(claim: str, cited_text: str, *, outcome: bool = False) -> bool:
    """The claim restates one source sentence but drops or adds its negation.

    `outcome`: the claim applies the rule to the reader's own figure, so a negation it adds is the
    result for the reader ("At 27, you do not meet the applicant age of 28 to 65 years"), not a
    reversed rule. Dropping the source's negation is never allowed."""
    # Only a figure the source does not state can be the one corrected: "is not 60 days" of a 60-day
    # rule negates the rule itself.
    stated = {f.value for f in extract_numeric_facts(cited_text)}
    claim = _CONTRAST.sub(
        lambda m: " " if (facts := extract_numeric_facts(m.group(0))) and all(f.value not in stated for f in facts)
        else m.group(0), claim)
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
        if outcome:
            return False
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
    ... up to INR 8 lakh, ...; above INR 24 lakh, the Committee.") is one sentence. A figure in
    a table cell is also beside its column's heading ("Processing fee" over "1.75% + GST")."""
    line_start = text.rfind("\n", 0, start) + 1
    newline = text.find("\n", end)
    line_end = newline if newline != -1 else len(text)
    stops = [m.end() for m in _FULL_STOP.finditer(text, line_start, start)]
    sentence_start = stops[-1] if stops else line_start
    following = _FULL_STOP.search(text, end, line_end)
    sentence_end = following.end() if following else line_end
    window = text[max(line_start, min(sentence_start, start - NUMBER_WINDOW)):min(line_end, max(sentence_end, end + NUMBER_WINDOW))]
    return f"{window}\n{heading}" if (heading := _column_heading(text, line_start, line_end, start)) else window


def _column_heading(text: str, line_start: int, line_end: int, start: int) -> str:
    """The heading over the cell at `start`, when its line is a table row ("a | b | c") under a
    heading row of as many cells: the first row of the table."""
    row = text[line_start:line_end]
    if "|" not in row:
        return ""
    heading = ""
    for line in reversed(text[:line_start].split("\n")):
        if not line.strip():
            continue  # rows are often a blank line apart
        if line.count("|") != row.count("|"):
            break
        heading = line
    if not heading:
        return ""
    cells = heading.split("|")
    own = cells[text[line_start:start].count("|")].strip()
    # The first column's heading names what every row is ("Loan (Rs. lakh)" over 30 / 50 / 100): each cell of
    # a row is beside it too, or a figure in "50 | Rs. 106,358 | Rs. 66,214" is "not stated for loan".
    first = cells[0].strip()
    return " ".join(dict.fromkeys(c for c in (own, first) if c))


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


# --- figures an answer may state that its evidence does not ------------------------------
#
# Applying a rule to the reader's own case ("a Rs. 60 lakh loan at 0.40% pays Rs. 24,000") states
# figures the policy never prints. They are accepted only when they can be checked: the reader's
# own figure where the question puts it, and arithmetic on the reader's figures and the evidence's,
# recomputed here. Arithmetic on the evidence's figures alone is not accepted (except a change of
# unit): with enough figures in a passage, some sum would "explain" almost any invented number.
_FAMILY = {"amount": "value", "quantity": "value", "number": "value", "percent": "percent"}
_PER_YEAR = {"month": Decimal(12), "week": Decimal(52), "day": Decimal(365)}
_GIVEN_BEFORE = re.compile(
    r"(?:\bnot|\bno|rather than|instead of|\bfor|\bof|\bon|\bwith|\bif|\bwhen|\bat|\bas of|\bfrom|\buntil|"
    r"\bsince|\bbefore|\bafter|\bthan|\bvs\.?|\bversus|\bbut|\bonly|\bjust|\baged)\s*(?:a|an|the|only|just)?\s*$",
    re.I)
_GIVEN_AFTER = re.compile(
    # "You are 27, which is below ...", "27 years old, which is under ...", "age 25 is not eligible"
    r"^\s*(?:years?\s+old\s*)?,?\s*(?:(?:which|that|this|it)\s+)?"
    r"(?:(?:is|are|was|were|would\s+be|falls?|lies)\s+)?(?:not\s+|well\s+|still\s+)?"
    r"(?:below|above|under|over|less|more|lower|higher|greater|within|beyond|short\s+of|exceeds?|"
    r"meets?|does\s+not|doesn't|isn't|qualif|requires?|needs?|eligible|ineligible|allowed|permitted|"
    r"accepted|acceptable|enough|sufficient|insufficient)", re.I)


# What a recomputed figure is ("the total interest", "you save", "the difference"): the calculation's
# result, which no passage names. Only for a claim that states such a result; every other subject word
# must still be in its evidence.
_RESULT_WORDS = frozenset(
    "total totals overall sum cumulative combined altogether net saving savings save saves saved difference "
    "extra additional".split()
)


# How a computed figure is put to the reader ("you would pay about ...", "you can borrow up to ...").
_ADDRESSING = frozenset("you your yours yourself would could will might about around approximately roughly".split())


def _number(fact) -> Decimal | None:
    """A fact's number: "93.98 lakh" -> 9398000, "180 months" -> 180; None for a date."""
    try:
        return Decimal(fact.value.partition(" ")[0])
    except InvalidOperation:
        return None


# "Your total EMIs are 60% of your income", "you earn Rs. 50,000": the figure is said to be the reader's.
# A claim is one sentence, so a full stop here is an abbreviation or a decimal ("Your EMIs of Rs. 30,000").
_READERS_OWN = re.compile(r"\b(?:your|you|you're|you’re|you've|you’ve)\b[^;!?]{0,50}$", re.I)


def _given_in_context(text: str, fact, *, premise: bool = False) -> bool:
    """The question's figure is used as the reader's. A `premise` (a figure the question says the documents
    give: "the updated policy says the rate is 2%") may only be corrected ("10.05%, not 2%")."""
    if premise:
        return bool(_CORRECTED.search(text[max(0, fact.start - 30):fact.start]))
    return bool(_GIVEN_BEFORE.search(text[max(0, fact.start - 30):fact.start])
                or _GIVEN_AFTER.match(text[fact.end:fact.end + 40])
                or _READERS_OWN.search(text[max(0, fact.start - 50):fact.start]))


_CORRECTED = re.compile(r"(?:\bnot|\bno|\bnor|rather than|instead of|\bthan|\bvs\.?|\bversus|\bbut)\s*(?:a|an|the)?\s*$", re.I)
# "The policy says the rate is 2%", "according to the circular, the fee is 1%", "Policy update: rate is 2%": what
# the question claims the documents say. Its figures are the reader's premise, not the reader's own case.
_CLAIMED = re.compile(
    r"\b(?:polic(?:y|ies)|documents?|guide|rules?|updates?|circulars?|notices?|clauses?|sections?|bank)\b"
    r"[^.?!\n]{0,60}?\b(?:says?|said|states?|stated|is\s+now|are\s+now|now\s+(?:is|are)|changed|updated|reads?)\b"
    r"|\baccording\s+to\b|\b(?:policy|rule)\s+update\b", re.I)


def premise_values(question: str) -> set[str]:
    """The values of the figures the question attributes to the documents ("the policy says ... 2%")."""
    values = set()
    for sentence in re.split(r"(?<=[.?!])\s+|\n+", question):
        if _CLAIMED.search(sentence):
            values |= {f.value for f in extract_numeric_facts(sentence)}
    return values


# "28 to 65 years", "2-3%": the first figure of a range takes the unit written after the second.
_RANGE_START = re.compile(r"(\d+(?:\.\d+)?)\s*(?:to|-|–|—)\s*$", re.I)


def _range_starts(text: str, facts: list) -> set[tuple[str, str]]:
    starts: set[tuple[str, str]] = set()
    for fact in facts:
        if fact.kind in ("duration", "percent") and (m := _RANGE_START.search(text[max(0, fact.start - 20):fact.start])):
            unit = fact.value.partition(" ")[2] if fact.kind == "duration" else "%"
            starts |= {f.key for f in extract_numeric_facts(f"{m.group(1)} {unit}".replace(" %", "%"))}
    return starts


def _magnitude(fact) -> tuple[str, Decimal, str] | None:
    """(family, number, unit): "6000000" rupees, "0.4" percent, "35" year."""
    try:
        if fact.kind == "duration":
            number, _, unit = fact.value.partition(" ")
            return "duration", Decimal(number), unit
        if fact.kind in _FAMILY:
            return _FAMILY[fact.kind], Decimal(fact.value), ""
    except InvalidOperation:
        return None
    return None


def _close(a: Decimal, b: Decimal) -> bool:
    return abs(a - b) <= max(Decimal("0.01"), abs(b) * Decimal("0.005"))


def _in_years(number: Decimal, unit: str) -> Decimal | None:
    if unit == "year":
        return number
    return number / _PER_YEAR[unit] if unit in _PER_YEAR else None


def _derived(fact, evidence_facts: list, given: list) -> bool:
    """The figure is arithmetic on the question's figures and the evidence's, or the evidence's figure
    in another unit of time ("72 months" stated as "6 years")."""
    target = _magnitude(fact)
    if target is None:
        return False
    family, value, unit = target
    stated = [m for f in evidence_facts if (m := _magnitude(f))]
    asked = [m for f in given if (m := _magnitude(f))]
    if family == "duration":
        # The evidence's figure in another unit; never the question's own figure restated.
        wanted = _in_years(value, unit)
        if wanted is not None and any((y := _in_years(n, u)) is not None and _close(wanted, y)
                                      for _f, n, u in stated if _f == "duration"):
            return True
    for index, (a_family, a, a_unit) in enumerate(asked):
        others = [(m, True) for m in stated] + [(m, False) for m in asked[:index] + asked[index + 1:]]
        for (b_family, b, b_unit), documented in others:
            candidates = []
            # A rate applies to an amount only when the documents state one of them: "80% of Rs. 1 crore"
            # from a reader who misremembers 80% is their premise, not the policy's figure.
            if a_family == "percent" and b_family == "value" and documented:
                candidates.append(b * a / 100)
            if b_family == "percent" and a_family == "value" and documented:
                candidates.append(a * b / 100)
            if a_family == b_family and a_unit == b_unit:
                candidates += [a + b, abs(a - b)]
            # "EMIs of Rs. 1,07,767 over 15 years come to Rs. 1,93,98,060": a monthly figure over the
            # question's period, in months.
            for (x_family, x, x_unit), (y_family, y, _y_unit) in (((a_family, a, a_unit), (b_family, b, b_unit)),
                                                                   ((b_family, b, b_unit), (a_family, a, a_unit))):
                if family == "value" and x_family == "duration" and y_family == "value" and x_unit in ("year", "month"):
                    candidates.append(y * (x * 12 if x_unit == "year" else x))
            if a_family == "value" and b_family == "value":
                candidates.append(a * b)
                if b:
                    candidates.append(a / b)
                if a:
                    candidates.append(b / a)
                if family == "percent":
                    # "Rs. 35,000 is 58.33% of Rs. 60,000": one figure as a share of the other.
                    candidates += [x * 100 for x in (a / b if b else None, b / a if a else None) if x is not None]
            if candidates and family in ("value", "percent", "duration") and any(_close(value, c) for c in candidates):
                return True
    return False


def validate_claims(
    raw_claims: list[dict], evidence: dict[str, EvidenceText], subjects: list[str] | None = None,
    question: str = "", calculations: list[Calculation] | None = None,
) -> list[ClaimResult]:
    """`subjects` are the question's key terms: what the claims must be about; `question` is
    read for the variants it pins down ("Tier 2"); `calculations` are the answer's recomputed
    arithmetic (app.modules.rag.calculate), whose results a claim citing their evidence may state."""
    asked = qualifiers(question)
    given = extract_numeric_facts(without_version_refs(question))  # the reader's own figures
    given_values = {f.value for f in given}
    # "I am 27" restated as "27 years": the reader's bare number with the unit its rule uses.
    given_values |= {f"{f.value} {unit}" for f in given if f.kind == "number" for unit in ("year", "month", "day")}
    premises = premise_values(without_version_refs(question))  # "the policy says the rate is 2%": correct only
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
        evidence_facts = extract_numeric_facts(cited_text)
        available = {f.key for f in evidence_facts} | _range_starts(cited_text, evidence_facts)
        allowed = set().union(*(evidence[e].allowed_numbers for e in known))
        # A bare number may legitimately restate a figure the evidence expresses
        # in another unit or with a unit attached ("75" for "75%" or "75 lakh"),
        # so any kind of fact also matches on value alone.
        available_values = {value for _kind, value in available | allowed}
        available_values |= {value.partition(" ")[0] for kind, value in available if kind == "duration"}  # "60-day"
        # "Version 2.0" names a version of the evidence; it is a reference, not a figure.
        versions = {normal_label(v) for e in evidence.values() for v in e.versions}
        references = [m.span(1) for m in _CLAIM_VERSION.finditer(text) if normal_label(m.group(1)) in versions]
        facts = [f for f in extract_numeric_facts(text)
                 if not any(start <= f.start and f.end <= end for start, end in references)]
        stated = [f for f in facts if f.key in available or f.key in allowed or f.value in available_values]
        derived = [f for f in facts if f not in stated and _derived(f, evidence_facts, given)]
        # The result (or a step) of a calculation recomputed over the evidence this claim cites.
        mine = [c for c in calculations or () if set(c.evidence_ids) & set(known)]
        # ... or of one the claim writes out itself ("₹35,000 / ₹60,000 × 100 = 58.33%"), recomputed the same way.
        written = written_arithmetic(text, figures_in(question) | figures_in(cited_text))
        computed = [f for f in facts if f not in stated and f not in derived and (n := _number(f)) is not None
                    and ((mine and any(matches(n, c) for c in mine)) or any(near(n, v) for v in written))]
        applied = False  # the claim applies a rule to the reader's own figure
        hard = False  # a failure no rewording explains (see ClaimResult.wording_only)
        for fact in facts:
            result.numbers_checked += 1
            if fact in stated or fact in derived or fact in computed:
                continue
            # The question's own figure, used as the question uses it ("for a Rs. 60 lakh loan", "Rs. 8
            # lakh is below ...", "..., not 1%"), beside a figure the evidence supports.
            if fact.value in given_values and (stated or derived or computed) and _given_in_context(
                    text, fact, premise=fact.value in premises):
                applied = True
                continue
            result.valid = False
            if fact.value in premises:
                # What the question claims the documents say, stated as a fact: never judged by meaning.
                hard = True
                result.problems.append(f"'{fact.raw}' is the question's claim about the documents, not their figure")
            elif fact.value in given_values:
                # The reader's figure, phrased otherwise: whether it is presented as theirs is a question
                # of meaning. Any other figure the evidence does not state is invented.
                result.problems.append(f"'{fact.raw}' is the question's figure, not set against a rule")
            else:
                hard = True
                result.problems.append(f"'{fact.raw}' is not in the cited evidence")

        claim_words = _content_words(text)
        # A recomputed figure is put in the question's words ("you would pay", "you can borrow"), around
        # figures that were all checked: those words count as supported.
        echoed = (_RESULT_WORDS | _ADDRESSING | _content_words(question)) if computed or derived else frozenset()
        if claim_words:
            labels = " ".join(evidence[e].label for e in known)
            # Word forms count ("deposits" supports "deposit", "reported" supports "reporting"), as in
            # every other term check: applying a rule restates it in other forms of its own words.
            source = TermIndex(f"{cited_text}\n{labels}")
            support = sum(1 for w in claim_words if w in echoed or source.mentions(w)) / len(claim_words)
            if support < MIN_SUPPORT:
                result.valid = False
                result.problems.append(f"weak support ({support:.0%} of terms found in evidence)")

        claim_index = TermIndex(text)
        named = [t for t in subjects or [] if _is_subject(t) and claim_index.mentions(t)
                 and not (computed and t in _RESULT_WORDS)]
        evidence_index = TermIndex(cited_text + "\n" + "\n".join(evidence[e].label for e in known))
        if absent := [t for t in named if not evidence_index.mentions(t)]:
            result.valid = False
            result.problems.append(f"the cited evidence does not mention {', '.join(absent)}")
        elif unbound := _unbound_numbers(text, cited_text, named, allowed):
            result.valid, hard = False, True
            result.problems.append(f"{', '.join(unbound)} is not stated for {', '.join(named)} in the cited evidence")
        mismatched = [(w, v) for w, v in _variants(text) if w in asked and v != asked[w]]
        other = [_variant_name(w, v) for w, v in mismatched]
        if other:
            result.valid, hard = False, True
            wanted = ", ".join(dict.fromkeys(_variant_name(w, asked[w]) for w, _v in mismatched))
            result.problems.append(f"is about {', '.join(dict.fromkeys(other))}, not {wanted}")
        cited_versions = set().union(*(evidence[e].versions for e in known))
        if cited_versions and (wrong := _unsupported_versions(
                text, cited_versions, cited_text, evidence, [evidence[e] for e in known])):
            result.valid, hard = False, True
            result.problems.append(wrong)
        if _flips_polarity(text, cited_text, outcome=applied):
            result.valid, hard = False, True
            result.problems.append("reverses the negation of the source sentence")
        result.wording_only = not result.valid and not hard
        results.append(result)
    return results
