"""Shared validation for the two chunk-storage adapters (no I/O)."""

from contracts.types import DocChunk


def validate_doc_chunks(chunks: list[DocChunk]) -> list[DocChunk]:
    # Revalidate even existing models: model_copy can bypass construction-time
    # validation, including the immutable vector representation and text limit.
    validated = sorted(
        (DocChunk.model_validate(chunk) for chunk in chunks), key=lambda chunk: chunk.ordinal
    )
    if [chunk.ordinal for chunk in validated] != list(range(len(validated))):
        raise ValueError("chunk ordinals must be exactly 0..n-1")
    if (
        len(
            {
                (chunk.source_content_hash, chunk.source_title_hash, chunk.embedding_model)
                for chunk in validated
            }
        )
        > 1
    ):
        raise ValueError("chunks must share one source content/title hash and embedding model")
    return validated


def validate_chunks_for_content(
    chunks: list[DocChunk], content_hash: str, title_hash: str
) -> list[DocChunk]:
    validated = validate_doc_chunks(chunks)
    if any(chunk.source_content_hash != content_hash for chunk in validated):
        raise ValueError("chunks must be built from the expected content hash")
    if any(chunk.source_title_hash != title_hash for chunk in validated):
        raise ValueError("chunks must be built from the expected title hash")
    return validated
