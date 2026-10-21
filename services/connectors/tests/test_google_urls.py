import pytest

from src.sources.google import parse_file_id

FILE_ID = "1a2B3c-D4e_F5g6H7i8J9k"


@pytest.mark.parametrize(
    "url",
    [
        f"https://docs.google.com/document/d/{FILE_ID}/edit",
        f"https://docs.google.com/document/d/{FILE_ID}/edit?usp=sharing",
        f"https://docs.google.com/spreadsheets/d/{FILE_ID}/edit#gid=0",
        f"https://docs.google.com/presentation/d/{FILE_ID}/edit",
        f"https://drive.google.com/file/d/{FILE_ID}/view",
        f"https://drive.google.com/open?id={FILE_ID}",
        f"https://drive.google.com/open?authuser=0&id={FILE_ID}",
        f"https://docs.google.com/forms/d/{FILE_ID}/edit",
    ],
)
def test_recognized_forms_yield_the_file_id(url):
    assert parse_file_id(url) == FILE_ID


@pytest.mark.parametrize("kind", ["document", "spreadsheets", "presentation", "forms"])
@pytest.mark.parametrize("account", [0, 12])
def test_account_indexed_editor_urls(kind, account):
    assert parse_file_id(f"https://docs.google.com/{kind}/u/{account}/d/{FILE_ID}/edit") == FILE_ID
    assert parse_file_id(f"https://docs.google.com/{kind}/u/{account}/d/e/published/pub") is None


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/doc",
        "https://docs.google.com/document/",
        "https://drive.google.com/open?authuser=0",
        "not a url at all",
        "",
        "https://docs.google.com/forms/d/e/1FAIpQLSfExample/viewform",
        "https://docs.google.com/document/d/e/2PACX-published/pub",
        "https://docs.google.com/spreadsheets/d/e/2PACX-published/pubhtml",
        "https://docs.google.com/presentation/d/e/2PACX-published/pub",
        "https://forms.gle/abc123",
    ],
)
def test_unrecognized_forms_yield_none(url):
    assert parse_file_id(url) is None
