from hashlib import sha256
from pathlib import Path

import pytest

from src.sources.base import SourceUnavailable
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
        self.include_tabs_content = False

    def documents(self):
        return self

    def get(self, *, documentId, includeTabsContent=False):
        self.include_tabs_content = includeTabsContent
        return self

    def execute(self):
        if "tabs" in self._doc and not self.include_tabs_content:
            return {"body": self._doc["tabs"][0].get("documentTab", {}).get("body", {})}
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


def _tab(title, elements, children=None):
    return {
        "tabProperties": {"title": title},
        "documentTab": {"body": {"content": elements}},
        "childTabs": children or [],
    }


def test_all_tabs_and_nested_tabs_are_rendered_in_document_order():
    docs = _FakeDocs(
        {
            "body": {"content": [_para("stale legacy body")]},
            "tabs": [
                _tab("Overview", [_para("overview detail")]),
                _tab(
                    "Operations",
                    [_para("operations detail", "HEADING_1")],
                    [_tab("Recovery", [_table([["Step", "Action"], ["1", "Restore"]])])],
                ),
                _tab("Roadmap", [_para("future detail", link="https://example.com")]),
            ],
        }
    )

    out = DocsExtractor().extract({"docs": docs}, "file123", "application/vnd.google-apps.document")

    assert docs.include_tabs_content is True
    assert "stale legacy body" not in out.text
    assert '# Tab: "Overview"' in out.text.splitlines()
    assert '# Tab: "Operations" / "Recovery"' in out.text.splitlines()
    assert "## operations detail" in out.text.splitlines()
    assert "| 1 | Restore |" in out.text
    assert "[future detail](https://example.com)" in out.text
    assert out.text.index("overview detail") < out.text.index("operations detail")
    assert out.text.index("operations detail") < out.text.index("Restore")
    assert out.text.index("Restore") < out.text.index("future detail")


def test_single_tab_preserves_existing_rendered_text():
    elements = [
        _para("Title", "TITLE"),
        _para("Subtitle", "SUBTITLE"),
        *[_para(f"Heading {level}", f"HEADING_{level}") for level in range(1, 7)],
        _para("body"),
        _para("item", bullet=True),
        _para("link", link="https://example.com"),
        _table([["Name", "Value"], ["one", "two"]]),
    ]
    docs = _FakeDocs({"tabs": [_tab("Tab 1", elements)]})
    out = DocsExtractor().extract({"docs": docs}, "file123", "application/vnd.google-apps.document")
    assert docs.include_tabs_content is True
    expected = (
        "# Title\n## Subtitle\n# Heading 1\n## Heading 2\n### Heading 3\n"
        "#### Heading 4\n##### Heading 5\n###### Heading 6\nbody\n- item\n"
        "[link](https://example.com)\n| Name | Value |\n| --- | --- |\n| one | two |"
    )
    assert out.text == _extract(elements).text == expected
    assert sha256(out.text.encode()).hexdigest() == sha256(expected.encode()).hexdigest()


def test_empty_parent_tab_does_not_hide_child_content():
    docs = _FakeDocs({"tabs": [_tab("Parent", [], [_tab("Child", [_para("child detail")])])]})
    out = DocsExtractor().extract({"docs": docs}, "file123", "application/vnd.google-apps.document")
    assert out.text == '# Tab: "Parent" / "Child"\nchild detail'


@pytest.mark.parametrize("title", [None, "", "  \t  "])
def test_blank_tab_title_uses_fallback_after_stripping(title):
    docs = _FakeDocs({"tabs": [_tab(title, [], [_tab("Child", [_para("detail")])])]})
    out = DocsExtractor().extract({"docs": docs}, "file123", "application/vnd.google-apps.document")
    assert out.text == '# Tab: "Untitled tab" / "Child"\ndetail'


def test_tab_path_components_are_unambiguous_and_stay_on_one_line():
    docs = _FakeDocs(
        {
            "tabs": [
                _tab("Q1 / Q2", [_para("literal slash")]),
                _tab("Q1", [], [_tab("Q2", [_para("nested")])]),
                _tab('Notes "quoted" \\ draft\n# 日本語 #', [_para("special characters")]),
            ]
        }
    )
    out = DocsExtractor().extract({"docs": docs}, "file123", "application/vnd.google-apps.document")
    assert out.text.splitlines() == [
        '# Tab: "Q1 / Q2"',
        "literal slash",
        '# Tab: "Q1" / "Q2"',
        "nested",
        r'# Tab: "Notes \"quoted\" \\ draft\n# 日本語 #"',
        "special characters",
    ]


@pytest.mark.parametrize("elements", [[], [_para("  ")], [{"sectionBreak": {}}]])
def test_empty_tabs_do_not_emit_labels_or_fall_back_to_stale_body(elements):
    docs = _FakeDocs(
        {
            "body": {"content": [_para("stale legacy body")]},
            "tabs": [_tab("Empty", elements), _tab("Also empty", [])],
        }
    )
    out = DocsExtractor().extract({"docs": docs}, "file123", "application/vnd.google-apps.document")
    assert out.text == ""


@pytest.mark.parametrize("tabs", [None, []])
def test_legacy_body_fallback_when_no_tabs_are_returned(tabs):
    docs = _FakeDocs({"tabs": tabs, "body": {"content": [_para("legacy body")]}})
    out = DocsExtractor().extract({"docs": docs}, "file123", "application/vnd.google-apps.document")
    assert docs.include_tabs_content is True
    assert out.text == "legacy body"


def test_failed_tab_request_is_not_retried_as_a_first_tab_only_fetch():
    class FailingDocs(_FakeDocs):
        def execute(self):
            if self.include_tabs_content:
                raise TimeoutError("synthetic failure")
            return super().execute()

    docs = FailingDocs({"body": {"content": [_para("partial body")]}})
    with pytest.raises(SourceUnavailable, match="google transport failure: TimeoutError"):
        DocsExtractor().extract({"docs": docs}, "file123", "application/vnd.google-apps.document")


@pytest.mark.parametrize(
    "style, prefix",
    [("TITLE", "##"), ("SUBTITLE", "###")]
    + [(f"HEADING_{level}", "#" * min(level + 1, 6)) for level in range(1, 7)],
)
def test_multi_tab_headings_nest_below_tab_labels(style, prefix):
    docs = _FakeDocs(
        {"tabs": [_tab("One", [_para("Heading", style)]), _tab("Two", [_para("body")])]}
    )
    out = DocsExtractor().extract({"docs": docs}, "file123", "application/vnd.google-apps.document")
    assert out.text == f'# Tab: "One"\n{prefix} Heading\n# Tab: "Two"\nbody'


def test_catalog_chunking_fixture_matches_extractor_output():
    # The catalog consumes this exact fixture in test_chunking.py. Comparing the
    # producer's bytes here tests the boundary without cross-service imports.
    docs = _FakeDocs(
        {
            "tabs": [
                _tab(
                    "Operations",
                    [_para("Runbook", "TITLE"), _para("Operate safely. " * 30)],
                    [
                        _tab(
                            "Recovery",
                            [_para("Runbook", "HEADING_1"), _para("Restore safely. " * 30)],
                        )
                    ],
                ),
                _tab(
                    "Archive",
                    [],
                    [
                        _tab(
                            "History", [_para("Runbook", "HEADING_1"), _para("Read history. " * 30)]
                        )
                    ],
                ),
            ]
        }
    )
    out = DocsExtractor().extract({"docs": docs}, "file123", "application/vnd.google-apps.document")
    fixture = Path(__file__).with_name("fixtures") / "google_doc_tabs.md"
    assert out.text + "\n" == fixture.read_text()
