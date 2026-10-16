import base64

import httpx
import pytest

from src.sources.base import (
    SourceForbidden,
    SourceNotFound,
    SourceUnavailable,
    SourceUnsupported,
)
from src.sources.github import GithubSource, parse_github_title, parse_github_url

API = "https://api.github.com"
README_URL = f"{API}/repos/utmist/site/readme"
REPO_URL = f"{API}/repos/utmist/site"
BLOB_URL = f"{API}/repos/utmist/site/contents/docs/setup.py"


class _GitHub:
    """Fake GitHub REST API: routes (path, accept) to canned responses and
    records every request so tests can assert what was (not) fetched."""

    def __init__(self, files=None, repos=None, private=False, token=None, status=None):
        self.files = files or {}  # api url -> bytes
        self.repos = repos if repos is not None else {REPO_URL}
        self.private = private
        self.token = token
        self.status = status  # (code, headers) forced on every request
        self.requests: list[httpx.Request] = []
        self.size_override: dict[str, int] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status is not None:
            code, headers = self.status
            return httpx.Response(code, headers=headers, json={"message": "x"})
        if self.private and request.headers.get("Authorization") != f"Bearer {self.token}":
            # GitHub hides private repos from unauthorized callers with 404.
            return httpx.Response(404, json={"message": "Not Found"})
        url = str(request.url.copy_with(query=None))
        if url in self.files:
            data = self.files[url]
            if request.headers["Accept"] == "application/vnd.github.raw+json":
                return httpx.Response(200, content=data)
            size = self.size_override.get(url, len(data))
            return httpx.Response(
                200,
                json={
                    "type": "file",
                    "size": size,
                    "encoding": "base64",
                    "content": base64.b64encode(data).decode(),
                },
            )
        if url in self.repos:
            return httpx.Response(200, json={"full_name": "utmist/site"})
        return httpx.Response(404, json={"message": "Not Found"})

    def raw_requests(self):
        return [
            r for r in self.requests if r.headers["Accept"] == "application/vnd.github.raw+json"
        ]


def _source(fake, token="", max_file_bytes=1024, max_content_chars=10_000):
    client = httpx.Client(transport=httpx.MockTransport(fake))
    return GithubSource(
        token=token,
        max_content_chars=max_content_chars,
        max_file_bytes=max_file_bytes,
        request_timeout_s=5.0,
        client=client,
    )


# --- URL parsing ---------------------------------------------------------


def test_parse_github_title_from_path():
    assert parse_github_title("https://github.com/utmist/site") == "utmist/site"
    assert parse_github_title("https://github.com/utmist") is None


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/utmist/site", ("utmist", "site", None, None)),
        ("https://github.com/utmist/site/", ("utmist", "site", None, None)),
        ("https://www.github.com/utmist/site.git", ("utmist", "site", None, None)),
        ("https://github.com/utmist/site/tree/dev", ("utmist", "site", "dev", None)),
        (
            "https://github.com/utmist/site/blob/main/docs/setup.py",
            ("utmist", "site", "main", "docs/setup.py"),
        ),
    ],
)
def test_parse_github_url(url, expected):
    assert parse_github_url(url) == expected


@pytest.mark.parametrize("url", ["https://gitlab.com/a/b", "https://github.com/onlyowner"])
def test_unparseable_url_is_not_found(url):
    with pytest.raises(SourceNotFound):
        parse_github_url(url)


def test_unsupported_github_page_is_unsupported():
    with pytest.raises(SourceUnsupported):
        parse_github_url("https://github.com/utmist/site/issues/4")


# --- README --------------------------------------------------------------


def test_repository_url_returns_readme_with_owner_repo_title():
    fake = _GitHub(files={README_URL: b"# Site\n\nHello"})
    result = _source(fake).fetch("https://github.com/utmist/site")

    assert result.title == "utmist/site"
    assert result.content == "# Site\n\nHello"
    assert result.warnings == []
    assert result.permissions is None
    # Default branch: no ref is sent, so GitHub resolves it (and the
    # README's actual filename) itself.
    assert all("ref" not in r.url.params for r in fake.requests)


def test_tree_url_requests_readme_at_that_ref():
    fake = _GitHub(files={README_URL: b"dev readme"})
    result = _source(fake).fetch("https://github.com/utmist/site/tree/dev")

    assert result.content == "dev readme"
    assert {r.url.params["ref"] for r in fake.requests} == {"dev"}


def test_repository_without_readme_returns_no_content_with_warning():
    fake = _GitHub(files={})
    result = _source(fake).fetch("https://github.com/utmist/site")

    assert result.title == "utmist/site"
    assert result.content is None
    assert result.warnings == ["repository has no README"]


def test_missing_repository_is_not_found():
    fake = _GitHub(files={}, repos=set())
    with pytest.raises(SourceNotFound):
        _source(fake).fetch("https://github.com/utmist/site")


# --- blob ----------------------------------------------------------------


def test_blob_url_returns_any_text_file_at_ref():
    fake = _GitHub(files={BLOB_URL: b"print('hi')\n"})
    result = _source(fake).fetch("https://github.com/utmist/site/blob/v1.2/docs/setup.py")

    assert result.title == "utmist/site/docs/setup.py"
    assert result.content == "print('hi')\n"
    assert {r.url.params["ref"] for r in fake.requests} == {"v1.2"}


def test_blob_pointing_at_directory_is_unsupported():
    def handler(request):
        return httpx.Response(200, json=[{"type": "file", "name": "a.md"}])

    with pytest.raises(SourceUnsupported):
        _source(handler).fetch("https://github.com/utmist/site/blob/main/docs")


def test_binary_blob_is_unsupported():
    fake = _GitHub(files={BLOB_URL: b"\x89PNG\x00\x00"})
    with pytest.raises(SourceUnsupported, match="binary"):
        _source(fake).fetch("https://github.com/utmist/site/blob/main/docs/setup.py")


def test_oversized_file_is_rejected_before_bytes_are_fetched():
    fake = _GitHub(files={BLOB_URL: b"small"})
    fake.size_override[BLOB_URL] = 5000
    with pytest.raises(SourceUnsupported, match="5000 bytes"):
        _source(fake, max_file_bytes=1024).fetch(
            "https://github.com/utmist/site/blob/main/docs/setup.py"
        )
    assert fake.raw_requests() == []


def test_oversized_readme_is_rejected_before_bytes_are_fetched():
    fake = _GitHub(files={README_URL: b"x" * 2000})
    with pytest.raises(SourceUnsupported):
        _source(fake, max_file_bytes=1024).fetch("https://github.com/utmist/site")
    assert fake.raw_requests() == []


def test_content_is_clamped_to_max_content_chars():
    fake = _GitHub(files={README_URL: b"abcdef"})
    result = _source(fake, max_content_chars=3).fetch("https://github.com/utmist/site")
    assert result.content == "abc"


# --- auth ----------------------------------------------------------------


def test_public_repository_needs_no_token():
    fake = _GitHub(files={README_URL: b"public"})
    _source(fake, token="").fetch("https://github.com/utmist/site")
    assert all("Authorization" not in r.headers for r in fake.requests)


def test_token_enables_private_repository_access():
    fake = _GitHub(files={README_URL: b"private"}, private=True, token="ghp_secret")
    result = _source(fake, token="ghp_secret").fetch("https://github.com/utmist/site")
    assert result.content == "private"


def test_private_repository_without_token_is_not_found():
    fake = _GitHub(files={README_URL: b"private"}, private=True, token="ghp_secret")
    with pytest.raises(SourceNotFound):
        _source(fake, token="").fetch("https://github.com/utmist/site")


# --- error mapping -------------------------------------------------------


@pytest.mark.parametrize(
    "status,headers,expected",
    [
        (404, {}, SourceNotFound),
        (401, {}, SourceForbidden),
        (403, {}, SourceForbidden),
        (403, {"x-ratelimit-remaining": "0"}, SourceUnavailable),
        (403, {"retry-after": "60"}, SourceUnavailable),
        (429, {}, SourceUnavailable),
        (500, {}, SourceUnavailable),
        (503, {}, SourceUnavailable),
    ],
)
def test_status_maps_to_normalized_error(status, headers, expected):
    fake = _GitHub(status=(status, headers))
    with pytest.raises(expected):
        _source(fake).fetch("https://github.com/utmist/site/blob/main/docs/setup.py")


def test_transport_failure_is_unavailable():
    def handler(request):
        raise httpx.ConnectError("boom")

    with pytest.raises(SourceUnavailable):
        _source(handler).fetch("https://github.com/utmist/site")


def test_token_never_appears_in_error_messages():
    fake = _GitHub(status=(401, {}))
    with pytest.raises(SourceForbidden) as exc:
        _source(fake, token="ghp_secret").fetch("https://github.com/utmist/site/blob/m/a.md")
    assert "ghp_secret" not in str(exc.value)
