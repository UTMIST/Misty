"""GitHub source: repository README or a single blob file, as text.

Plain httpx calls against the REST API — no vendor SDK. Two URL shapes:

- `github.com/{owner}/{repo}` (optionally `/tree/{ref}`) → the README GitHub
  itself resolves (`GET /repos/{owner}/{repo}/readme`), so `README.md`,
  `README.rst`, `readme.txt`, … and the default branch all work without us
  guessing names.
- `github.com/{owner}/{repo}/blob/{ref}/{path}` → that file at that ref. Any
  text file is accepted, not only Markdown; binary content is unsupported.

A ref may contain `/` (`feature/docs`), so it is resolved against the
repository's actual branches and tags rather than guessed from the URL.

File bytes are streamed in the raw media type and the read stops once
`max_file_bytes` is passed, so an oversized file is never fully downloaded.
"""

import re
from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import quote, unquote, urlsplit

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
# A full or abbreviated commit SHA, usable as a ref without being a branch/tag.
_SHA = re.compile(r"[0-9a-f]{7,40}")


def parse_github_title(url: str) -> str | None:
    """Derive an `owner/repo` title from a github.com URL path. Returns None
    if the path lacks at least owner + repo."""
    segments = [s for s in urlsplit(url).path.split("/") if s]
    if len(segments) < 2:
        return None
    return f"{segments[0]}/{segments[1]}"


def parse_github_url(url: str) -> tuple[str, str, str | None, list[str]]:
    """`(owner, repo, kind, segments)` for a supported github.com URL.

    `kind` is None for a bare repository URL, else `"tree"` or `"blob"`, and
    `segments` is everything after it: the ref followed (for a blob) by the
    file path. Where the ref ends is only known once the repository's refs
    are looked up. The path is percent-decoded here, exactly once.
    """
    parts = urlsplit(url or "")
    if parts.hostname not in _HOSTS:
        raise SourceNotFound("not a github.com url")
    segments = [s for s in unquote(parts.path).split("/") if s]
    if len(segments) < 2:
        raise SourceNotFound("github url has no owner/repo")
    owner, repo = segments[0], segments[1].removesuffix(".git")
    rest = segments[2:]
    if not rest:
        return owner, repo, None, []
    if rest[0] == "tree" and len(rest) >= 2:
        return owner, repo, "tree", rest[1:]
    if rest[0] == "blob" and len(rest) >= 3:
        return owner, repo, "blob", rest[1:]
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
        owner, repo, kind, segments = parse_github_url(url)
        client = self._client or httpx.Client(timeout=self._request_timeout_s)
        try:
            ref, rest = (None, [])
            if kind is not None:
                ref, rest = self._resolve_ref(client, owner, repo, segments)
            if kind == "blob":
                if not rest:
                    raise SourceUnsupported("github blob url has no file path")
                return self._fetch_blob(client, owner, repo, ref, "/".join(rest))
            if rest:
                # A /tree/ URL into a subdirectory: only repository READMEs.
                raise SourceUnsupported("only github repository and /blob/ file urls are supported")
            return self._fetch_readme(client, owner, repo, ref)
        finally:
            if self._client is None:
                client.close()

    def _resolve_ref(
        self, client: httpx.Client, owner: str, repo: str, segments: list[str]
    ) -> tuple[str, list[str]]:
        """Split `segments` into `(ref, path segments)` using the refs the
        repository really has. The longest branch or tag that prefixes the
        segments wins, so `feature/docs` beats `feature` when both exist.
        A nonexistent ref is SourceNotFound, never a fallback."""
        first = segments[0]
        names: set[str] = set()
        for namespace in ("heads", "tags"):
            refs = self._get_json(
                client,
                f"/repos/{_seg(owner)}/{_seg(repo)}/git/matching-refs/{namespace}/{_seg(first)}",
                None,
            )
            if not isinstance(refs, list):
                raise SourceUnavailable("github returned an unexpected refs listing")
            prefix = f"refs/{namespace}/"
            for r in refs:
                name = r.get("ref", "") if isinstance(r, dict) else ""
                if name.startswith(prefix):
                    names.add(name.removeprefix(prefix))
        for n in range(len(segments), 0, -1):
            candidate = "/".join(segments[:n])
            if candidate in names:
                return candidate, segments[n:]
        if _SHA.fullmatch(first):
            return first, segments[1:]
        raise SourceNotFound("github ref not found")

    def _fetch_readme(
        self, client: httpx.Client, owner: str, repo: str, ref: str | None
    ) -> SourceResult:
        title = f"{owner}/{repo}"
        endpoint = f"/repos/{_seg(owner)}/{_seg(repo)}/readme"
        try:
            text = self._read_text(client, endpoint, ref)
        except SourceNotFound:
            # GitHub uses 404 for both "no README" and "no such repo" (and for
            # a private repo this token cannot see). Any ref was already
            # resolved, so only a repo that resolves is a README-less repo;
            # otherwise the 404 stands.
            self._get_json(client, f"/repos/{_seg(owner)}/{_seg(repo)}", None)
            return SourceResult(title=title, content=None, warnings=["repository has no README"])
        return SourceResult(title=title, content=text[: self._max_content_chars])

    def _fetch_blob(
        self, client: httpx.Client, owner: str, repo: str, ref: str | None, path: str
    ) -> SourceResult:
        endpoint = f"/repos/{_seg(owner)}/{_seg(repo)}/contents/{quote(path)}"
        text = self._read_text(client, endpoint, ref)
        return SourceResult(title=f"{owner}/{repo}/{path}", content=text[: self._max_content_chars])

    def _read_text(self, client: httpx.Client, endpoint: str, ref: str | None) -> str:
        """Stream the file in the raw media type, stopping as soon as it is
        known to exceed `max_file_bytes`, and return it decoded as text."""
        with self._stream(client, endpoint, ref, accept=_RAW) as resp:
            # A file comes back as the raw media type; a directory, submodule
            # or symlink comes back as a JSON listing/object instead.
            if not resp.headers.get("content-type", "").startswith("application/vnd.github.raw"):
                raise SourceUnsupported("github url does not point at a file")
            length = resp.headers.get("content-length")
            if length is not None and length.isdigit() and int(length) > self._max_file_bytes:
                raise SourceUnsupported(
                    f"github file is {length} bytes, over the limit of {self._max_file_bytes} bytes"
                )
            data = bytearray()
            try:
                for chunk in resp.iter_bytes():
                    data += chunk
                    if len(data) > self._max_file_bytes:
                        raise SourceUnsupported(
                            f"github file exceeds the limit of {self._max_file_bytes} bytes"
                        )
            except httpx.HTTPError as e:
                raise SourceUnavailable(f"github unreachable: {type(e).__name__}") from e
        if b"\x00" in data:
            raise SourceUnsupported("github file is binary, not text")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            raise SourceUnsupported("github file is not utf-8 text")

    def _get_json(self, client: httpx.Client, endpoint: str, ref: str | None):
        with self._stream(client, endpoint, ref, accept=_JSON) as resp:
            try:
                resp.read()
                return resp.json()
            except httpx.HTTPError as e:
                raise SourceUnavailable(f"github unreachable: {type(e).__name__}") from e
            except ValueError as e:
                raise SourceUnavailable("github returned an unparseable body") from e

    @contextmanager
    def _stream(
        self, client: httpx.Client, endpoint: str, ref: str | None, *, accept: str
    ) -> Iterator[httpx.Response]:
        headers = {"Accept": accept, "X-GitHub-Api-Version": _API_VERSION}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        params = {"ref": ref} if ref else None
        # GitHub answers a renamed/transferred repository with a 301 to its
        # new location; follow it rather than reading the redirect body.
        request = client.build_request(
            "GET", f"{self._api_base_url}{endpoint}", headers=headers, params=params
        )
        try:
            resp = client.send(request, stream=True, follow_redirects=True)
        except httpx.HTTPError as e:
            raise SourceUnavailable(f"github unreachable: {type(e).__name__}") from e
        try:
            _raise_for_status(resp)
            yield resp
        finally:
            resp.close()


def _seg(value: str) -> str:
    return quote(value, safe="")


def _raise_for_status(resp: httpx.Response) -> None:
    status = resp.status_code
    if 200 <= status < 300:
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
    # Includes a 3xx left over after following redirects (e.g. a loop).
    raise SourceUnavailable(f"github returned {status}")
