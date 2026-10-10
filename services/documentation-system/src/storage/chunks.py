"""Shared validation for the two chunk-storage adapters (no I/O)."""

from contracts.types import DocChunk


def validate_doc_chunks(chunks: list[DocChunk]) -> list[DocChunk]:
    # Revalidate even existing models: model_copy and mutable vector lists can
    # otherwise bypass their construction-time validation.
    validated = sorted(
        (DocChunk.model_validate(chunk) for chunk in chunks), key=lambda chunk: chunk.ordinal
    )
    if [chunk.ordinal for chunk in validated] != list(range(len(validated))):
        raise ValueError("chunk ordinals must be exactly 0..n-1")
    if len({(chunk.source_content_hash, chunk.embedding_model) for chunk in validated}) > 1:
        raise ValueError("chunks must share one source content hash and embedding model")
    return validated
