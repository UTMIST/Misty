from src.sources.google_extractors.sheets import MAX_ROWS_PER_TAB, SheetsExtractor


class _FakeValues:
    def __init__(self, ranges_response, recorder):
        self._response, self._recorder = ranges_response, recorder

    def batchGet(self, *, spreadsheetId, ranges):
        self._recorder["batchGet_calls"] += 1
        self._recorder["ranges"] = list(ranges)
        return self

    def execute(self):
        return self._response


class _FakeSpreadsheets:
    def __init__(self, meta, ranges_response, recorder):
        self._meta, self._ranges_response = meta, ranges_response
        self._recorder = recorder

    def get(self, **kwargs):
        self._recorder["get_kwargs"] = kwargs
        return self

    def execute(self):
        return self._meta

    def values(self):
        return _FakeValues(self._ranges_response, self._recorder)


class _FakeSheetsService:
    def __init__(self, meta, ranges_response, recorder):
        self._meta, self._ranges_response = meta, ranges_response
        self._recorder = recorder

    def spreadsheets(self):
        return _FakeSpreadsheets(self._meta, self._ranges_response, self._recorder)


def _extract(tabs, value_ranges):
    """tabs: list of (title, index). value_ranges: list of row-lists."""
    recorder = {"batchGet_calls": 0}
    meta = {"sheets": [{"properties": {"title": t, "index": i}} for t, i in tabs]}
    response = {"valueRanges": [{"values": rows} if rows else {} for rows in value_ranges]}
    services = {"sheets": _FakeSheetsService(meta, response, recorder)}
    result = SheetsExtractor().extract(
        services, "file123", "application/vnd.google-apps.spreadsheet"
    )
    return result, recorder


def test_every_tab_is_emitted_under_its_own_heading():
    result, _ = _extract(
        [("Budget", 0), ("Sponsors", 1)],
        [[["Category", "Spent"]], [["Name", "Tier"]]],
    )
    assert "## Budget" in result.text
    assert "## Sponsors" in result.text


def test_tabs_appear_in_workbook_index_order():
    result, _ = _extract([("Second", 1), ("First", 0)], [[["b"]], [["a"]]])
    assert result.text.index("## First") < result.text.index("## Second")


def test_all_tabs_are_fetched_in_a_single_batchget():
    _, recorder = _extract([("A", 0), ("B", 1)], [[["1"]], [["2"]]])
    assert recorder["batchGet_calls"] == 1
    assert recorder["ranges"] == ["'A'", "'B'"]


def test_tab_title_with_an_apostrophe_is_quoted_and_doubled_in_the_range():
    result, recorder = _extract([("Jon's Data", 0)], [[["x"]]])
    assert recorder["ranges"] == ["'Jon''s Data'"]
    assert "## Jon's Data" in result.text


def test_tab_title_with_a_space_is_quoted_in_the_range_but_heading_stays_raw():
    result, recorder = _extract([("Form Responses 1", 0)], [[["x"]]])
    assert recorder["ranges"] == ["'Form Responses 1'"]
    assert "## Form Responses 1" in result.text


def test_tab_title_that_looks_like_a_cell_reference_is_quoted():
    _, recorder = _extract([("Q1", 0)], [[["x"]]])
    assert recorder["ranges"] == ["'Q1'"]


def test_metadata_call_does_not_request_grid_data():
    _, recorder = _extract([("A", 0)], [[["1"]]])
    assert "includeGridData" not in recorder["get_kwargs"]


def test_rows_render_as_csv():
    result, _ = _extract([("T", 0)], [[["Category", "Spent"], ["Events", "300"]]])
    assert "Category,Spent" in result.text
    assert "Events,300" in result.text


def test_cell_containing_a_comma_is_quoted_and_does_not_split_the_row():
    result, _ = _extract([("T", 0)], [[["Amount"], ["$1,200"]]])
    assert '"$1,200"' in result.text


def test_empty_tab_is_skipped_with_no_heading_and_no_warning():
    result, _ = _extract([("Full", 0), ("Empty", 1)], [[["x"]], []])
    assert "## Full" in result.text
    assert "## Empty" not in result.text
    assert result.warnings == []


def test_oversized_tab_is_truncated_and_warns_with_the_real_row_count():
    rows = [[str(i)] for i in range(MAX_ROWS_PER_TAB + 25)]
    result, _ = _extract([("Big", 0)], [rows])
    assert len(result.warnings) == 1
    assert "Big" in result.warnings[0]
    assert str(MAX_ROWS_PER_TAB + 25) in result.warnings[0]
    assert result.text.count("\n") <= MAX_ROWS_PER_TAB + 1


def test_ragged_rows_survive():
    result, _ = _extract([("T", 0)], [[["a", "b", "c"], ["1"]]])
    assert "a,b,c" in result.text
    assert "\n1" in result.text


def test_short_value_ranges_warns_with_both_counts():
    recorder = {"batchGet_calls": 0}
    meta = {
        "sheets": [
            {"properties": {"title": "A", "index": 0}},
            {"properties": {"title": "B", "index": 1}},
        ]
    }
    # Only one valueRange for two tabs: batchGet returned short.
    response = {"valueRanges": [{"values": [["x"]]}]}
    services = {"sheets": _FakeSheetsService(meta, response, recorder)}
    result = SheetsExtractor().extract(
        services, "file123", "application/vnd.google-apps.spreadsheet"
    )
    assert len(result.warnings) == 1
    assert "1" in result.warnings[0]
    assert "2" in result.warnings[0]


def test_empty_value_ranges_warns_and_yields_empty_text():
    recorder = {"batchGet_calls": 0}
    meta = {"sheets": [{"properties": {"title": "A", "index": 0}}]}
    response = {"valueRanges": []}
    services = {"sheets": _FakeSheetsService(meta, response, recorder)}
    result = SheetsExtractor().extract(
        services, "file123", "application/vnd.google-apps.spreadsheet"
    )
    assert result.text == ""
    assert len(result.warnings) == 1


def test_spreadsheet_with_no_tabs_yields_empty_text():
    result, _ = _extract([], [])
    assert result.text == ""


def test_extractor_declares_scope_and_service():
    assert SheetsExtractor().scopes == ("https://www.googleapis.com/auth/spreadsheets.readonly",)
    assert SheetsExtractor().services == ("sheets",)
