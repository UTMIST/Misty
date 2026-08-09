"""Google Forms extraction to markdown.

Drive cannot export a Form to text at all, so the native API is the only route
— there is no fallback to degrade to.

Only the form's STRUCTURE is read. Responses are deliberately never fetched:
they need a separate call and scope, they duplicate the linked responses
spreadsheet the Sheets extractor already reads, and they are applicant
personal data.
"""

from src.sources.google_extractors.base import ExtractedText, execute

FORMS_READONLY = "https://www.googleapis.com/auth/forms.body.readonly"
FORM_MIME = "application/vnd.google-apps.form"


def _choice_options(question: dict) -> list[str]:
    choice = question.get("choiceQuestion") or {}
    return [o.get("value", "") for o in choice.get("options") or [] if o.get("value")]


def _item_lines(item: dict) -> list[str]:
    title = (item.get("title") or "").strip()

    if "pageBreakItem" in item:
        return [f"## {title}"] if title else []

    if "textItem" in item:
        # Descriptive blocks authors add between questions.
        description = (item.get("description") or "").strip()
        return [line for line in (title, description) if line]

    if "questionItem" in item or "questionGroupItem" in item:
        if not title:
            return []
        lines = [f"- {title}"]
        if "questionItem" in item:
            # Choice options are content, not metadata: on a recruitment form
            # they enumerate the open roles someone would actually ask about.
            options = _choice_options((item["questionItem"] or {}).get("question") or {})
            if options:
                lines.append("  Options: " + ", ".join(options))
        return lines

    # imageItem, videoItem, and anything else carry no extractable text.
    return []


class FormsExtractor:
    scopes = (FORMS_READONLY,)
    services = ("forms",)

    def extract(self, services: dict, file_id: str, mime: str) -> ExtractedText:
        form = execute(services["forms"].forms().get(formId=file_id))
        info = (form or {}).get("info") or {}

        lines: list[str] = []
        title = (info.get("title") or "").strip()
        if title:
            lines.append(f"# {title}")
        description = (info.get("description") or "").strip()
        if description:
            lines.append(description)

        for item in (form or {}).get("items") or []:
            lines.extend(_item_lines(item))

        return ExtractedText(text="\n".join(lines))
