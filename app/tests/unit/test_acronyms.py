"""Glossary / acronym extraction: the row a person sees in the PDF must be answerable.

Regression cover for a real failure: an acronym table split mid-table by the
chunker left the expansion in a tiny orphan chunk that relevance ranking never
selected, so "What does HVDC stand for?" returned a no-answer.
"""
import pytest

from app.modules.rag.service import _acronym_expansion, _is_acronym_expansion

# How the extractor emits a two-column glossary table: label and value on
# separate lines, the whole table flattened into one chunk.
STACKED_TABLE = (
    "Central Electricity Authority\nNational Electricity Plan\nACRONYMS\nAcronyms\nExpansion\n"
    "AAAC\nAll Aluminium Alloy Conductor\n"
    "BESS\nBattery Energy Storage System\n"
    "HVDC\nHigh Voltage Direct Current\n"
    "CTU\nCentral Transmission Utility"
)

# The orphan chunk: the row the chunker severed from the table above.
ORPHAN_ROW = "HVDC — High Voltage Direct Current"

INLINE_TABLE = "ACRONYMS\nHVDC - High Voltage Direct Current\nCEA: Central Electricity Authority"


@pytest.mark.parametrize("text, expected", [
    (STACKED_TABLE, "High Voltage Direct Current"),
    (ORPHAN_ROW, "High Voltage Direct Current"),
    (INLINE_TABLE, "High Voltage Direct Current"),
])
def test_expansion_is_read_from_either_layout(text, expected):
    assert _acronym_expansion("HVDC", text) == expected


def test_stack_and_orphan_agree():
    """The same row must resolve identically before and after the table is split."""
    assert _acronym_expansion("HVDC", STACKED_TABLE) == _acronym_expansion("HVDC", ORPHAN_ROW)


def test_match_is_case_insensitive():
    assert _acronym_expansion("hvdc", "HVDC - High Voltage Direct Current") == "High Voltage Direct Current"


@pytest.mark.parametrize("question_text", [
    "The HVDC system is used for bulk power transfer across long distances.",
    "VSC based HVDC technology has been adopted in recent projects.",
])
def test_prose_mention_is_not_an_expansion(question_text):
    """A sentence that merely mentions the acronym must not be quoted as its meaning."""
    assert _acronym_expansion("HVDC", question_text) == ""


def test_prose_that_restates_the_acronym_is_rejected():
    """A sentence about the subject must not be quoted as the term's meaning."""
    stacked = "Section 4\nHVDC\nHVDC is used for bulk power transfer between regional grids."
    assert _acronym_expansion("HVDC", stacked) == ""
    inline = "HVDC - HVDC links move bulk power over long distances"
    assert _acronym_expansion("HVDC", inline) == ""


def test_single_word_value_is_rejected():
    assert not _is_acronym_expansion("Transmission")


def test_missing_acronym_returns_empty():
    assert _acronym_expansion("ZZZNOTREAL", STACKED_TABLE) == ""


@pytest.mark.parametrize("acronym, text, expected", [
    ("STU", "Acronyms: STU | Expansion: State Transmission Utility", "State Transmission Utility"),
    ("REZ", "RVPN | Rajasthan Rajya Vidyut\n\nREZ | Renewable Energy Zone", "Renewable Energy Zone"),
    ("STU", "developed by State Transmission Utilities (STUs) and licensees", "State Transmission Utility"),
    ("STATCOM", "bus reactors and Static Compensators (STATCOMs) are", "Static Compensator"),
    ("OSOWOG", "Under One Sun One World One Grid (OSOWOG) initiative", "One Sun One World One Grid"),
])
def test_table_rows_and_inline_definitions(acronym, text, expected):
    assert _acronym_expansion(acronym, text) == expected


@pytest.mark.parametrize("text", [
    "TBCB\nUnder Bidding 2028-29 Uttar Pradesh",        # a status cell next to the acronym
    "12 | LILO of line | TBCB | Under Bidding | 2029",   # acronym is not the row's key
])
def test_neighbouring_cells_that_do_not_spell_the_acronym_are_rejected(text):
    assert _acronym_expansion("TBCB", text) == ""


def test_inline_definitions_can_be_excluded():
    assert _acronym_expansion("STATCOM", "Static Compensators (STATCOMs) are used", inline=False) == ""


@pytest.mark.parametrize("question, acronym", [
    ("What does BESS stand for?", "BESS"),
    ("What is the full form of BESS?", "BESS"),
    ("BESS full form", "BESS"),
    ("Expand the acronym STATCOM", "STATCOM"),
    ("What is HVDC short for?", "HVDC"),
    ("What does REZ mean?", "REZ"),
    ("What does the rule mean?", None),
    ("Why are both BESS and pumped-storage plants considered?", None),
])
def test_acronym_question_forms(question, acronym):
    from app.modules.rag.service import acronym_in_question

    assert acronym_in_question(question) == acronym
