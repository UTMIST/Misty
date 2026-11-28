from uuid import UUID

import httpx

from contracts.documents import DocumentsUnavailable

# Each documentation-system call gets about 15 seconds, inside the ~20-second
# tool budget in docs/MISTY-MCP.md. Search makes a paid embedding call, so it is
# slower than the directory's lookups.
_TIMEOUT = httpx.Timeout(15.0)

# documentation-system's SearchRequest limits (contracts/retrieval.py there).
# Checked here so a bad query or k is a caller error, not a fake outage.
MAX_K = 50
MAX_QUERY_CHARS = 2000

# The body documentation-system sends for a missing or invisible doc. Any other
# 404 (a wrong DOC_BASE_URL, a proxy's "Application not found") is a fault.
_DOC_NOT_FOUND = {"detail": "doc not found"}


class HttpDocumentsClient:
    """Searches and reads documentation-system on behalf of one member.

    A 404 with documentation-system's own "doc not found" body means 'no such
    doc, or not visible to this person' (returns None; documentation-system
    deliberately makes the two look the same). Any other failure, including a
    404 from something else, raises DocumentsUnavailable without upstream
    detail. A query or k that documentation-system would reject raises
    ValueError before any request. The gateway never retries on its own.

    Holds one pooled httpx.Client for its whole lifetime, like
    HttpDirectoryClient. src/api/deps.py owns one instance per app lifespan.
    """

    def __init__(self, base_url: str, api_key: str, client: httpx.Client | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=_TIMEOUT)
        self._client.headers["X-API-Key"] = api_key

    def close(self) -> None:
        self._client.close()

    def _send(self, method: str, path: str, person_id: UUID, **kwargs) -> httpx.Response:
        # str(UUID(...)) so only a well-formed UUID can reach the header.
        headers = {"X-On-Behalf-Of": str(UUID(str(person_id)))}
        try:
            return self._client.request(
                method, f"{self._base_url}{path}", headers=headers, **kwargs
            )
        except httpx.HTTPError:
            # Transport exceptions can contain URLs; keep logs free of upstream data.
            raise DocumentsUnavailable("documentation-system unreachable") from None

    @staticmethod
    def _json(resp: httpx.Response):
        try:
            return resp.json()
        except ValueError:
            raise DocumentsUnavailable("invalid documentation-system JSON") from None

    @staticmethod
    def _not_found(resp: httpx.Response) -> bool:
        try:
            return resp.json() == _DOC_NOT_FOUND
        except ValueError:
            return False

    def search(self, person_id: UUID, query: str, k: int = 10) -> list[dict]:
        if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_CHARS:
            raise ValueError(f"query must be nonblank and at most {MAX_QUERY_CHARS} characters")
        if "\x00" in query:
            raise ValueError("query must not contain NUL characters")
        if type(k) is not int or not 1 <= k <= MAX_K:
            raise ValueError(f"k must be an integer from 1 to {MAX_K}")
        resp = self._send("POST", "/search", person_id, json={"query": query, "k": k})
        if not (200 <= resp.status_code < 300):
            raise DocumentsUnavailable(f"documentation-system search returned {resp.status_code}")
        body = self._json(resp)
        results = body.get("results") if isinstance(body, dict) else None
        if not isinstance(results, list) or not all(
            isinstance(item, dict) and _is_uuid(item.get("doc_id")) for item in results
        ):
            raise DocumentsUnavailable("invalid documentation-system search response")
        return results

    def get_doc(self, person_id: UUID, doc_id: UUID) -> dict | None:
        resp = self._send("GET", f"/docs/{UUID(str(doc_id))}", person_id)
        if resp.status_code == 404:
            if self._not_found(resp):
                return None
            raise DocumentsUnavailable("documentation-system doc route not found")
        if not (200 <= resp.status_code < 300):
            raise DocumentsUnavailable(f"documentation-system returned {resp.status_code}")
        body = self._json(resp)
        if not isinstance(body, dict) or not isinstance(body.get("id"), str):
            raise DocumentsUnavailable("invalid documentation-system doc response")
        return body


def _is_uuid(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        UUID(value)
    except ValueError:
        return False
    return True
