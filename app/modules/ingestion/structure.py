"""Layout-aware section detection.

Two streaming passes over the pages, so memory stays flat for 100,000-page PDFs:

1. `LayoutStats` learns the body font size and repeated header/footer lines.
2. `StructureBuilder` turns lines into paragraphs and heading-delimited
   sections, emitting each section as soon as it closes.

Headings are recognised from numbering ("5.2 LTV for High Value Loans"),
keywords ("Chapter IV", "Annexure A") and typography (larger or bold,
short, not sentence-like). Everything is deterministic.
"""

import re
import uuid
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field

from app.modules.ingestion.extraction import is_table_block
from app.modules.ingestion.text import ContentFingerprint, sha256_text

MAX_SECTION_CHARS = 200_000
EDGE_LINES = 2
MIN_PAGES_FOR_BOILERPLATE = 4

_NUMBERED = re.compile(r"^(?P<num>\d{1,2}(?:\.\d{1,3}){0,5})(?:\.|\))?\s+(?P<title>\S.*)$")
_KEYWORD = re.compile(
    # "Chapter 3", "Chapter - 3", "CHAPTER: IV", "Annex 5.1"
    r"^(?P<kw>chapter|section|part|annexure|annex|appendix|schedule|article)(?:\s*[-–—:.]\s*|\s+)"
    r"(?P<num>\d{1,3}(?:\.\d{1,3}){0,3}|[ivxlcdm]{1,6}|[a-z])\b[\s:.\-–—]*(?P<title>.*)$",
    re.IGNORECASE,
)
_BARE_NUMBER = re.compile(r"^\d{1,2}(?:\.\d{1,3}){1,5}\.?$")
SAME_ROW_TOLERANCE = 3.0
_TOP_LEVEL_KEYWORDS = {"chapter", "part", "annexure", "annex", "appendix", "schedule"}
_PAGE_NUMBER = re.compile(r"^(?:page\s*)?[\d#]+(?:\s*(?:of|/)\s*[\d#]+)?$|^[-–]\s*\d+\s*[-–]$", re.I)
_SENTENCE_WORDS = re.compile(r"\b(shall|must|will|should|may|is|are|was|were|has|have)\b", re.I)


# A standalone number ("Page 12 of 50") varies per page; digits inside an
# identifier ("BNK-126", "v4.0", "2,487") are content and must stay distinct.
_STANDALONE_NUMBER = re.compile(r"(?<![\w.,\-])\d+(?![\w.,\-])")


def _edge_key(text: str) -> str:
    return " ".join(_STANDALONE_NUMBER.sub("#", text.lower()).split())


def is_page_number(text: str) -> bool:
    return bool(_PAGE_NUMBER.match(text.strip()))


def edge_indices(count: int) -> set[int]:
    """Positions of a page's outermost lines, where running headers/footers sit."""
    if count > EDGE_LINES * 2:
        return set(range(EDGE_LINES)) | set(range(count - EDGE_LINES, count))
    return {0, count - 1} if count else set()  # short page: only its outermost lines


@dataclass
class LayoutStats:
    size_chars: Counter = field(default_factory=Counter)
    edge_lines: Counter = field(default_factory=Counter)
    pages: int = 0
    body_size: float = 0.0
    boilerplate: frozenset[str] = frozenset()

    def observe(self, lines: list[list]) -> None:
        self.pages += 1
        for _block, text, size, _bold, _y in lines:
            if size:
                self.size_chars[size] += len(text)
        for key in {_edge_key(lines[i][1]) for i in edge_indices(len(lines))}:
            self.edge_lines[key] += 1

    def finalize(self) -> "LayoutStats":
        if self.size_chars:
            self.body_size = self.size_chars.most_common(1)[0][0]
        if self.pages >= MIN_PAGES_FOR_BOILERPLATE:
            threshold = max(3, self.pages * 0.5)
            self.boilerplate = frozenset(k for k, c in self.edge_lines.items() if c >= threshold)
        return self


@dataclass(frozen=True)
class Heading:
    level: int
    number: str | None
    title: str
    numbered: bool

    @property
    def label(self) -> str:
        if not self.number or self.title == self.number:
            return self.title
        return f"{self.number} {self.title}".strip()


def classify_heading(text: str, size: float, bold: bool, body_size: float) -> Heading | None:
    text = " ".join(text.split())
    if len(text) < 2 or len(text) > 160:
        return None
    larger = bool(body_size) and size >= body_size * 1.08
    emphasised = bold or larger

    match = _KEYWORD.match(text)
    if match and not _is_reference(match["title"]):
        keyword, number = match["kw"].lower(), match["num"].upper()
        title = match["title"].strip(" :-") or f"{keyword.title()} {number}"
        if len(title) <= 120 and (emphasised or not title.endswith(".")):
            level = 1 if keyword in _TOP_LEVEL_KEYWORDS else 2
            return Heading(level, f"{keyword.title()} {number}", title, True)

    match = _NUMBERED.match(text)
    if match:
        number, title = match["num"], match["title"].strip()
        if any(int(part) > 99 for part in number.split(".")) or not _looks_like_title(title):
            return None
        # A bare "1 Includes 60,207 MW..." in body type is a footnote or list
        # item; a top-level number opens a section only when set off as a heading.
        plain_ok = "." in number and not title.endswith(".") and len(title) <= 80 and not _is_sentence(title)
        if emphasised or plain_ok:
            return Heading(number.count(".") + 1, number, title.rstrip(":"), True)
        return None

    if not emphasised or not _looks_like_title(text) or text.endswith("."):
        return None
    if len(text) > 100 or len(text.split()) > 12 or _is_sentence(text):
        return None
    if body_size and size >= body_size * 1.25:
        return Heading(1, None, text.rstrip(":"), False)
    return Heading(2 if larger else 3, None, text.rstrip(":"), False)


def _is_reference(title: str) -> bool:
    """A citation of a provision, not a heading: "Section 3(4) of the Act", "section 3 of the Act"."""
    title = title.strip()
    return bool(title) and (title[0] in "([" or title[0].islower() or title.endswith("]"))


def _looks_like_title(text: str) -> bool:
    if len(text) > 120 or text.endswith((",", ";")) or not re.search(r"[A-Za-z]", text):
        return False
    return text[0].isupper() or text[0].isdigit() or text[0] in "(\"'"


def _is_sentence(text: str) -> bool:
    return len(text.split()) > 6 and bool(_SENTENCE_WORDS.search(text))


@dataclass
class SectionDraft:
    id: uuid.UUID
    parent_id: uuid.UUID | None
    order_index: int
    level: int
    number: str | None
    title: str
    path: str
    page_start: int
    page_end: int
    paragraphs: list[str] = field(default_factory=list)
    page_marks: list[list[int]] = field(default_factory=list)
    length: int = 0

    @property
    def content(self) -> str:
        return "\n\n".join(self.paragraphs)

    @property
    def content_hash(self) -> str:
        return sha256_text(f"{self.title}\n{self.content}")

    def add_paragraph(self, text: str, page_number: int) -> None:
        offset = self.length + (2 if self.paragraphs else 0)
        if not self.page_marks or self.page_marks[-1][1] != page_number:
            self.page_marks.append([offset, page_number])
        self.paragraphs.append(text)
        self.length = offset + len(text)
        self.page_end = max(self.page_end, page_number)


@dataclass
class _OpenHeading:
    level: int
    id: uuid.UUID
    path: str


class StructureBuilder:
    def __init__(self, stats: LayoutStats) -> None:
        self.stats = stats
        self.fingerprint = ContentFingerprint()
        self._stack: list[_OpenHeading] = []
        self._current: SectionDraft | None = None
        self._order = 0
        self._seen_structured_heading = False
        self._paragraph: list[str] = []
        self._paragraph_block: int | None = None
        self._paragraph_page = 0
        # Heading text is not part of any paragraph, so it would never reach a
        # chunk: "What is Chapter 5 about?" could not find "Chapter 5 Analysis
        # and Studies for 2026-27". Labels of headings opened since the last
        # paragraph are written at the start of the next paragraph's section.
        self._pending_labels: list[str] = []
        self.section_count = 0

    def add_page(self, page_number: int, lines: list[list]) -> Iterator[SectionDraft]:
        lines = merge_numbered_rows(lines)
        table_blocks = _table_blocks(lines)
        edges = edge_indices(len(lines))
        for index, (block, text, size, bold, _y) in enumerate(lines):
            text = text.strip()
            # Only a line at the page edge is a running header/footer; the same
            # words elsewhere ("CENTRAL ELECTRICITY AUTHORITY" on the cover) are content.
            if not text or is_page_number(text) or (index in edges and _edge_key(text) in self.stats.boilerplate):
                continue
            # A two-column table often uses bold labels in the left column.  Those
            # labels look like headings line-by-line, but opening a section for
            # each one leaves the table values outside searchable content.
            heading = None if (block in table_blocks or is_table_block(block)) else classify_heading(
                text, size, bold, self.stats.body_size
            )
            if heading and not heading.numbered and not self._seen_structured_heading and page_number == 1:
                heading = None  # document title block on the cover stays in the front matter
            if heading:
                yield from self._flush_paragraph()
                yield from self._open_section(heading, page_number)
                self.fingerprint.update(heading.label)
                self._pending_labels.append(heading.label)
                continue
            if self._paragraph and (block != self._paragraph_block or page_number != self._paragraph_page):
                yield from self._flush_paragraph()
            self._paragraph.append(text)
            self._paragraph_block, self._paragraph_page = block, page_number
        # Paragraphs do not span pages, so every section's page marks stay exact.
        yield from self._flush_paragraph()

    def finish(self) -> Iterator[SectionDraft]:
        yield from self._flush_paragraph()
        if self._current is not None:
            yield self._close()

    # --- internals -------------------------------------------------------------

    def _flush_paragraph(self) -> Iterator[SectionDraft]:
        if not self._paragraph:
            return
        text = _join_lines(self._paragraph)
        page = self._paragraph_page
        self._paragraph = []
        self.fingerprint.update(text)
        if self._current is None:
            self._current = self._new_section(Heading(0, None, "Front matter", False), page, None, "Front matter")
        elif self._current.length + len(text) > MAX_SECTION_CHARS and self._current.paragraphs:
            yield from self._continue_section(page)
        if self._pending_labels:
            self._current.add_paragraph("\n".join(dict.fromkeys(self._pending_labels)), page)
            self._pending_labels = []
        self._current.add_paragraph(text, page)

    def _open_section(self, heading: Heading, page_number: int) -> Iterator[SectionDraft]:
        if self._current is not None:
            yield self._close()
        if heading.numbered:
            self._seen_structured_heading = True
        while self._stack and self._stack[-1].level >= heading.level:
            self._stack.pop()
        parent = self._stack[-1] if self._stack else None
        path = f"{parent.path} > {heading.label}" if parent else heading.label
        self._current = self._new_section(heading, page_number, parent.id if parent else None, path)
        self._stack.append(_OpenHeading(heading.level, self._current.id, path))

    def _continue_section(self, page_number: int) -> Iterator[SectionDraft]:
        previous = self._current
        yield self._close()
        title = previous.title if previous.title.endswith("(continued)") else f"{previous.title} (continued)"
        self._current = SectionDraft(
            id=uuid.uuid4(), parent_id=previous.parent_id, order_index=self._next_order(),
            level=previous.level, number=previous.number, title=title, path=previous.path,
            page_start=page_number, page_end=page_number,
        )

    def _new_section(
        self, heading: Heading, page_number: int, parent_id: uuid.UUID | None, path: str
    ) -> SectionDraft:
        return SectionDraft(
            id=uuid.uuid4(), parent_id=parent_id, order_index=self._next_order(),
            level=heading.level, number=heading.number, title=heading.title[:500], path=path,
            page_start=page_number, page_end=page_number,
        )

    def _next_order(self) -> int:
        self._order += 1
        return self._order

    def _close(self) -> SectionDraft:
        section, self._current = self._current, None
        self.section_count += 1
        return section


def merge_numbered_rows(lines: list[list]) -> list[list]:
    """Join a bare section number with the title set beside it on the same row.

    Layouts often place "3.2" and "Transmission Planning Criteria" in separate
    text blocks; classified apart, neither is recognised as heading "3.2".
    """
    merged: list[list] = []
    index = 0
    while index < len(lines):
        block, text, size, bold, y = lines[index]
        following = lines[index + 1] if index + 1 < len(lines) else None
        if (
            following is not None and not is_table_block(block) and _BARE_NUMBER.match(text.strip())
            and abs(float(following[4]) - float(y)) <= SAME_ROW_TOLERANCE
        ):
            merged.append([block, f"{text.strip().rstrip('.')} {following[1].strip()}", max(size, following[2]),
                           bool(bold or following[3]), y])
            index += 2
            continue
        merged.append(lines[index])
        index += 1
    return merged


def _table_blocks(lines: list[list]) -> set[int]:
    """Return layout blocks that behave like multi-row, two-column tables.

    PDF extraction commonly emits each row as a bold label and a regular value
    at the same vertical position inside one block.  Treating the labels as
    headings fragments a glossary into empty sections, so preserve the block as
    ordinary searchable text instead.
    """
    blocks: dict[int, list[tuple[float, bool]]] = {}
    for block, _text, _size, bold, y in lines:
        blocks.setdefault(block, []).append((float(y), bool(bold)))

    tables: set[int] = set()
    for block, entries in blocks.items():
        paired_rows = sum(
            1
            for y, bold in entries
            if bold and any(not other_bold and abs(other_y - y) <= 1.5 for other_y, other_bold in entries)
        )
        if paired_rows >= 3:
            tables.add(block)
    return tables


def _join_lines(lines: list[str]) -> str:
    out = lines[0]
    for line in lines[1:]:
        if out.endswith("-") and line[:1].islower():
            out = out[:-1] + line
        else:
            out = f"{out} {line}"
    return out
