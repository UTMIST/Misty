from urllib.parse import quote

import httpx

from contracts.directory import DirectoryUnavailable

_TIMEOUT = httpx.Timeout(5.0)


class HttpDirectoryClient:
    """Looks up people and identifiers over team-tracking's HTTP API. A 404
    means 'no such record' (returns None); connection failure or 5xx means
    'directory unavailable' (raises DirectoryUnavailable)."""

    def __init__(self, base_url: str, api_key: str, client: httpx.Client | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._client = client

    def _get(self, path: str):
        client = self._client or httpx.Client(timeout=_TIMEOUT)
        try:
            resp = client.get(f"{self._base_url}{path}", headers={"X-API-Key": self._api_key})
        except httpx.HTTPError as e:
            raise DirectoryUnavailable(f"directory unreachable: {e}") from e
        finally:
            if self._client is None:
                client.close()
        if resp.status_code == 404:
            return None
        if not (200 <= resp.status_code < 300):
            raise DirectoryUnavailable(f"directory returned {resp.status_code}")
        return resp.json()

    def get_person_by_github(self, github_login: str) -> dict | None:
        return self._get(f"/people/by-identifier/github/{quote(github_login, safe='')}")

    def list_identifiers(self, person_id: str) -> list[dict]:
        result = self._get(f"/people/{person_id}/identifiers")
        return result or []
