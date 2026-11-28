"""An in-memory documentation-system client for tests.

Inject it with `app.dependency_overrides[get_documents] = lambda: fake`. All
data is synthetic. Each document lists the person ids allowed to see it, so a
test can check that a caller only gets what that person may see.
"""

from uuid import UUID

from contracts.documents import DocumentsUnavailable
from src.documents.http_client import MAX_K, MAX_QUERY_CHARS


class FakeDocumentsClient:
    def __init__(self, docs: list[dict] | None = None, *, down: bool = False) -> None:
        # Each doc: {"id": str, "title": str, "url": str, "text": str, "visible_to": [person ids]}
        self._docs = docs or []
        self.down = down
        self.calls: list[tuple] = []
        self.closed = False

    def _visible(self, person_id: UUID) -> list[dict]:
        return [d for d in self._docs if str(person_id) in d.get("visible_to", [])]

    def search(self, person_id: UUID, query: str, k: int = 10) -> list[dict]:
        # Same input rules as the real client, so a test can't pass on input
        # documentation-system would reject.
        if not query.strip() or len(query) > MAX_QUERY_CHARS or "\x00" in query:
            raise ValueError("invalid query")
        if type(k) is not int or not 1 <= k <= MAX_K:
            raise ValueError("invalid k")
        self.calls.append(("search", str(person_id), query, k))
        if self.down:
            raise DocumentsUnavailable("fake documentation-system down")
        terms = query.lower().split()
        hits = [
            d
            for d in self._visible(person_id)
            if all(t in d.get("text", "").lower() for t in terms)
        ]
        return [
            {"doc_id": d["id"], "title": d.get("title"), "url": d.get("url"), "text": d["text"]}
            for d in hits[:k]
        ]

    def get_doc(self, person_id: UUID, doc_id: UUID) -> dict | None:
        self.calls.append(("get_doc", str(person_id), str(doc_id)))
        if self.down:
            raise DocumentsUnavailable("fake documentation-system down")
        for d in self._visible(person_id):
            if d["id"] == str(doc_id):
                return {k: v for k, v in d.items() if k != "visible_to"}
        return None

    def close(self) -> None:
        self.closed = True
