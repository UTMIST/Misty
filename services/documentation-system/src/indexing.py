"""Index a doc's stored content for retrieval: chunk, embed, replace (#175).

The one entry point, `index_doc`, is called inline after content is written
(ingest, refetch), after a doc is retired, and by the admin POST /docs/index.
It is keyed on catalog identity (doc_id) and the stored doc_content text,
never on a URL or a fetch, so any catalogued item with stored text indexes the
same way.

Lifecycle guarantees, all from building the whole set in memory and writing it
with one conditional replacement:

- Unchanged content (same hash, existing chunks) makes no provider call and no
  write, so re-indexing never duplicates.
- A new set replaces the old one entirely; shorter text leaves no tail chunks.
- An embedding or storage failure writes nothing. The previous set stays
  intact and consistent, and retrying is calling index_doc again.
- If content changes while we embed, the write is refused (`skipped`): the
  writer of the newer content triggers its own index.
"""

import logging
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from contracts.embedder import Embedder, EmbedderRejected, EmbedderUnavailable
from contracts.storage import StorageAdapter
from contracts.types import DocChunk
from src.chunking import Chunk, chunk_text
from src.content import content_hash

logger = logging.getLogger(__name__)

# Context supplements the ~2,000-character source chunk. Bound it separately
# so long titles/headings cannot dominate each request or exceed /embed limits.
MAX_CONTEXT_CHARS = 1_000

IndexStatus = Literal["indexed", "unchanged", "cleared", "skipped", "failed"]


@dataclass(frozen=True)
class IndexOutcome:
    status: IndexStatus
    chunk_count: int = 0
    # A short, non-sensitive reason for `failed` and `skipped`.
    detail: str | None = None


def index_doc(
    doc_id: UUID, *, storage: StorageAdapter, embedder: Embedder, force: bool = False
) -> IndexOutcome:
    """Bring one doc's chunk set in line with its stored content.

    `force` re-embeds even when the content hash is unchanged (#209's
    model-change path). Never raises for provider or storage failures; they
    come back as `failed` so callers can report them without losing the
    catalog write that triggered indexing.
    """
    try:
        doc = storage.get_doc(doc_id)
        text = storage.get_doc_content(doc_id) if doc is not None and doc.active else None
        if text is None:
            # Recheck under the chunk writers' lock: a concurrent reactivation
            # or refetch may already have rebuilt the index since our read.
            cleared = storage.clear_doc_chunks_if_unindexable(doc_id)
            return IndexOutcome("cleared") if cleared else _skipped()

        # Hash the text we actually chunk, so the conditional write compares
        # the stored hash against exactly what these chunks were built from.
        expected_hash = content_hash(text)
        state = storage.get_doc_index_state(doc_id)
        if not force and state is not None and state.source_content_hash == expected_hash:
            return IndexOutcome("unchanged", chunk_count=state.chunk_count)

        chunks = chunk_text(text).chunks
        if not chunks:
            # Whitespace-only text: nothing to embed. Still conditional, so a
            # concurrent write of real text is not wiped.
            written = storage.replace_doc_chunks_if_current(doc_id, expected_hash, [])
            return IndexOutcome("cleared") if written else _skipped()

        batch = embedder.embed([embedding_input(doc.title, chunk) for chunk in chunks])
        doc_chunks = [
            DocChunk(
                ordinal=chunk.ordinal,
                chunk_text=chunk.text,
                start_offset=chunk.start,
                end_offset=chunk.end,
                source_content_hash=expected_hash,
                embedding=vector,
                embedding_model=batch.model,
                dimensions=batch.dimensions,
            )
            for chunk, vector in zip(chunks, batch.vectors, strict=True)
        ]
        if not storage.replace_doc_chunks_if_current(doc_id, expected_hash, doc_chunks):
            return _skipped()
        return IndexOutcome("indexed", chunk_count=len(doc_chunks))
    except EmbedderUnavailable as e:
        return _failed(doc_id, f"embedding service unavailable: {e}")
    except EmbedderRejected as e:
        return _failed(doc_id, f"embedding request rejected: {e}")
    except (SQLAlchemyError, ValueError) as e:
        # ValueError: a chunk failed DocChunk validation. Neither error text is
        # safe to return (SQL, chunk text), so report only the type.
        return _failed(doc_id, f"index write failed: {type(e).__name__}")


def embedding_input(title: str | None, chunk: Chunk) -> str:
    """The text sent to /embed for a chunk: the doc title and heading path as
    context, then the chunk. Added here, at index time, and never stored in
    chunk_text, so stored offsets still point at the exact source text. Only
    the added context is truncated; the source chunk is always kept whole."""
    # Slice each field before joining, avoiding large temporary strings for
    # every chunk in a document with an unusually long heading.
    context = [
        title[:MAX_CONTEXT_CHARS] if title else "",
        " > ".join(heading[:MAX_CONTEXT_CHARS] for heading in chunk.heading_path),
    ]
    prefix = "\n".join(part for part in context if part)[:MAX_CONTEXT_CHARS]
    return prefix + "\n\n" + chunk.text


def clear_doc_index(doc_id: UUID, *, storage: StorageAdapter) -> IndexOutcome:
    """Remove a doc's chunks (retirement, confirmed source deletion)."""
    try:
        storage.replace_doc_chunks(doc_id, [])
    except SQLAlchemyError as e:
        return _failed(doc_id, f"index write failed: {type(e).__name__}")
    return IndexOutcome("cleared")


def index_warning(outcome: IndexOutcome) -> str | None:
    """The ingest/refetch warning for an outcome that left the index behind."""
    if outcome.status != "failed":
        return None
    return f"indexing failed ({outcome.detail}); retry with POST /docs/index"


def _skipped() -> IndexOutcome:
    return IndexOutcome("skipped", detail="content changed during indexing")


def _failed(doc_id: UUID, detail: str) -> IndexOutcome:
    logger.warning("indexing doc %s failed: %s", doc_id, detail)
    return IndexOutcome("failed", detail=detail)
