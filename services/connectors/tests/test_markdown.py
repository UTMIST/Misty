from src.sources.google_extractors.markdown import render_markdown_table


def test_renders_header_separator_and_body():
    out = render_markdown_table([["Tier", "Amount"], ["Gold", "$5,000"]])
    assert out == [
        "| Tier | Amount |",
        "| --- | --- |",
        "| Gold | $5,000 |",
    ]


def test_pipes_in_cells_are_escaped():
    out = render_markdown_table([["Name"], ["Gold | Silver"]])
    # An unescaped pipe would be read as a column separator and split the row.
    assert out[-1] == r"| Gold \| Silver |"


def test_short_rows_are_padded_to_header_width():
    out = render_markdown_table([["A", "B", "C"], ["1"]])
    assert out[-1] == "| 1 |  |  |"


def test_long_rows_widen_the_table_rather_than_being_truncated():
    out = render_markdown_table([["A"], ["1", "2"]])
    assert out[0] == "| A |  |"
    assert out[1] == "| --- | --- |"
    assert out[-1] == "| 1 | 2 |"


def test_single_row_table_still_has_a_separator():
    out = render_markdown_table([["Only"]])
    assert out == ["| Only |", "| --- |"]


def test_no_rows_renders_nothing():
    assert render_markdown_table([]) == []


def test_rows_of_empty_lists_render_nothing():
    assert render_markdown_table([[], []]) == []


def test_newlines_in_cells_are_replaced_with_br_so_the_row_survives():
    out = render_markdown_table([["Tier", "Perks"], ["Gold", "Logo on banner\nBooth space"]])
    # An unescaped newline would split the row across two physical lines and
    # destroy the table; <br> keeps it on one line and still renders.
    assert out[-1] == "| Gold | Logo on banner<br>Booth space |"
    assert len(out) == 3
