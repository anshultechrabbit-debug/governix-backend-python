import pytest

from app.modules.ingestion import structure
from app.modules.ingestion.structure import LayoutStats, StructureBuilder, classify_heading

BODY = 10.0


@pytest.mark.parametrize("text, size, bold, expected", [
    ("5.2 LTV for High Value Loans", 10, False, (2, "5.2", "LTV for High Value Loans")),
    ("5 Loan to Value", 13, True, (1, "5", "Loan to Value")),
    ("3.1.4 Documentation:", 10, True, (3, "3.1.4", "Documentation")),
    ("Chapter IV Credit Appraisal", 12, True, (1, "Chapter IV", "Credit Appraisal")),
    ("Annexure A", 10, False, (1, "Annexure A", "Annexure A")),
    ("ELIGIBILITY CRITERIA", 13, True, (1, None, "ELIGIBILITY CRITERIA")),
    ("Risk Mitigants", 10, True, (3, None, "Risk Mitigants")),
])
def test_headings(text, size, bold, expected):
    heading = classify_heading(text, size, bold, BODY)
    assert heading is not None
    assert (heading.level, heading.number, heading.title) == expected


@pytest.mark.parametrize("text, size, bold", [
    ("21 and 65 years of age shall be eligible.", 10, False),  # sentence starting with a number
    ("1. Applicants must be resident Indians aged between 21 and 65 years.", 10, False),
    ("2025 Annual Report", 10, False),  # year, not a section number
    ("The Bank shall review this policy every year and update it as required", 10, True),
    ("Minimum income is Rs. 25,000.", 10, False),
    ("plain body text", 10, False),
    ("Page 4 of 10", 10, False),
])
def test_non_headings(text, size, bold):
    assert classify_heading(text, size, bold, BODY) is None


def build(pages: list[list[list]]):
    stats = LayoutStats()
    for lines in pages:
        stats.observe(lines)
    stats.finalize()
    builder = StructureBuilder(stats)
    sections = []
    for number, lines in enumerate(pages, start=1):
        sections.extend(builder.add_page(number, lines))
    sections.extend(builder.finish())
    return sections, builder


def line(block, text, size=BODY, bold=False):
    return [block, text, size, bold, 0.0]


def test_builds_nested_sections_with_page_marks():
    pages = [
        [
            line(0, "HOME LOAN CREDIT POLICY", 18),
            line(1, "Policy No: HL-2025-01"),
            line(2, "5 Loan to Value", 13, True),
            line(3, "The maximum LTV shall be"),
            line(3, "80% for small loans."),
        ],
        [
            line(0, "More LTV text on page two."),
            line(1, "5.2 LTV for High Value Loans", 12, True),
            line(2, "Above Rs. 75 lakh the LTV shall not exceed 75%."),
        ],
    ]
    sections, builder = build(pages)
    front, ltv, high = sections
    assert front.title == "Front matter"
    assert front.content == "HOME LOAN CREDIT POLICY\n\nPolicy No: HL-2025-01"

    assert (ltv.number, ltv.level, ltv.parent_id) == ("5", 1, None)
    # The heading travels with the section text, so a chunk can be found by it.
    assert ltv.content == "5 Loan to Value\n\nThe maximum LTV shall be 80% for small loans.\n\nMore LTV text on page two."
    assert (ltv.page_start, ltv.page_end) == (1, 2)
    assert ltv.page_marks == [[0, 1], [ltv.content.index("More"), 2]]

    assert high.parent_id == ltv.id
    assert high.path == "5 Loan to Value > 5.2 LTV for High Value Loans"
    assert [s.order_index for s in sections] == [1, 2, 3]
    assert builder.fingerprint.word_count > 0


def test_repeated_headers_footers_and_page_numbers_are_removed():
    pages = [
        [line(0, "ACME BANK - CONFIDENTIAL"), line(1, f"Body text of page {n}."), line(2, f"Page {n} of 5")]
        for n in range(1, 6)
    ]
    sections, _ = build(pages)
    content = "\n".join(s.content for s in sections)
    assert "CONFIDENTIAL" not in content and "Page 3 of 5" not in content
    assert "Body text of page 3." in content


def test_hyphenated_line_breaks_are_joined():
    sections, _ = build([[line(0, "The appli-"), line(0, "cant must sign.")]])
    assert sections[0].content == "The applicant must sign."


def test_two_column_glossary_rows_are_searchable_content():
    pages = [
        [line(0, "1 Scope", 14, True), line(1, "Scope text.")],
        [
        line(0, "ACRONYMS", 14, True),
        [4, "BESS", 11, True, 100.0],
        [4, "Battery Energy Storage System", 11, False, 100.2],
        [4, "HVAC", 11, True, 112.0],
        [4, "High Voltage Alternating Current", 11, False, 112.2],
        [4, "HVDC", 11, True, 124.0],
        [4, "High Voltage Direct Current", 11, False, 124.2],
        ],
    ]
    sections, _ = build(pages)

    glossary = sections[-1]
    assert glossary.title == "ACRONYMS"
    assert "HVDC High Voltage Direct Current" in glossary.content
    assert "HVAC High Voltage Alternating Current" in glossary.content


def test_oversized_sections_are_split_into_continuations(monkeypatch):
    monkeypatch.setattr(structure, "MAX_SECTION_CHARS", 50)
    pages = [[line(0, "1 Scope", 13, True)] + [line(i, "x" * 30 + f" para {i}.") for i in range(1, 6)]]
    sections, _ = build(pages)
    assert len(sections) > 2
    assert sections[1].title == "Scope (continued)"
    assert all(s.number == "1" for s in sections)
    assert all(s.length <= 50 or len(s.paragraphs) == 1 for s in sections)


def test_fingerprint_is_insensitive_to_layout_but_not_content():
    a, builder_a = build([[line(0, "1 Scope", 13, True), line(1, "Loans up to 80% LTV.")]])
    b, builder_b = build([[line(0, "1 Scope", 13, True), line(1, "Loans up to"), line(1, "80%  LTV")]])
    c, builder_c = build([[line(0, "1 Scope", 13, True), line(1, "Loans up to 75% LTV.")]])
    assert builder_a.fingerprint.content_hash == builder_b.fingerprint.content_hash
    assert builder_a.fingerprint.content_hash != builder_c.fingerprint.content_hash


def test_headings_without_body_are_carried_into_the_next_paragraph():
    pages = [[
        line(0, "Chapter 5", 14, True),
        line(1, "Analysis and Studies for 2026-27", 14, True),
        line(2, "This chapter covers the studies."),
    ]]
    sections, _ = build(pages)
    assert sections[-1].content.startswith("Chapter 5\nAnalysis and Studies for 2026-27\n\nThis chapter")
    assert sections[0].title == "Chapter 5"  # "Chapter 5", not "Chapter 5 Chapter 5"


def test_provision_citation_is_not_a_heading():
    pages = [[
        line(0, "NATIONAL ELECTRICITY PLAN", 30, True),
        line(1, "Section 3(4) of the Electricity Act 2003]", 14),
        line(2, "GOVERNMENT OF INDIA", 18),
        line(3, "OCTOBER 2024", 24, True),
    ]]
    sections, _ = build(pages)
    assert len(sections) == 1 and sections[0].title == "Front matter"
    assert "Section 3(4) of the Electricity Act 2003]" in sections[0].content
    assert "OCTOBER 2024" in sections[0].content


def test_table_rows_are_paragraphs_never_headings():
    pages = [[
        line(-1, "Chapter 4 | New Technologies Options | 31", 0.0),
        line(-2, "Chapter 5 | Analysis and Studies for 2026-27 | 45", 0.0),
    ]]
    sections, _ = build(pages)
    assert len(sections) == 1
    assert sections[0].content == (
        "Chapter 4 | New Technologies Options | 31\n\nChapter 5 | Analysis and Studies for 2026-27 | 45"
    )


def test_boilerplate_words_away_from_the_page_edge_are_kept():
    pages = [
        [line(0, "Central Electricity Authority"), line(1, "Body."), line(2, "Text."),
         line(3, "More."), line(4, "Footer")]
        for _ in range(5)
    ]
    pages[0] = [line(0, "Cover"), line(1, "Plan"), line(2, "Central Electricity Authority"),
                line(3, "Date"), line(4, "End")]
    sections, _ = build(pages)
    assert "Central Electricity Authority" in sections[0].content


def test_bare_section_number_joins_title_on_the_same_row():
    pages = [[
        [1, "Chapter - 3", 9.0, True, 55.8],
        [7, "3.2", 10.0, True, 278.7],
        [8, "Transmission Planning Criteria", 10.0, True, 279.0],
        [9, "Criteria text.", 10.0, False, 300.0],
    ]]
    sections, _ = build(pages)
    assert [s.path for s in sections] == ["Chapter 3", "Chapter 3 > 3.2 Transmission Planning Criteria"]


def test_body_type_single_number_line_is_a_footnote_not_a_heading():
    assert classify_heading("1 Includes 60,207 MW of solar rooftop capacity", BODY, False, BODY) is None
    assert classify_heading("5 Loan to Value", 13, True, BODY) is not None


def test_per_page_identifiers_in_a_header_are_content_not_boilerplate():
    """"Policy ID: BNK-126 Version: v4.0" differs on every page and must stay indexed."""
    pages = [
        [line(0, f"Policy ID: BNK-{100 + n}    Version: v{n}.0    Status: Active"), line(1, f"Body text of page {n}."),
         line(2, "More body."), line(3, "Even more."), line(4, "Last line.")]
        for n in range(1, 7)
    ]
    sections, _ = build(pages)
    content = "\n".join(s.content for s in sections)
    assert "BNK-103" in content and "v4.0" in content


def test_page_number_only_headers_are_still_removed():
    pages = [
        [line(0, "ACME BANK"), line(1, f"Body of page {n}."), line(2, "Text."), line(3, "More."), line(4, f"Page {n} of 6")]
        for n in range(1, 7)
    ]
    sections, _ = build(pages)
    assert "Page 3 of 6" not in "\n".join(s.content for s in sections)
