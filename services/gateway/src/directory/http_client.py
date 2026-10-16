from urllib.parse import quote

import httpx

from contracts.directory import DirectoryUnavailable

_TIMEOUT = httpx.Timeout(5.0)


class HttpDirectoryClient:
    """Looks up people and identifiers over team-tracking's HTTP API. A 404
    means 'no such record' (returns None); connection failure or 5xx means
    'directory unavailable' (raises DirectoryUnavailable).

    Holds one pooled httpx.Client for its whole lifetime. The resolver makes two
    directory calls per request, so a client built (and closed) per call would
    mean two fresh TCP + TLS handshakes on the hot path. Construct this once —
    src/api/deps.py caches the instance — rather than per request.
    """

    def __init__(self, base_url: str, api_key: str, client: httpx.Client | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        # Set once as a default header so the key never has to be rebuilt (or
        # accidentally logged) per call. It is never read back out.
        self._client = client or httpx.Client(timeout=_TIMEOUT)
        self._client.headers["X-API-Key"] = api_key

    def _get(self, path: str):
        try:
            resp = self._client.get(f"{self._base_url}{path}")
        except httpx.HTTPError:
            # Transport exceptions can contain URLs; keep logs free of upstream data.
            raise DirectoryUnavailable("directory unreachable") from None
        if resp.status_code == 404:
            return None
        if not (200 <= resp.status_code < 300):
            raise DirectoryUnavailable(f"directory returned {resp.status_code}")
        try:
            result = resp.json()
        except ValueError:
            raise DirectoryUnavailable("invalid directory JSON") from None
        if result is None:
            raise DirectoryUnavailable("empty directory response")
        return result

    def get_person_by_github(self, github_login: str) -> dict | None:
        result = self._get(f"/people/by-identifier/github/{quote(github_login, safe='')}")
        if result is not None and (
            not isinstance(result, dict)
            or not isinstance(result.get("id"), str)
            or not result["id"]
        ):
            raise DirectoryUnavailable("invalid directory person response")
        return result

    def list_identifiers(self, person_id: str) -> list[dict]:
        result = self._get(f"/people/{quote(person_id, safe='')}/identifiers")
        if result is None:
            return []
        if not isinstance(result, list) or any(
            not isinstance(item, dict)
            or not isinstance(item.get("provider"), str)
            or not isinstance(item.get("external_id"), str)
            or not item["external_id"]
            for item in result
        ):
            raise DirectoryUnavailable("invalid directory identifiers response")
        return result
