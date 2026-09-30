"""Table rows keep each record's cells together, labelled by their column."""
from app.modules.ingestion.extraction import is_table_block, render_rows


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
