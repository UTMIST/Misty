from typing import Protocol
from uuid import UUID


class DocumentsUnavailable(Exception):
    """Raised when documentation-system is unreachable or its response is unusable."""


class DocumentsClient(Protocol):
    """Reads documentation-system as one member, never with catalog-wide access.

    Every call asserts the member with `X-On-Behalf-Of`, so documentation-system
    returns only what that person may see. The person must come from the
    authenticated session, never from a tool argument.

    `search` raises ValueError for a blank query, one over 2000 characters, or k
    outside 1..50 (documentation-system's own limits), so bad input never looks
    like an outage. DocumentsUnavailable means documentation-system failed.
    """

    def search(self, person_id: UUID, query: str, k: int = 10) -> list[dict]: ...
    def get_doc(self, person_id: UUID, doc_id: UUID) -> dict | None: ...
    def close(self) -> None: ...
