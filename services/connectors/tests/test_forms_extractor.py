from src.sources.google_extractors.forms import FormsExtractor

FORM_MIME_TYPE = "application/vnd.google-apps.form"


class _FakeForms:
    def __init__(self, form):
        self._form = form
        self.responses_called = False

    def forms(self):
        return self

    def get(self, *, formId):
        return self

    def execute(self):
        return self._form

    def responses(self):
        self.responses_called = True
        raise AssertionError("forms responses must never be fetched")


def _extract(form):
    service = _FakeForms(form)
    result = FormsExtractor().extract({"forms": service}, "file123", FORM_MIME_TYPE)
    return result, service


def _question(title, options=None):
    question = {"required": False}
    if options is not None:
        question["choiceQuestion"] = {
            "type": "RADIO",
            "options": [{"value": v} for v in options],
        }
    else:
        question["textQuestion"] = {"paragraph": False}
    return {"itemId": "q", "title": title, "questionItem": {"question": question}}


def test_title_and_description_become_a_heading_and_paragraph():
    out, _ = _extract(
        {"info": {"title": "Director Recruitment", "description": "Rolling basis."}, "items": []}
    )
    assert out.text.startswith("# Director Recruitment")
    assert "Rolling basis." in out.text


def test_questions_are_emitted_as_bullets():
    out, _ = _extract({"info": {"title": "T"}, "items": [_question("Why this role?")]})
    assert "- Why this role?" in out.text


def test_choice_options_are_listed_under_their_question():
    form = {
        "info": {"title": "T"},
        "items": [_question("Which role?", options=["VP Eng", "Director"])],
    }
    out, _ = _extract(form)
    assert "- Which role?" in out.text
    assert "Options: VP Eng, Director" in out.text


def test_page_break_becomes_a_section_heading():
    form = {
        "info": {"title": "T"},
        "items": [
            {"itemId": "s", "title": "Role Preferences", "pageBreakItem": {}},
            _question("Why?"),
        ],
    }
    out, _ = _extract(form)
    assert "## Role Preferences" in out.text
    assert out.text.index("## Role Preferences") < out.text.index("- Why?")


def test_text_items_are_included_as_body_text():
    form = {
        "info": {"title": "T"},
        "items": [
            {"itemId": "t", "title": "Note", "description": "Read carefully", "textItem": {}}
        ],
    }
    out, _ = _extract(form)
    assert "Note" in out.text
    assert "Read carefully" in out.text


def test_image_and_video_items_are_skipped():
    form = {
        "info": {"title": "T"},
        "items": [
            {"itemId": "i", "title": "A picture", "imageItem": {}},
            {"itemId": "v", "title": "A video", "videoItem": {}},
            _question("Real question"),
        ],
    }
    out, _ = _extract(form)
    assert "A picture" not in out.text
    assert "A video" not in out.text
    assert "- Real question" in out.text


def test_question_groups_contribute_their_title_only():
    form = {
        "info": {"title": "T"},
        "items": [
            {"itemId": "g", "title": "Rate each area", "questionGroupItem": {"questions": [{}, {}]}}
        ],
    }
    out, _ = _extract(form)
    assert "- Rate each area" in out.text


def test_responses_are_never_fetched():
    _, service = _extract({"info": {"title": "T"}, "items": [_question("Q")]})
    assert service.responses_called is False


def test_extractor_declares_forms_scope_and_service():
    assert FormsExtractor().scopes == ("https://www.googleapis.com/auth/forms.body.readonly",)
    assert FormsExtractor().services == ("forms",)
