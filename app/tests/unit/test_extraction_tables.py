"""Table rows keep each record's cells together, labelled by their column."""
import pymupdf
import pymupdf.table as table

from app.modules.ingestion import table_chars
from app.modules.ingestion.extraction import _rulings, _table_rows, is_table_block, render_rows


def test_rows_are_labelled_by_header_and_keep_cells_aligned():
    header = ["", "Comments received", "Comments received from", "Action taken/Remarks"]
    rows = [
        ["3", "Revise planning horizon to 5-10 years", "Adani Electricity Mumbai Ltd. (AEML)", "Plan covers 10 years"],
        ["", "continued comment text", "", "continued reply"],  # a row continued from the previous page
    ]
    assert render_rows(rows, header) == [
        "3 | Comments received: Revise planning horizon to 5-10 years | "
        "Comments received from: Adani Electricity Mumbai Ltd. (AEML) | Action taken/Remarks: Plan covers 10 years",
        "Comments received: continued comment text | Action taken/Remarks: continued reply",
    ]


def test_rows_without_header_are_plain_cells():
    assert render_rows([["STU", "State Transmission Utility"], ["", ""]], None) == ["STU | State Transmission Utility"]


def test_table_blocks_are_negative():
    assert is_table_block(-1) and is_table_block(-40) and not is_table_block(0)


def _ruled_page(columns: bool = True):
    """Prose around a 3 x 3 ruled table, and a full-width rule that runs past the table's columns."""
    page = pymupdf.open().new_page()
    page.insert_text((72, 60), "Lending policy overview: prose outside any table.")
    page.draw_line((40, 120), (560, 120))  # the table's top border, drawn across the page
    xs, ys = [100, 220, 340, 460], [120, 140, 160, 180]
    for y in ys[1:]:
        page.draw_line((xs[0], y), (xs[-1], y))
    if columns:
        for x in xs:
            page.draw_line((x, ys[0]), (x, ys[-1]))
    for r, row in enumerate([["Tier", "Score", "Rate"], ["A", "750+", "8.5%"], ["B", "700-749", "9.1%"]]):
        for c, text in enumerate(row):
            page.insert_text((xs[c] + 4, ys[r] + 14), text)
    page.insert_text((72, 240), "Prose below the table.")
    return page


def _characters(make_chars, page, clip):
    table.CHARS._list().clear()
    make_chars(page, clip=clip)
    found = list(table.CHARS._list())
    table.CHARS._list().clear()
    return found


def test_fast_character_list_is_identical_to_pymupdfs():
    page = _ruled_page()
    _, area = _rulings(page)
    for clip in (None, area):
        assert _characters(table_chars.make_chars, page, clip) == _characters(table_chars._original, page, clip)
    if pymupdf.VersionBind == table_chars.CHECKED_VERSION:
        assert table.make_chars is table_chars.make_chars


def test_fast_cell_text_is_identical_to_pymupdfs():
    tables = _ruled_page().find_tables(strategy="lines_strict").tables
    assert tables
    for found in tables:
        assert table_chars.extract(found) == table_chars._original_extract(found)
    if pymupdf.VersionBind == table_chars.CHECKED_VERSION:
        assert table.Table.extract is table_chars.extract


def test_search_confined_to_the_column_rulings_finds_the_same_tables():
    page = _ruled_page()
    _, area = _rulings(page)
    assert area is not None and 40 < area.x0 and area.x1 < 560  # narrower than the rule crossing it
    whole = [t.extract() for t in page.find_tables(strategy="lines_strict").tables]
    confined = [t.extract() for t in page.find_tables(clip=area, strategy="lines_strict").tables]
    assert confined == whole and len(whole) == 1 and len(whole[0]) == 3


def test_a_page_without_column_rulings_is_not_searched():
    page = _ruled_page(columns=False)
    assert _rulings(page)[1] is None

    def fail(**_):
        raise AssertionError("table search ran")

    page.find_tables = fail
    assert _table_rows(page, []) == []
