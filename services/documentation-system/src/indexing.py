"""Index a doc's stored content for retrieval: chunk, embed, replace (#175).

The one entry point, `index_doc`, is called inline after content is written
(ingest, refetch), after a title change or retirement, and by the admin POST /docs/index.
It is keyed on catalog identity (doc_id) and the stored doc_content text,
never on a URL or a fetch, so any catalogued item with stored text indexes the
same way.

Lifecycle guarantees, all from building the whole set in memory and writing it
with one conditional replacement:

- Unchanged content and title (same hashes, existing chunks) makes no provider call and no
  write, so re-indexing never duplicates.
- A new set replaces the old one entirely; shorter text leaves no tail chunks.
- An embedding or storage failure writes nothing. The previous set stays
  intact and consistent, and retrying is calling index_doc again.
- If content or title changes while we embed, the write is refused (`skipped`): the
  writer of the newer content/title triggers its own index.

Model changes (#209): each embedding model keeps its own complete set. Search
reads only the set for the model it serves; while a re-embed target is
configured, the target's set is built beside it, and sets for any other model
are deleted. A set that is current for its model is never re-embedded, so an
interrupted re-embed resumes where it stopped.
"""

import logging
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from contracts.embedder import Embedder, EmbedderRejected, EmbedderUnavailable
from contracts.storage import StorageAdapter
from contracts.types import DocChunk
from src.chunking import Chunk, ChunkSettings, chunk_text
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
    doc_id: UUID,
    *,
    storage: StorageAdapter,
    embedder: Embedder,
    target: Embedder | None = None,
    force: bool = False,
    settings: ChunkSettings = ChunkSettings(),
) -> IndexOutcome:
    """Bring one doc's chunk sets in line with its stored content and title.

    `embedder` produces the set search serves; `target`, during a re-embed,
    the set for the model search will switch to. Sets for any other model are
    deleted. `force` re-embeds even when a set's body/title hashes are
    unchanged. Never raises for provider or storage failures; they come back
    as `failed` so callers can report them without losing the catalog write
    that triggered indexing.

    `settings` chooses chunk size and overlap for the offline evaluation harness.
    Body/title hashes do not include those settings; changing them requires force=True.
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
        expected_title_hash = content_hash(doc.title or "")
        states = storage.get_doc_index_states(doc_id)
        embedders = index_embedders(embedder, target)
        keep = [e.model for e in embedders]
        due = [
            e
            for e in embedders
            if force
            or e.model not in states
            or states[e.model].source_content_hash != expected_hash
            or states[e.model].source_title_hash != expected_title_hash
        ]

        indexed_count = None
        if due:
            chunks = chunk_text(text, settings).chunks
            if not chunks:
                # Whitespace-only text: nothing to embed in any model. Still
                # conditional, so a concurrent write of real text is not wiped.
                written = storage.replace_doc_chunks_if_current(
                    doc_id, expected_hash, [], expected_title_hash=expected_title_hash
                )
                return IndexOutcome("cleared") if written else _skipped()
            inputs = [embedding_input(doc.title, chunk) for chunk in chunks]
            # The served set first: a failing re-embed target never holds back
            # the set search reads.
            for e in due:
                outcome = _write_set(
                    e, doc_id, storage, chunks, inputs, expected_hash, expected_title_hash
                )
                if outcome is not None:
                    return outcome
            indexed_count = len(chunks)

        if set(states) - set(keep):
            storage.delete_doc_chunks_except(doc_id, keep)
        if indexed_count is not None:
            return IndexOutcome("indexed", chunk_count=indexed_count)
        return IndexOutcome("unchanged", chunk_count=states[embedder.model].chunk_count)
    except (SQLAlchemyError, ValueError) as e:
        # ValueError: a chunk failed DocChunk validation. Neither error text is
        # safe to return (SQL, chunk text), so report only the type.
        return _failed(doc_id, f"index write failed: {type(e).__name__}")


def index_embedders(embedder: Embedder, target: Embedder | None) -> list[Embedder]:
    """The served model's embedder, then a distinct re-embed target if any."""
    if target is not None and target.model != embedder.model:
        return [embedder, target]
    return [embedder]


def _write_set(
    embedder: Embedder,
    doc_id: UUID,
    storage: StorageAdapter,
    chunks: list[Chunk],
    inputs: list[str],
    expected_hash: str,
    expected_title_hash: str,
) -> IndexOutcome | None:
    """Embed and store one model's set; an outcome only when it did not land."""
    try:
        batch = embedder.embed(inputs)
    except EmbedderUnavailable as e:
        return _failed(doc_id, f"embedding service unavailable for {embedder.model}: {e}")
    except EmbedderRejected as e:
        return _failed(doc_id, f"embedding request rejected for {embedder.model}: {e}")
    doc_chunks = [
        DocChunk(
            ordinal=chunk.ordinal,
            chunk_text=chunk.text,
            start_offset=chunk.start,
            end_offset=chunk.end,
            source_content_hash=expected_hash,
            source_title_hash=expected_title_hash,
            embedding=vector,
            embedding_model=batch.model,
            dimensions=batch.dimensions,
        )
        for chunk, vector in zip(chunks, batch.vectors, strict=True)
    ]
    if not storage.replace_doc_chunks_if_current(
        doc_id, expected_hash, doc_chunks, expected_title_hash=expected_title_hash
    ):
        return _skipped()
    return None


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
    return IndexOutcome("skipped", detail="content or title changed during indexing")


def _failed(doc_id: UUID, detail: str) -> IndexOutcome:
    logger.warning("indexing doc %s failed: %s", doc_id, detail)
    return IndexOutcome("failed", detail=detail)
