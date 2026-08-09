from src.sources.google_extractors.docs import DocsExtractor


def _para(text, style="NORMAL_TEXT", link=None, bullet=False):
    run = {"textRun": {"content": text + "\n"}}
    if link:
        run["textRun"]["textStyle"] = {"link": {"url": link}}
    para = {"paragraph": {"elements": [run], "paragraphStyle": {"namedStyleType": style}}}
    if bullet:
        para["paragraph"]["bullet"] = {"listId": "l1"}
    return para


def _table(rows):
    return {
        "table": {
            "tableRows": [
                {"tableCells": [{"content": [_para(cell)]} for cell in row]} for row in rows
            ]
        }
    }


class _FakeDocs:
    def __init__(self, doc):
        self._doc = doc

    def documents(self):
        return self

    def get(self, *, documentId):
        return self

    def execute(self):
        return self._doc


def _extract(elements):
    services = {"docs": _FakeDocs({"title": "T", "body": {"content": elements}})}
    return DocsExtractor().extract(services, "file123", "application/vnd.google-apps.document")


def test_headings_become_markdown_levels():
    out = _extract([_para("Big", "HEADING_1"), _para("Small", "HEADING_3")]).text
    assert "# Big" in out
    assert "### Small" in out


def test_title_style_becomes_a_top_level_heading():
    assert "# Doc Title" in _extract([_para("Doc Title", "TITLE")]).text


def test_normal_paragraphs_have_no_prefix():
    out = _extract([_para("just words")]).text
    assert "just words" in out
    assert "#" not in out


def test_list_items_get_a_dash_prefix():
    assert "- an item" in _extract([_para("an item", bullet=True)]).text


def test_links_render_as_markdown_links():
    out = _extract([_para("click here", link="https://example.com")]).text
    assert "[click here](https://example.com)" in out


def test_tables_render_with_a_header_row():
    out = _extract([_table([["Tier", "Amount"], ["Gold", "$500"]])]).text
    assert "| Tier | Amount |" in out
    assert "| --- | --- |" in out
    assert "| Gold | $500 |" in out


def test_paragraph_newlines_are_not_doubled():
    out = _extract([_para("one"), _para("two")]).text
    assert "\n\n\n" not in out


def test_extractor_declares_the_docs_scope():
    assert DocsExtractor().scopes == ("https://www.googleapis.com/auth/documents.readonly",)
