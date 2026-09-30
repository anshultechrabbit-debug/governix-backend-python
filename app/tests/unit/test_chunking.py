import uuid

from app.modules.search import chunking
from app.modules.search.chunking import SectionInput, chunk_section


def section(content, marks=None):
    return SectionInput(uuid.uuid4(), "5.2", "5 LTV > 5.2 High Value", content, marks or [[0, 1]], 1)


def test_small_section_is_one_chunk():
    [chunk] = chunk_section(section("The LTV shall not exceed 75%."))
    assert chunk.text == "The LTV shall not exceed 75%."
    assert (chunk.page_start, chunk.page_end, chunk.section_number) == (1, 1, "5.2")
    assert chunk.chunk_hash and chunk.token_count >= 1


def test_chunks_respect_paragraphs_pages_and_overlap(monkeypatch):
    monkeypatch.setattr(chunking, "TARGET_CHARS", 120)
    paragraphs = [f"Paragraph {i} explains rule {i}. It ends here." for i in range(6)]
    content = "\n\n".join(paragraphs)
    page_two_starts = content.index("Paragraph 3")
    chunks = chunk_section(section(content, [[0, 4], [page_two_starts, 5]]))

    assert len(chunks) >= 3
    assert chunks[0].page_start == 4
    assert chunks[-1].page_end == 5
    assert any(c.page_start == 4 and c.page_end == 5 for c in chunks) or chunks[1].page_start in (4, 5)
    # Every paragraph is present somewhere; the next chunk repeats the previous last sentence.
    joined = " ".join(c.text for c in chunks)
    assert all(p in joined for p in paragraphs)
    assert chunks[1].text.startswith("It ends here.")


def test_huge_paragraph_is_split_by_sentences(monkeypatch):
    monkeypatch.setattr(chunking, "MAX_CHARS", 100)
    monkeypatch.setattr(chunking, "TARGET_CHARS", 100)
    content = " ".join(f"Sentence number {i} is here." for i in range(30))
    chunks = chunk_section(section(content))
    assert len(chunks) > 3
    assert all(len(c.text) <= 100 + chunking.OVERLAP_CHARS + 1 for c in chunks)


def test_empty_section_has_no_chunks():
    assert chunk_section(section("")) == []
