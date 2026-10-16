"""GitHub source: repository README or a single blob file, as text.

Plain httpx calls against the REST API — no vendor SDK. Two URL shapes:

- `github.com/{owner}/{repo}` (optionally `/tree/{ref}`) → the README GitHub
  itself resolves (`GET /repos/{owner}/{repo}/readme`), so `README.md`,
  `README.rst`, `readme.txt`, … and the default branch all work without us
  guessing names.
- `github.com/{owner}/{repo}/blob/{ref}/{path}` → that file at that ref. Any
  text file is accepted, not only Markdown; binary content is unsupported.

Both paths read the contents API's JSON metadata first and check `size`
against `max_file_bytes` before the raw bytes are requested.
"""

from urllib.parse import quote, urlsplit

import httpx

from src.sources.base import (
    SourceForbidden,
    SourceNotFound,
    SourceResult,
    SourceUnavailable,
    SourceUnsupported,
)

GITHUB_SOURCE_IDS = ("github",)

_HOSTS = {"github.com", "www.github.com"}
_API_VERSION = "2022-11-28"
_JSON = "application/vnd.github+json"
_RAW = "application/vnd.github.raw+json"


def parse_github_title(url: str) -> str | None:
    """Derive an `owner/repo` title from a github.com URL path. Returns None
    if the path lacks at least owner + repo."""
    segments = [s for s in urlsplit(url).path.split("/") if s]
    if len(segments) < 2:
        return None
    return f"{segments[0]}/{segments[1]}"


def parse_github_url(url: str) -> tuple[str, str, str | None, str | None]:
    """`(owner, repo, ref, path)` for a supported github.com URL.

    `path` is None for a repository (README) URL. A ref containing `/` is
    ambiguous in a blob URL; the first segment is taken as the ref, matching
    the common case of branch names without slashes.
    """
    parts = urlsplit(url or "")
    if parts.hostname not in _HOSTS:
        raise SourceNotFound("not a github.com url")
    segments = [s for s in parts.path.split("/") if s]
    if len(segments) < 2:
        raise SourceNotFound("github url has no owner/repo")
    owner, repo = segments[0], segments[1].removesuffix(".git")
    rest = segments[2:]
    if not rest:
        return owner, repo, None, None
    if rest[0] == "tree" and len(rest) == 2:
        return owner, repo, rest[1], None
    if rest[0] == "blob" and len(rest) >= 3:
        return owner, repo, rest[1], "/".join(rest[2:])
    raise SourceUnsupported("only github repository and /blob/ file urls are supported")


class GithubSource:
    def __init__(
        self,
        *,
        token: str = "",
        max_content_chars: int,
        max_file_bytes: int,
        request_timeout_s: float,
        api_base_url: str = "https://api.github.com",
        client: httpx.Client | None = None,
    ) -> None:
        self._token = token
        self._max_content_chars = max_content_chars
        self._max_file_bytes = max_file_bytes
        self._request_timeout_s = request_timeout_s
        self._api_base_url = api_base_url.rstrip("/")
        self._client = client

    def fetch(self, url: str) -> SourceResult:
        owner, repo, ref, path = parse_github_url(url)
        client = self._client or httpx.Client(timeout=self._request_timeout_s)
        try:
            if path is None:
                return self._fetch_readme(client, owner, repo, ref)
            return self._fetch_blob(client, owner, repo, ref, path)
        finally:
            if self._client is None:
                client.close()

    def _fetch_readme(
        self, client: httpx.Client, owner: str, repo: str, ref: str | None
    ) -> SourceResult:
        title = f"{owner}/{repo}"
        endpoint = f"/repos/{_seg(owner)}/{_seg(repo)}/readme"
        try:
            meta = self._get_json(client, endpoint, ref)
        except SourceNotFound:
            # GitHub uses 404 for both "no README" and "no such repo" (and for
            # a private repo this token cannot see). Only a repo that resolves
            # is a README-less repo; otherwise the 404 stands.
            self._get_json(client, f"/repos/{_seg(owner)}/{_seg(repo)}", None)
            return SourceResult(title=title, content=None, warnings=["repository has no README"])
        text = self._read_file(client, endpoint, ref, meta)
        return SourceResult(title=title, content=text[: self._max_content_chars])

    def _fetch_blob(
        self, client: httpx.Client, owner: str, repo: str, ref: str | None, path: str
    ) -> SourceResult:
        endpoint = f"/repos/{_seg(owner)}/{_seg(repo)}/contents/{quote(path)}"
        meta = self._get_json(client, endpoint, ref)
        if not isinstance(meta, dict) or meta.get("type") != "file":
            raise SourceUnsupported("github url does not point at a file")
        text = self._read_file(client, endpoint, ref, meta)
        return SourceResult(title=f"{owner}/{repo}/{path}", content=text[: self._max_content_chars])

    def _read_file(self, client: httpx.Client, endpoint: str, ref: str | None, meta) -> str:
        """Size-check the metadata, then return the file decoded as text."""
        size = meta.get("size") if isinstance(meta, dict) else None
        if isinstance(size, int) and size > self._max_file_bytes:
            raise SourceUnsupported(
                f"github file is {size} bytes, over the limit of {self._max_file_bytes} bytes"
            )
        data = self._get_raw(client, endpoint, ref)
        if len(data) > self._max_file_bytes:
            raise SourceUnsupported(
                f"github file exceeds the limit of {self._max_file_bytes} bytes"
            )
        if b"\x00" in data:
            raise SourceUnsupported("github file is binary, not text")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            raise SourceUnsupported("github file is not utf-8 text")

    def _get_json(self, client: httpx.Client, endpoint: str, ref: str | None):
        resp = self._request(client, endpoint, ref, accept=_JSON)
        try:
            return resp.json()
        except ValueError as e:
            raise SourceUnavailable("github returned an unparseable body") from e

    def _get_raw(self, client: httpx.Client, endpoint: str, ref: str | None) -> bytes:
        return self._request(client, endpoint, ref, accept=_RAW).content

    def _request(
        self, client: httpx.Client, endpoint: str, ref: str | None, *, accept: str
    ) -> httpx.Response:
        headers = {"Accept": accept, "X-GitHub-Api-Version": _API_VERSION}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        params = {"ref": ref} if ref else None
        try:
            resp = client.get(f"{self._api_base_url}{endpoint}", headers=headers, params=params)
        except httpx.HTTPError as e:
            raise SourceUnavailable(f"github unreachable: {type(e).__name__}") from e
        _raise_for_status(resp)
        return resp


def _seg(value: str) -> str:
    return quote(value, safe="")


def _raise_for_status(resp: httpx.Response) -> None:
    status = resp.status_code
    if status < 400:
        return
    if status == 404:
        raise SourceNotFound("github file not found")
    # GitHub signals rate limiting as 429, or as 403 with the remaining quota
    # at zero / a retry-after — transient, so it is Unavailable, not Forbidden.
    rate_limited = status == 429 or (
        status == 403
        and (resp.headers.get("x-ratelimit-remaining") == "0" or "retry-after" in resp.headers)
    )
    if rate_limited or status >= 500:
        raise SourceUnavailable(f"github returned {status}")
    if status in (401, 403):
        raise SourceForbidden(f"github returned {status}")
    raise SourceUnavailable(f"github returned {status}")
