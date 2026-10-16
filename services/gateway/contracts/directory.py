from typing import Protocol


class DirectoryUnavailable(Exception):
    """Raised when team-tracking is unreachable or its response is unusable."""


class DirectoryClient(Protocol):
    def get_person_by_github(self, github_login: str) -> dict | None: ...
    def list_identifiers(self, person_id: str) -> list[dict]: ...
