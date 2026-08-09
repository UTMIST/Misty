from src.sources.google_extractors.slides import SlidesExtractor


def _shape(text, placeholder=None, object_id="s1"):
    shape = {"text": {"textElements": [{"textRun": {"content": text}}]}}
    if placeholder:
        shape["placeholder"] = {"type": placeholder}
    return {"objectId": object_id, "shape": shape}


def _slide(elements, notes=None):
    slide = {"pageElements": elements}
    if notes is not None:
        slide["slideProperties"] = {
            "notesPage": {
                "notesProperties": {"speakerNotesObjectId": "notes1"},
                "pageElements": [_shape(notes, object_id="notes1")],
            }
        }
    return slide


class _FakeSlides:
    def __init__(self, deck):
        self._deck = deck

    def presentations(self):
        return self

    def get(self, *, presentationId):
        return self

    def execute(self):
        return self._deck


def _extract(slides):
    services = {"slides": _FakeSlides({"slides": slides})}
    return SlidesExtractor().extract(
        services, "file123", "application/vnd.google-apps.presentation"
    )


def test_titled_slide_gets_a_numbered_heading():
    out = _extract([_slide([_shape("Sponsorship Tiers\n", placeholder="TITLE")])]).text
    assert "## Slide 1: Sponsorship Tiers" in out


def test_centered_title_also_counts_as_the_title():
    out = _extract([_slide([_shape("Welcome\n", placeholder="CENTERED_TITLE")])]).text
    assert "## Slide 1: Welcome" in out


def test_slide_with_no_text_at_all_has_no_colon():
    # The only remaining case with genuinely no title candidate: a slide with
    # no text shapes and no table. (A slide with body text but no placeholder
    # now promotes that text to the title via the fallback below, so it no
    # longer belongs to "no colon" — see
    # test_no_placeholder_promotes_first_text_shape_to_title.)
    out = _extract([_slide([{"objectId": "img1", "image": {"contentUrl": "https://x"}}])]).text
    assert "## Slide 1" in out
    assert "## Slide 1:" not in out


def test_slide_with_only_a_table_has_no_colon():
    table = {
        "objectId": "t1",
        "table": {
            "tableRows": [
                {"tableCells": [{"text": {"textElements": [{"textRun": {"content": "A\n"}}]}}]}
            ]
        },
    }
    out = _extract([_slide([table])]).text
    assert "## Slide 1" in out
    assert "## Slide 1:" not in out


def test_no_placeholder_promotes_first_text_shape_to_title():
    out = _extract(
        [
            _slide(
                [
                    _shape("Recruitment Overview\n", object_id="s1"),
                    _shape("More details here\n", object_id="s2"),
                ]
            )
        ]
    ).text
    assert "## Slide 1: Recruitment Overview" in out
    assert out.count("Recruitment Overview") == 1
    assert "More details here" in out


def test_placeholder_title_wins_over_an_earlier_non_placeholder_shape():
    out = _extract(
        [
            _slide(
                [
                    _shape("Not the title\n", object_id="s1"),
                    _shape("Actual Title\n", placeholder="TITLE", object_id="s2"),
                ]
            )
        ]
    ).text
    assert "## Slide 1: Actual Title" in out
    assert "## Slide 1: Not the title" not in out
    assert "Not the title" in out  # stays in the body


def test_slides_are_numbered_from_one_in_order():
    out = _extract(
        [
            _slide([_shape("First\n", placeholder="TITLE")]),
            _slide([_shape("Second\n", placeholder="TITLE")]),
        ]
    ).text
    assert "## Slide 1: First" in out
    assert "## Slide 2: Second" in out


def test_bulleted_shape_keeps_one_line_per_bullet():
    # A single Slides shape holds every bullet, with newlines INSIDE textRun
    # content. Stripping them the way the Docs extractor does would collapse
    # this into one run-on line. A preceding title placeholder keeps the
    # bulleted shape out of the title fallback, so this actually exercises
    # the body path rather than passing through the title path.
    out = _extract(
        [_slide([_shape("Title\n", placeholder="TITLE"), _shape("alpha\nbeta\ngamma\n")])]
    ).text
    assert "## Slide 1: Title" in out
    heading_index = out.index("## Slide 1: Title")
    assert "alpha\nbeta\ngamma" in out[heading_index:]


def test_speaker_notes_are_extracted_via_the_notes_object_id():
    out = _extract(
        [_slide([_shape("Q3 Roadmap\n", placeholder="TITLE")], notes="Lead with Gold.\n")]
    ).text
    assert "**Speaker notes:** Lead with Gold." in out


def test_slide_without_notes_omits_the_notes_line():
    out = _extract([_slide([_shape("No notes here\n")])]).text
    assert "Speaker notes" not in out


def test_notes_page_without_a_matching_object_id_yields_no_notes():
    slide = {
        "pageElements": [_shape("body\n")],
        "slideProperties": {
            "notesPage": {
                "notesProperties": {"speakerNotesObjectId": "missing"},
                "pageElements": [_shape("orphan\n", object_id="other")],
            }
        },
    }
    out = _extract([slide]).text
    assert "Speaker notes" not in out
    assert "orphan" not in out


def test_non_text_elements_are_skipped():
    slide = _slide(
        [
            {"objectId": "img1", "image": {"contentUrl": "https://x"}},
            _shape("real text\n"),
        ]
    )
    out = _extract([slide]).text
    assert "real text" in out


def test_whitespace_only_shape_is_dropped():
    # A preceding title placeholder keeps "kept" out of the title fallback,
    # so this test verifies whitespace-only shapes are dropped from the body.
    out = _extract(
        [_slide([_shape("Title\n", placeholder="TITLE"), _shape("   \n"), _shape("kept\n")])]
    ).text
    assert "## Slide 1: Title" in out
    assert "kept" in out
    assert "\n\n" not in out


def test_table_renders_as_a_markdown_table():
    table = {
        "objectId": "t1",
        "table": {
            "tableRows": [
                {
                    "tableCells": [
                        {"text": {"textElements": [{"textRun": {"content": "Tier\n"}}]}},
                        {"text": {"textElements": [{"textRun": {"content": "Amount\n"}}]}},
                    ]
                },
                {
                    "tableCells": [
                        {"text": {"textElements": [{"textRun": {"content": "Gold\n"}}]}},
                        {"text": {"textElements": [{"textRun": {"content": "$5,000\n"}}]}},
                    ]
                },
            ]
        },
    }
    out = _extract([_slide([table])]).text
    assert "| Tier | Amount |" in out
    assert "| --- | --- |" in out
    assert "| Gold | $5,000 |" in out


def test_multiline_table_cell_produces_a_single_well_formed_row():
    table = {
        "objectId": "t1",
        "table": {
            "tableRows": [
                {
                    "tableCells": [
                        {"text": {"textElements": [{"textRun": {"content": "Tier\n"}}]}},
                        {"text": {"textElements": [{"textRun": {"content": "Perks\n"}}]}},
                    ]
                },
                {
                    "tableCells": [
                        {"text": {"textElements": [{"textRun": {"content": "Gold\n"}}]}},
                        {
                            "text": {
                                "textElements": [
                                    {"textRun": {"content": "Logo on banner\nBooth space\n"}}
                                ]
                            }
                        },
                    ]
                },
            ]
        },
    }
    out = _extract([_slide([table])]).text
    lines = out.splitlines()
    assert "| Gold | Logo on banner<br>Booth space |" in lines
    # Slide heading + header row + separator + one body row: the newline
    # inside the cell must not have split that body row into extra lines.
    assert len(lines) == 4


def test_non_title_placeholders_are_not_promoted_to_the_title():
    out = _extract(
        [
            _slide(
                [_shape("3", placeholder="SLIDE_NUMBER"), _shape("Acme Inc", placeholder="FOOTER")]
            )
        ]
    ).text
    assert "## Slide 1" in out
    assert "## Slide 1:" not in out
    assert "3" in out
    assert "Acme Inc" in out


def test_empty_deck_yields_empty_text():
    assert _extract([]).text == ""


def test_extractor_declares_scope_and_service():
    assert SlidesExtractor().scopes == ("https://www.googleapis.com/auth/presentations.readonly",)
    assert SlidesExtractor().services == ("slides",)
