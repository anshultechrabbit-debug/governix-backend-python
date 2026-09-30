"""Section-aware chunking with exact page attribution.

Chunks never cross section boundaries (a citation always names one section),
prefer paragraph and sentence boundaries, and carry a one-sentence overlap so
a rule split across a boundary is still retrievable. Page numbers come from the
section's page marks, never guessed.
"""

import math
import uuid
from bisect import bisect_right
from dataclasses import dataclass

from app.modules.ingestion.text import sha256_text, split_sentences

TARGET_CHARS = 1500
MAX_CHARS = 2200
OVERLAP_CHARS = 300


@dataclass(frozen=True)
class SectionInput:
    id: uuid.UUID
    number: str | None
    path: str
    content: str
    page_marks: list[list[int]]
    page_start: int


@dataclass
class ChunkDraft:
    section_id: uuid.UUID
    section_number: str | None
    section_path: str
    text: str
    char_start: int
    page_start: int
    page_end: int

    @property
    def token_count(self) -> int:
        return max(1, math.ceil(len(self.text) / 4))

    @property
    def chunk_hash(self) -> str:
        return sha256_text(f"{self.section_path}\n{self.text}")


@dataclass
class _Unit:
    text: str
    start: int


def _units(content: str) -> list[_Unit]:
    units: list[_Unit] = []
    position = 0
    for paragraph in content.split("\n\n"):
        start = content.find(paragraph, position)
        position = start + len(paragraph)
        if not paragraph.strip():
            continue
        if len(paragraph) <= MAX_CHARS:
            units.append(_Unit(paragraph, start))
            continue
        offset = start
        for sentence in split_sentences(paragraph):
            sentence_start = content.find(sentence, offset)
            offset = sentence_start + len(sentence)
            for piece_start in range(0, len(sentence), MAX_CHARS):
                piece = sentence[piece_start:piece_start + MAX_CHARS]
                if piece.strip():
                    units.append(_Unit(piece, sentence_start + piece_start))
    return units


def page_at(page_marks: list[list[int]], offset: int, default: int) -> int:
    if not page_marks:
        return default
    offsets = [mark[0] for mark in page_marks]
    index = bisect_right(offsets, offset) - 1
    return page_marks[max(index, 0)][1]


def _last_sentence(text: str) -> str:
    sentences = split_sentences(text)
    last = sentences[-1] if len(sentences) > 1 else ""
    return last if 0 < len(last) <= OVERLAP_CHARS else ""


def chunk_section(section: SectionInput) -> list[ChunkDraft]:
    units = _units(section.content)
    chunks: list[ChunkDraft] = []
    current: list[_Unit] = []
    overlap = ""

    def emit() -> None:
        nonlocal current, overlap
        if not current:
            return
        start = current[0].start
        end = current[-1].start + len(current[-1].text)
        body = "\n\n".join(u.text for u in current)
        text = f"{overlap} {body}".strip() if overlap else body
        chunks.append(ChunkDraft(
            section_id=section.id,
            section_number=section.number,
            section_path=section.path,
            text=text,
            char_start=start,
            page_start=page_at(section.page_marks, start, section.page_start),
            page_end=page_at(section.page_marks, max(end - 1, start), section.page_start),
        ))
        overlap = _last_sentence(body)
        current = []

    for unit in units:
        size = sum(len(u.text) + 2 for u in current) + len(unit.text)
        if current and size > TARGET_CHARS:
            emit()
        current.append(unit)
    emit()
    return chunks
