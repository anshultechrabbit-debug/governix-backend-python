"""Deterministic metadata detection from a document's opening pages.

Everything here is a *detection* with its evidence and confidence; nothing is
treated as confirmed until a person accepts it.
"""

import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date

from app.modules.ingestion.analysis.dates import DATE_FRAGMENT, MONTH_YEAR, MONTHS, parse_date
from app.modules.ingestion.structure import is_page_number

_ID = r"([A-Z0-9][A-Z0-9/\-_.]{1,40}[A-Z0-9])"
POLICY_NUMBER = re.compile(rf"\bpolicy\s*(?:no|number|ref(?:erence)?|code|id)\b\.?\s*[:#\-]?\s*{_ID}", re.I)
DOCUMENT_NUMBER = re.compile(
    rf"\b(?:document|doc|ref(?:erence)?)\s*(?:no|number)\b\.?\s*[:#\-]?\s*{_ID}", re.I
)
CIRCULAR_NUMBER = re.compile(rf"\bcircular\s*(?:no|number)\b\.?\s*[:#\-]?\s*{_ID}", re.I)
VERSION = re.compile(
    r"\bversion\s*(?:no\.?|number)?\s*[:\-]?\s*v?(\d{1,3}(?:\.\d{1,3})?)\b|\bv(\d{1,3}(?:\.\d{1,3})?)\b",
    re.I,
)
REVISION = re.compile(r"\b(?:revision|rev\.?)\s*(?:no\.?|number)?\s*[:\-]?\s*(\d{1,3})\b", re.I)
EFFECTIVE = re.compile(
    rf"\b(?:effective\s*(?:from|date|on|w\.?e\.?f\.?)?|with\s+effect\s+from|w\.?\s?e\.?\s?f\.?)\s*[:\-]?\s*(?:from\s+)?({DATE_FRAGMENT})",
    re.I,
)
ISSUE_DATE = re.compile(
    rf"(?:\bdated|\bdate\s+of\s+issue|\bissue\s+date|\bissued\s+on|^\s*date)\s*[:\-]?\s*({DATE_FRAGMENT})",
    re.I | re.M,
)
ISSUER = re.compile(r"\b(?:issued\s+by|issuing\s+(?:authority|department)|owner)\s*[:\-]\s*([^\n]{2,100})", re.I)
DEPARTMENT = re.compile(r"\b(?:department|dept\.?)\s*[:\-]\s*([^\n]{2,80})", re.I)
DEPARTMENT_NAME = re.compile(r"\b([A-Z][A-Za-z&]+(?:\s+[A-Z][A-Za-z&]+){0,3}\s+Department)\b")

AMEND_CLAUSE = re.compile(
    r"(?:clause|section|para(?:graph)?|point)s?\s+(?P<clauses>[\d.]+(?:\s*(?:,|and|&)\s*[\d.]+)*)\s+of\s+(?:the\s+)?"
    r"(?P<target>[^.;\n]{3,120}?)\s+(?:is|are|stands?|shall\s+stand|has\s+been|have\s+been)\s+(?:hereby\s+)?"
    r"(?P<verb>amended|modified|replaced|substituted|deleted|withdrawn|revised)",
    re.I,
)
SUPERSEDES = re.compile(
    r"\b(?:this\s+\w+\s+)?(?P<verb>supersedes|replaces|stands\s+withdrawn|in\s+supersession\s+of)\s+(?:the\s+)?"
    r"(?P<target>[^.;\n]{3,120})",
    re.I,
)
MODIFIES = re.compile(r"\bin\s+(?:partial\s+)?modification\s+of\s+(?:the\s+)?(?P<target>[^.;\n]{3,120})", re.I)
CLARIFIES = re.compile(r"\b(?:clarif(?:y|ies|ication)\s+(?:on|regarding|to)|with\s+reference\s+to)\s+(?:the\s+)?(?P<target>[^.;\n]{3,120})", re.I)

DOC_TYPE_WORDS = re.compile(
    r"\b(policy|circular|guidelines?|manual|procedure|sop|notice|framework|faqs?|directions?|handbook|process)\b",
    re.I,
)
FILENAME_LIKE = re.compile(r"(_|\.pdf$|\.docx?$|\bfinal\b|\bdraft\b|\bcopy\b|\bv\d+\b|^untitled|^microsoft|^document\d*$)", re.I)
# "VOLUME II – TRANSMISSION", "Part B": a subtitle that belongs to the title it follows.
SUBTITLE = re.compile(r"^(?:volume|vol\.?|part|book|tome)\s+(?:[ivxlc]+|\d{1,3}|[a-z])\b", re.I)
# Font sizes within this ratio of the largest are one title ("NATIONAL" 38.0, "PLAN" 38.1).
TITLE_SIZE_TOLERANCE = 0.97
LOWER_WORDS = {"a", "an", "and", "as", "at", "by", "for", "in", "of", "on", "or", "the", "to", "with", "from"}


@dataclass
class Detected:
    value: str | None = None
    confidence: float = 0.0
    source: str | None = None
    evidence: str | None = None


@dataclass
class AmendmentRef:
    relation_type: str  # AMENDS | SUPERSEDES | REPLACES | CLARIFIES
    target_text: str
    clauses: list[str] = field(default_factory=list)
    sentence: str = ""


@dataclass
class DocumentMetadata:
    title: Detected = field(default_factory=Detected)
    policy_number: Detected = field(default_factory=Detected)
    document_number: Detected = field(default_factory=Detected)
    circular_number: Detected = field(default_factory=Detected)
    version_label: Detected = field(default_factory=Detected)
    revision_number: Detected = field(default_factory=Detected)
    effective_date: date | None = None
    effective_date_evidence: str | None = None
    issue_date: date | None = None
    issuer: Detected = field(default_factory=Detected)
    department: Detected = field(default_factory=Detected)
    amendments: list[AmendmentRef] = field(default_factory=list)
    referenced_policy_numbers: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        data = asdict(self)
        data["effective_date"] = self.effective_date.isoformat() if self.effective_date else None
        data["issue_date"] = self.issue_date.isoformat() if self.issue_date else None
        return data


ACRONYMS = {
    "KYC", "AML", "CFT", "NRI", "NRE", "NRO", "FCNR", "LTV", "RBI", "SEBI", "IRDAI", "SOP", "FAQ",
    "NPA", "MSME", "EMI", "CIBIL", "CRR", "SLR", "NBFC", "PMLA", "CTR", "STR", "FEMA", "ECB", "GST",
    "PAN", "TDS", "ATM", "UPI", "NEFT", "RTGS", "IMPS", "POS", "CASA", "FD", "RD", "HO", "IT", "HR",
    "CSR", "ESG", "PSL", "EWS", "LIG", "MIG", "PMAY", "KCC", "SHG", "JLG", "MFI", "ALM", "ICAAP",
    "IFRS", "IND", "AS", "VAR", "NII", "ROA", "ROE", "CEO", "CFO", "CRO", "CCO", "BCP", "DR", "ISMS",
}


def smart_title_case(text: str) -> str:
    """'HOME LOAN CREDIT POLICY' -> 'Home Loan Credit Policy'; keeps acronyms (KYC, NRI)."""
    words = text.split()
    if not words or not text.isupper():
        return " ".join(words)
    result = []
    for index, word in enumerate(words):
        bare = re.sub(r"[^A-Za-z]", "", word)
        if re.fullmatch(r"[IVXLC]{1,5}", bare) and bare not in {"I", "C", "L"} or bare.upper() in ACRONYMS or (2 <= len(bare) <= 5 and not re.search(r"[AEIOU]", bare)):
            result.append(word)
        elif index and bare.lower() in LOWER_WORDS:
            result.append(word.lower())
        else:
            result.append(word[:1].upper() + word[1:].lower())
    return " ".join(result)


def normalize_title(text: str) -> str:
    """Canonical form for matching: lowercase, no punctuation, no version/date noise."""
    text = text.lower()
    text = re.sub(r"\b(v|ver|version|rev|revision)\s*\.?\s*\d+(\.\d+)*\b", " ", text)
    text = re.sub(r"\b(19|20)\d{2}\b", " ", text)
    text = re.sub(r"\b(final|draft|copy|updated|new|revised|amended)\b", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _clean_id(value: str) -> str | None:
    value = value.strip(" .:-").upper()
    return value if re.search(r"\d", value) and len(value) >= 3 else None


def _sentence_around(text: str, start: int, end: int) -> str:
    left = max(text.rfind(".", 0, start), text.rfind("\n", 0, start)) + 1
    right_candidates = [i for i in (text.find(".", end), text.find("\n", end)) if i != -1]
    right = min(right_candidates) + 1 if right_candidates else len(text)
    return " ".join(text[left:right].split())[:400]


def extract_metadata(
    first_page_lines: list[list],
    opening_text: str,
    body_font_size: float,
    pdf_title: str | None,
    date_order: str = "DMY",
) -> DocumentMetadata:
    """Detect identity metadata.

    first_page_lines: layout lines of page 1 ([block, text, size, bold, y0]).
    opening_text: text of the first few pages (and front matter).
    """
    meta = DocumentMetadata()
    meta.title = detect_title(first_page_lines, opening_text, body_font_size, pdf_title)

    if match := POLICY_NUMBER.search(opening_text):
        if value := _clean_id(match[1]):
            meta.policy_number = Detected(value, 0.95, "text", match[0])
    if match := DOCUMENT_NUMBER.search(opening_text):
        if value := _clean_id(match[1]):
            meta.document_number = Detected(value, 0.9, "text", match[0])
    if match := CIRCULAR_NUMBER.search(opening_text):
        if value := _clean_id(match[1]):
            meta.circular_number = Detected(value, 0.9, "text", match[0])
    if match := VERSION.search(opening_text):
        meta.version_label = Detected(match[1] or match[2], 0.85, "text", match[0])
    if match := REVISION.search(opening_text):
        meta.revision_number = Detected(match[1], 0.85, "text", match[0])

    if match := EFFECTIVE.search(opening_text):
        meta.effective_date = parse_date(match[1], date_order)
        meta.effective_date_evidence = " ".join(match[0].split())
    if match := ISSUE_DATE.search(opening_text):
        meta.issue_date = parse_date(match[1], date_order)
    if meta.issue_date is None:
        meta.issue_date = cover_date(first_page_lines, date_order)
    if meta.effective_date is None and meta.issue_date is not None:
        # A notified plan or circular with no separate effective date is in force
        # from publication; this stays a detection the person confirms.
        meta.effective_date = meta.issue_date
        meta.effective_date_evidence = f"Publication date on the cover ({meta.issue_date.isoformat()})"

    if match := ISSUER.search(opening_text):
        issuer = match[1].strip(" .:-")
        meta.issuer = Detected(issuer[:100], 0.8, "text", match[0])
        if dept := DEPARTMENT_NAME.search(issuer):
            meta.department = Detected(dept[1], 0.8, "issuer", match[0])
    if not meta.department.value:
        if match := DEPARTMENT.search(opening_text):
            meta.department = Detected(match[1].strip(" .:-")[:80], 0.7, "text", match[0])
        elif match := DEPARTMENT_NAME.search(opening_text):
            meta.department = Detected(match[1], 0.5, "text", match[0])

    meta.amendments = detect_amendments(opening_text)
    own = meta.policy_number.value
    meta.referenced_policy_numbers = sorted({
        v for m in POLICY_NUMBER.finditer(opening_text)
        if (v := _clean_id(m[1])) and v != own
    })
    return meta


def cover_date(first_page_lines: list[list], date_order: str = "DMY") -> date | None:
    """A cover line that is only a date ("OCTOBER 2024", "1 July 2025") is the publication date."""
    for line in first_page_lines[:40]:
        text = " ".join(line[1].split()).strip(" ,.")
        if not text or len(text) > 30:
            continue
        if (parsed := parse_date(text, date_order)) is not None:
            return parsed
        if (match := MONTH_YEAR.fullmatch(text)) is not None:
            return date(int(match["year"]), MONTHS[match["month"].lower()], 1)
    return None


def detect_title(
    first_page_lines: list[list], opening_text: str, body_font_size: float, pdf_title: str | None
) -> Detected:
    candidates: dict[str, tuple[float, str, str]] = {}

    def consider(text: str, score: float, source: str) -> None:
        text = " ".join(text.split()).strip(" :-")
        if not (3 <= len(text) <= 150) or not re.search(r"[A-Za-z]{3}", text):
            return
        if POLICY_NUMBER.search(text) or VERSION.fullmatch(text) or is_page_number(text) or EFFECTIVE.search(text):
            return
        if DOC_TYPE_WORDS.search(text):
            score += 0.2
        if text.isupper() or text.istitle():
            score += 0.1
        key = normalize_title(text)
        if not key:
            return
        if key not in candidates or candidates[key][0] < score:
            candidates[key] = (score, text, source)

    lines = [line for line in first_page_lines[:40] if line[1].strip()]
    if lines:
        max_size = max(line[2] for line in lines)
        if max_size and (not body_font_size or max_size >= body_font_size * 1.15):
            # Consecutive lines at the largest size form one title ("HOME LOAN / CREDIT POLICY").
            block: list[str] = []
            subtitle = None
            for index, line in enumerate(lines):
                if line[2] >= max_size * TITLE_SIZE_TOLERANCE:
                    block.append(line[1])
                elif block:
                    subtitle = line[1] if SUBTITLE.match(line[1].strip()) else None
                    break
            title = " ".join(block)
            if subtitle:
                title = f"{title} – {subtitle.strip()}"
            consider(title, 0.55, "largest_font")
        for index, line in enumerate(lines[:15]):
            if DOC_TYPE_WORDS.search(line[1]) and len(line[1].split()) <= 12 and not line[1].rstrip().endswith("."):
                consider(line[1], 0.35, "heading_keyword")
                if joined := _continued_title(lines, index):
                    consider(joined, 0.45, "heading_keyword")

    if pdf_title and not FILENAME_LIKE.search(pdf_title):
        consider(pdf_title, 0.4, "pdf_metadata")

    if not candidates:
        return Detected(None, 0.0, None, None)

    # Titles repeated in the opening pages (running headers, cover + first page) are stronger.
    normalized_opening = normalize_title(opening_text)
    scored = []
    for key, (score, text, source) in candidates.items():
        if normalized_opening.count(key) >= 2:
            score += 0.1
        if pdf_title and normalize_title(pdf_title) == key:
            score += 0.15
        scored.append((score, text, source))
    score, text, source = max(scored)
    repaired = repair_title(text, f"{opening_text}\n{pdf_title or ''}")
    return Detected(smart_title_case(repaired), round(min(score, 0.99), 2), source, text)


def _continued_title(lines: list[list], index: int) -> str | None:
    """A title set over several lines: "Policy on" / "'Microfinance Loans'".

    A line ending in a connecting word is unfinished; it continues on the next
    lines set in the same size and weight (at most three).
    """
    size, bold = lines[index][2], lines[index][3]
    parts = [lines[index][1].strip()]
    for line in lines[index + 1:index + 4]:
        if parts[-1].split()[-1].lower() not in LOWER_WORDS:
            break
        if not size or abs(line[2] - size) > size * (1 - TITLE_SIZE_TOLERANCE) or line[3] != bold:
            return None
        piece = line[1].strip().strip("‘’“”\"'")
        if not piece:
            return None
        parts.append(piece)
    return " ".join(parts) if len(parts) > 1 and parts[-1].split()[-1].lower() not in LOWER_WORDS else None


def repair_title(title: str, text: str) -> str:
    """Mend title words whose letters the text layer lost.

    Covers set in display fonts sometimes draw a letter as a graphic, so the text
    reads "OUR CODE OF E HICS" for "OUR CODE OF ETHICS". A run of words the document
    never uses outside its title is replaced by a close spelling the document does
    use; words it uses are left alone, so a correct title never changes.
    """
    vocabulary = Counter(w.lower() for w in re.findall(r"[A-Za-z]+", text))
    in_title = Counter(w.lower() for w in re.findall(r"[A-Za-z]+", title))

    def lost(word: str) -> bool:
        bare = re.sub(r"[^A-Za-z]", "", word).lower()
        if not bare or bare in LOWER_WORDS or bare.upper() in ACRONYMS or bare in {"a", "i"}:
            return False
        return len(bare) == 1 or vocabulary[bare] <= in_title[bare]

    words, mended, i = title.split(), [], 0
    while i < len(words):
        end = i
        while end < len(words) and lost(words[end]):
            end += 1
        if end == i:
            mended.append(words[i])
            i += 1
            continue
        run = words[i:end]
        # The whole run first ("E HICS" -> "ETHICS"), then each word on its own.
        whole = _close_word("".join(run), vocabulary)
        mended.extend([whole] if whole else [_close_word(word, vocabulary) or word for word in run])
        i = end
    return " ".join(mended)


def _close_word(broken: str, vocabulary: Counter) -> str | None:
    """The document's most used word that `broken` is, with one letter lost (two for long words), in its case.

    Only longer words qualify: a lost glyph shortens a word, while a same-length
    substitution ("manual"/"annual") or a plural ("loan"/"loans") is a different word.
    """
    bare = re.sub(r"[^A-Za-z]", "", broken)
    if len(bare) < 3:
        return None
    limit = 2 if len(bare) >= 8 else 1
    target = bare.lower()
    found = [
        (count, word) for word, count in vocabulary.items()
        if count >= 2 and len(target) < len(word) <= len(target) + limit
        and word not in (f"{target}s", f"{target}es") and _edits(target, word) <= limit
    ]
    if not found:
        return None
    word = max(found)[1]
    return word.upper() if bare.isupper() else word.title() if bare[0].isupper() else word


def _edits(a: str, b: str) -> int:
    """Levenshtein distance."""
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


def fallback_title(opening_text: str, category_name: str | None) -> Detected:
    """A generated name when no title is detectable: frequent phrase + document type."""
    words = [w for w in re.findall(r"[A-Za-z]{3,}", opening_text.lower()) if w not in LOWER_WORDS]
    bigrams = Counter(zip(words, words[1:]))
    if not bigrams:
        return Detected(None, 0.0, None, None)
    (a, b), _ = bigrams.most_common(1)[0]
    kind = (category_name or "Document").rstrip("s")
    return Detected(f"{a.title()} {b.title()} {kind}", 0.3, "generated", None)


def detect_amendments(text: str) -> list[AmendmentRef]:
    refs: list[AmendmentRef] = []
    seen: set[tuple[str, str]] = set()

    def add(ref: AmendmentRef) -> None:
        key = (ref.relation_type, normalize_title(ref.target_text))
        if key not in seen and key[1]:
            seen.add(key)
            refs.append(ref)

    for match in AMEND_CLAUSE.finditer(text):
        clauses = re.findall(r"\d+(?:\.\d+)*", match["clauses"])
        verb = match["verb"].lower()
        relation = "REPLACES" if verb in ("replaced", "substituted") else "AMENDS"
        add(AmendmentRef(relation, match["target"].strip(), clauses, _sentence_around(text, match.start(), match.end())))
    for match in SUPERSEDES.finditer(text):
        verb = match["verb"].lower()
        relation = "REPLACES" if verb == "replaces" else "SUPERSEDES"
        add(AmendmentRef(relation, _trim_target(match["target"]), [], _sentence_around(text, match.start(), match.end())))
    for match in MODIFIES.finditer(text):
        add(AmendmentRef("AMENDS", _trim_target(match["target"]), [], _sentence_around(text, match.start(), match.end())))
    for match in CLARIFIES.finditer(text):
        add(AmendmentRef("CLARIFIES", _trim_target(match["target"]), [], _sentence_around(text, match.start(), match.end())))
    return refs[:20]


def _trim_target(text: str) -> str:
    text = re.split(r",|\s+(?:dated|issued|with effect|w\.e\.f|and\s+all)\b", text, maxsplit=1, flags=re.I)[0]
    return text.strip(" ,:-")
