from urllib.parse import unquote

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
REFS_URL = f"{API}/repos/utmist/site/git/matching-refs"
RAW = "application/vnd.github.raw+json"


class _GitHub:
    """Fake GitHub REST API. Files are served the way GitHub serves them in
    the raw media type (streamed, `application/vnd.github.raw+json`), refs
    via `git/matching-refs`, and every request is recorded so tests can
    assert what was (not) fetched and how many body bytes were sent."""

    def __init__(
        self,
        files=None,
        repos=None,
        branches=("main", "dev"),
        tags=("v1.2",),
        private=False,
        token=None,
        status=None,
    ):
        self.files = files or {}  # api url -> bytes
        self.repos = repos if repos is not None else {REPO_URL}
        self.branches = set(branches)
        self.tags = set(tags)
        self.private = private
        self.token = token
        self.status = status  # (code, headers) forced on every request
        self.dirs: set[str] = set()
        self.redirects: dict[str, str] = {}  # api url -> location
        self.content_length: dict[str, int] = {}  # declared Content-Length override
        self.requests: list[httpx.Request] = []
        self.bytes_sent = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status is not None:
            code, headers = self.status
            return httpx.Response(code, headers=headers, json={"message": "x"})
        if self.private and request.headers.get("Authorization") != f"Bearer {self.token}":
            # GitHub hides private repos from unauthorized callers with 404.
            return httpx.Response(404, json={"message": "Not Found"})
        url = str(request.url.copy_with(query=None))
        if url in self.redirects:
            return httpx.Response(
                301,
                headers={"Location": self.redirects[url]},
                json={"message": "Moved Permanently"},
            )
        if url.startswith(REFS_URL + "/"):
            namespace, _, prefix = url.removeprefix(REFS_URL + "/").partition("/")
            names = self.branches if namespace == "heads" else self.tags
            prefix = unquote(prefix)
            return httpx.Response(
                200,
                json=[
                    {"ref": f"refs/{namespace}/{n}"} for n in sorted(names) if n.startswith(prefix)
                ],
            )
        if url in self.dirs:
            return httpx.Response(200, json=[{"type": "file", "name": "a.md"}])
        if url in self.files:
            assert request.headers["Accept"] == RAW
            data = self.files[url]
            headers = {"content-type": f"{RAW}; charset=utf-8"}
            if url in self.content_length:
                headers["content-length"] = str(self.content_length[url])
            return httpx.Response(200, headers=headers, content=self._chunks(data))
        if url in self.repos:
            return httpx.Response(200, json={"full_name": "utmist/site"})
        return httpx.Response(404, json={"message": "Not Found"})

    def _chunks(self, data: bytes, size: int = 256):
        for i in range(0, len(data), size):
            chunk = data[i : i + size]
            self.bytes_sent += len(chunk)
            yield chunk

    def file_requests(self):
        return [r for r in self.requests if r.headers["Accept"] == RAW]


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
        ("https://github.com/utmist/site", ("utmist", "site", None, [])),
        ("https://github.com/utmist/site/", ("utmist", "site", None, [])),
        ("https://www.github.com/utmist/site.git", ("utmist", "site", None, [])),
        ("https://github.com/utmist/site/tree/dev", ("utmist", "site", "tree", ["dev"])),
        (
            "https://github.com/utmist/site/tree/feature/docs",
            ("utmist", "site", "tree", ["feature", "docs"]),
        ),
        (
            "https://github.com/utmist/site/blob/main/docs/setup.py",
            ("utmist", "site", "blob", ["main", "docs", "setup.py"]),
        ),
        # Percent escapes are decoded once here, so the API path is encoded once.
        (
            "https://github.com/utmist/site/blob/main/setup%20guide.md",
            ("utmist", "site", "blob", ["main", "setup guide.md"]),
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
    assert [r.url.params["ref"] for r in fake.file_requests()] == ["dev"]


def test_tree_url_with_slash_in_ref_requests_readme_at_full_ref():
    fake = _GitHub(files={README_URL: b"docs readme"}, branches={"main", "feature/docs"})
    result = _source(fake).fetch("https://github.com/utmist/site/tree/feature/docs")

    assert result.content == "docs readme"
    assert [r.url.params["ref"] for r in fake.file_requests()] == ["feature/docs"]


def test_nonexistent_ref_is_not_found_not_a_readme_less_repo():
    fake = _GitHub(files={README_URL: b"main readme"})
    with pytest.raises(SourceNotFound, match="ref"):
        _source(fake).fetch("https://github.com/utmist/site/tree/does-not-exist")
    assert fake.file_requests() == []


def test_commit_sha_is_accepted_as_ref():
    fake = _GitHub(files={README_URL: b"at sha"})
    result = _source(fake).fetch("https://github.com/utmist/site/tree/1a2b3c4d")

    assert result.content == "at sha"
    assert [r.url.params["ref"] for r in fake.file_requests()] == ["1a2b3c4d"]


def test_tree_url_into_subdirectory_is_unsupported():
    fake = _GitHub(files={README_URL: b"x"})
    with pytest.raises(SourceUnsupported):
        _source(fake).fetch("https://github.com/utmist/site/tree/main/docs")


def test_repository_without_readme_returns_no_content_with_warning():
    fake = _GitHub(files={})
    result = _source(fake).fetch("https://github.com/utmist/site")

    assert result.title == "utmist/site"
    assert result.content is None
    assert result.warnings == ["repository has no README"]


def test_renamed_repository_redirect_is_followed():
    moved = f"{API}/repositories/42/readme"
    fake = _GitHub(files={moved: b"# Renamed"})
    fake.redirects[README_URL] = moved
    result = _source(fake).fetch("https://github.com/utmist/site")

    assert result.content == "# Renamed"
    assert result.warnings == []


def test_unfollowed_redirect_is_never_stored_as_content():
    def handler(request):
        return httpx.Response(301, json={"message": "Moved Permanently"})

    with pytest.raises(SourceUnavailable):
        _source(handler).fetch("https://github.com/utmist/site")


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
    assert [r.url.params["ref"] for r in fake.file_requests()] == ["v1.2"]


def test_blob_with_slash_in_ref_uses_longest_matching_ref():
    # Both `feature` (with a docs/ dir) and `feature/docs` exist: the URL
    # names the longer ref, so README.md at `feature/docs` is fetched.
    readme = f"{API}/repos/utmist/site/contents/README.md"
    fake = _GitHub(files={readme: b"right file"}, branches={"feature", "feature/docs"})
    result = _source(fake).fetch("https://github.com/utmist/site/blob/feature/docs/README.md")

    assert result.content == "right file"
    assert result.title == "utmist/site/README.md"
    assert [r.url.params["ref"] for r in fake.file_requests()] == ["feature/docs"]


def test_blob_with_shorter_ref_only_keeps_rest_as_path():
    nested = f"{API}/repos/utmist/site/contents/docs/README.md"
    fake = _GitHub(files={nested: b"nested"}, branches={"feature"})
    result = _source(fake).fetch("https://github.com/utmist/site/blob/feature/docs/README.md")

    assert result.content == "nested"
    assert [r.url.params["ref"] for r in fake.file_requests()] == ["feature"]


def test_encoded_filename_is_encoded_once():
    spaced = f"{API}/repos/utmist/site/contents/setup%20guide.md"
    fake = _GitHub(files={spaced: b"guide"})
    result = _source(fake).fetch("https://github.com/utmist/site/blob/main/setup%20guide.md")

    assert result.content == "guide"
    assert result.title == "utmist/site/setup guide.md"
    assert [r.url.raw_path.split(b"?")[0] for r in fake.file_requests()] == [
        b"/repos/utmist/site/contents/setup%20guide.md"
    ]


def test_encoded_ref_is_decoded_before_query_encoding():
    fake = _GitHub(files={BLOB_URL: b"x"}, branches={"feature/docs"})
    _source(fake).fetch("https://github.com/utmist/site/blob/feature%2Fdocs/docs/setup.py")
    assert [r.url.params["ref"] for r in fake.file_requests()] == ["feature/docs"]


def test_blob_pointing_at_directory_is_unsupported():
    fake = _GitHub()
    fake.dirs.add(f"{API}/repos/utmist/site/contents/docs")
    with pytest.raises(SourceUnsupported, match="file"):
        _source(fake).fetch("https://github.com/utmist/site/blob/main/docs")


def test_binary_blob_is_unsupported():
    fake = _GitHub(files={BLOB_URL: b"\x89PNG\x00\x00"})
    with pytest.raises(SourceUnsupported, match="binary"):
        _source(fake).fetch("https://github.com/utmist/site/blob/main/docs/setup.py")


def test_oversized_file_is_rejected_from_content_length_without_reading_body():
    fake = _GitHub(files={BLOB_URL: b"x" * 5000})
    fake.content_length[BLOB_URL] = 5000
    with pytest.raises(SourceUnsupported, match="5000 bytes"):
        _source(fake, max_file_bytes=1024).fetch(
            "https://github.com/utmist/site/blob/main/docs/setup.py"
        )
    assert fake.bytes_sent == 0


def test_oversized_file_download_stops_at_the_limit():
    # No Content-Length (e.g. chunked): the read stops one chunk past the
    # limit instead of buffering the whole 512 KiB file.
    fake = _GitHub(files={BLOB_URL: b"x" * 512 * 1024})
    with pytest.raises(SourceUnsupported, match="limit"):
        _source(fake, max_file_bytes=1024).fetch(
            "https://github.com/utmist/site/blob/main/docs/setup.py"
        )
    assert fake.bytes_sent <= 1024 + 256


def test_oversized_readme_download_stops_at_the_limit():
    fake = _GitHub(files={README_URL: b"x" * 64 * 1024})
    with pytest.raises(SourceUnsupported):
        _source(fake, max_file_bytes=1024).fetch("https://github.com/utmist/site")
    assert fake.bytes_sent <= 1024 + 256


def test_file_at_the_limit_is_accepted():
    fake = _GitHub(files={BLOB_URL: b"x" * 1024})
    result = _source(fake, max_file_bytes=1024).fetch(
        "https://github.com/utmist/site/blob/main/docs/setup.py"
    )
    assert len(result.content) == 1024


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
