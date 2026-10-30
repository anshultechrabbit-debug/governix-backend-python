"""Which policies a question names, by any name a reader would use for one.

People rarely type a policy's full registered title. "Know Your Customer and Anti-Money
Laundering Policy" is asked about as "the KYC/AML policy", "AML policy" or "KAP v2";
"Home Loan Credit & Operations Policy" as "the home loan policy" or "HL policy". Every
alias here is derived from the policy's own data, never from a hand-kept list:

* the distinctive words of its title ("home loan", "personal loan"), when they pick out one
  policy and no other;
* the initials of a run of title words ("KYC", "AML", "HLP", "KYCAML");
* the code its documents declare on their cover ("Document ID: KAP-v3.0" -> "KAP");
* its registered policy or document number.

A mention found this way is "explicit" when a document word ("policy", "manual") or a version
label follows it ("Home Loan Policy v1.0", "KYC/AML Policy"). An explicit mention says which
document to search; its words are not part of what the question asks about. A bare mention
("the processing fee on a personal loan") still points to the policy but keeps its words as
the subject.
"""

import re
import threading
import time
import uuid
from dataclasses import dataclass, field

# Words that say what kind of document a title is, not what it is about.
DOCUMENT_WORDS = frozenset(
    "policy policies manual manuals document documents guideline guidelines guidance procedure procedures "
    "framework standard standards circular circulars directive directives handbook code sop rules".split()
)
CONNECTORS = frozenset("and of for the on in to a an &".split())
# A two-letter alias ("HL") is too easily an ordinary abbreviation: it only counts when a document
# word or a version label follows it ("HL policy", "PL v2").
MIN_FREE_ACRONYM = 3
MAX_ACRONYM = 8
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[/&][A-Za-z][A-Za-z0-9]*)*|&")
_VERSION_AFTER = re.compile(r"^\s*(?:v|version|edition)\s*\d", re.I)
# "Document ID: HLP-v3.0", "Policy No.: CRO/2024/7", "Ref. Code - KAP-2".
_DECLARED_CODE = re.compile(
    r"\b(?i:document|doc\.?|policy|circular|reference|ref\.?)\s*(?i:id|no\.?|number|code|ref\.?)\s*[:#\-]?\s*"
    r"([A-Z][A-Z0-9]{1,9})(?=[-/_ .]|$)"
)
CATALOGUE_TTL_SECONDS = 600
# A misspelt title word ("morgtage" for "mortgage") is read as the title word only this far from it, in
# edits (a swap of two neighbouring letters is one), and only in a word at least MIN_MISSPELT long.
MIN_MISSPELT = 5
LONG_WORD = 8


@dataclass(frozen=True)
class PolicyNames:
    policy_id: uuid.UUID
    name: str
    words: frozenset[str]          # distinctive title words, lower case
    acronyms: frozenset[str]       # upper case: initials of title runs, declared codes, numbers
    # Connectors inside the title ("and" in "Know Your Customer and Anti-Money Laundering"): only
    # these may join title words in a question; "in the" between two mentions does not.
    connectors: frozenset[str] = frozenset()


@dataclass
class Mention:
    policy_id: uuid.UUID
    start: int
    end: int
    explicit: bool
    text: str = ""


def _title_words(name: str) -> list[str]:
    """ "Know Your Customer and Anti-Money Laundering Policy" -> [know, your, customer, and, anti, money, laundering, policy]."""
    return [w.lower() for w in re.findall(r"[A-Za-z0-9]+|&", name)]


def _initials(words: list[str]) -> set[str]:
    """Initials of every run of two or more title words, connectors skipped: KY, KYC, YC, ..., AML, KYCAML."""
    content = [w for w in words if w not in CONNECTORS]
    found = set()
    for start in range(len(content)):
        for end in range(start + 2, min(len(content), start + MAX_ACRONYM) + 1):
            found.add("".join(w[0] for w in content[start:end]).upper())
    return found


def _number_prefix(number: str | None) -> set[str]:
    """ "HLP-2024-01" -> {"HLP-2024-01", "HLP"}."""
    if not number:
        return set()
    number = number.strip().upper()
    prefix = re.match(r"[A-Z]{2,10}", number)
    return {number} | ({prefix.group(0)} if prefix else set())


def build_names(policy_id: uuid.UUID, name: str, *, numbers: list[str | None] = (),
                front_matter: list[str] = ()) -> PolicyNames:
    words = _title_words(name)
    distinctive = frozenset(w for w in words if w not in CONNECTORS and w not in DOCUMENT_WORDS)
    acronyms = _initials(words)
    for number in numbers:
        acronyms |= _number_prefix(number)
    for text in front_matter:
        acronyms |= {m.group(1) for m in _DECLARED_CODE.finditer(text)}
    return PolicyNames(policy_id, name, distinctive, frozenset(acronyms), frozenset(words) & CONNECTORS)


def _followed_by_document_word_or_version(question: str, end: int) -> tuple[bool, int]:
    """A document word ("policy") or a version label right after a name makes it a reference."""
    rest = question[end:]
    word = re.match(r"\s+([A-Za-z]+)", rest)
    if word and word.group(1).lower() in DOCUMENT_WORDS:
        return True, end + word.end()
    return bool(_VERSION_AFTER.match(rest)), end


def _edits(a: str, b: str) -> int:
    """Edits between two words: insertions, deletions, substitutions and swaps of neighbouring letters."""
    previous2: list[int] = []
    previous = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        row = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            row[j] = min(previous[j] + 1, row[j - 1] + 1, previous[j - 1] + (a[i - 1] != b[j - 1]))
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                row[j] = min(row[j], previous2[j - 2] + 1)
        previous2, previous = previous, row
    return previous[-1]


def _spelled(tokens: list[tuple[str, int, int]], catalogue: list[PolicyNames]) -> dict[int, str]:
    """Tokens that are a misspelling of exactly one title word of the catalogue ("morgtage" -> "mortgage")."""
    title_words = {w for policy in catalogue for w in policy.words}
    corrected = {}
    for index, (token, _start, _end) in enumerate(tokens):
        lowered = token.lower()
        if len(lowered) < MIN_MISSPELT or lowered in title_words or lowered in DOCUMENT_WORDS or lowered in CONNECTORS:
            continue
        allowed = 2 if len(lowered) >= LONG_WORD else 1
        close = {w for w in title_words if abs(len(w) - len(lowered)) <= allowed and _edits(lowered, w) <= allowed}
        if len(close) == 1:
            corrected[index] = close.pop()
    return corrected


def find_mentions(question: str, catalogue: list[PolicyNames]) -> list[Mention]:
    """Spans of the question that name a policy, each resolved to exactly one policy.

    A misspelt title word counts only in an explicit reference ("morgtage policy"), where the document word
    says a policy is being named."""
    tokens = [(m.group(0), m.start(), m.end()) for m in _WORD.finditer(question)]
    corrected = _spelled(tokens, catalogue)
    mentions: list[Mention] = []
    taken: set[int] = set()

    # 1. Initials and codes: "KYC/AML", "AML", "HLP", "HL policy".
    for index, (token, start, end) in enumerate(tokens):
        parts = [p for p in re.split(r"[/&]", token) if p]
        owners = []
        for part in parts:
            matches = [p for p in catalogue if part.upper() in p.acronyms]
            owners.append(matches)
        if not owners or not all(len(m) == 1 for m in owners) or len({m[0].policy_id for m in owners}) != 1:
            continue
        explicit, span_end = _followed_by_document_word_or_version(question, end)
        upper = all(p.isupper() for p in parts)
        short = min(len(p) for p in parts) < MIN_FREE_ACRONYM
        if not explicit and (short or not upper):
            continue  # "hl", "kyc" in running text: only as a reference ("kyc policy")
        mentions.append(Mention(owners[0][0].policy_id, start, span_end, explicit))
        taken.add(index)

    # 2. Title words: the longest run of consecutive words that belong to one policy's title only.
    index = 0
    while index < len(tokens):
        if index in taken:
            index += 1
            continue
        best: tuple[int, PolicyNames] | None = None
        for policy in catalogue:
            length = 0
            for offset, (token, _start, _end) in enumerate(tokens[index:]):
                lowered = corrected.get(index + offset, token.lower())
                if lowered in policy.words:
                    length += 1
                elif lowered in policy.connectors and length:
                    length += 1
                else:
                    break
            while length and tokens[index + length - 1][0].lower() in CONNECTORS:
                length -= 1
            run = {corrected.get(i, tokens[i][0].lower()) for i in range(index, index + length)} - CONNECTORS
            if len(run) < 2 and not (run and _followed_by_document_word_or_version(question, tokens[index + length - 1][2])[0]):
                continue
            if best is None or length > best[0]:
                best = (length, policy)
            elif length == best[0]:
                best = (length, None)  # two policies share these words: not a reference to either
        if best and best[1] is not None:
            length, policy = best
            run = {corrected.get(i, tokens[i][0].lower()) for i in range(index, index + length)} - CONNECTORS
            end = tokens[index + length - 1][2]
            explicit, span_end = _followed_by_document_word_or_version(question, end)
            if not explicit and any(i in corrected for i in range(index, index + length)):
                index += 1
                continue  # a misspelling is read as a title word only where a policy is named
            # The same words must not also be the whole distinctive part of another title, and a bare
            # mention must carry at least half of the title ("home loan", not "your customer").
            unique = not any(other is not policy and run <= other.words for other in catalogue)
            if unique and (explicit or len(run) * 2 >= len(policy.words)):
                mentions.append(Mention(policy.policy_id, tokens[index][1], span_end, explicit))
                index += length
                continue
        index += 1
    for mention in mentions:
        mention.text = question[mention.start:mention.end]
    return sorted(mentions, key=lambda m: m.start)


def without_references(question: str, mentions: list[Mention]) -> str:
    """The question without the explicit references: the words that remain are what it asks about."""
    for mention in sorted((m for m in mentions if m.explicit), key=lambda m: m.start, reverse=True):
        question = question[:mention.start] + " " + question[mention.end:]
    return " ".join(question.split())


@dataclass
class _Entry:
    expires: float
    catalogue: list[PolicyNames] = field(default_factory=list)


_CACHE: dict[uuid.UUID, _Entry] = {}
_LOCK = threading.Lock()


def cached_catalogue(organization_id: uuid.UUID, load) -> list[PolicyNames]:
    """The organisation's policy names, rebuilt at most every CATALOGUE_TTL_SECONDS."""
    now = time.monotonic()
    with _LOCK:
        entry = _CACHE.get(organization_id)
        if entry and entry.expires > now:
            return entry.catalogue
    catalogue = load()
    with _LOCK:
        _CACHE[organization_id] = _Entry(now + CATALOGUE_TTL_SECONDS, catalogue)
    return catalogue
