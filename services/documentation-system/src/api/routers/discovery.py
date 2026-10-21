"""Import new Drive identities through the normal ingest/index pipeline."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from contracts.directory import DirectoryClient
from contracts.embedder import Embedder
from contracts.storage import StorageAdapter
from contracts.types import Doc, DocIngest
from src.api.auth import AuthedKey, get_actor, require_scope
from src.api.deps import get_directory, get_embedder, get_fetchers, get_storage
from src.config import get_settings
from src.content import content_hash
from src.fetch.registry import FetcherRegistry
from src.indexing import index_doc
from src.ingest import ingest_doc

router = APIRouter(prefix="/docs/discovery", tags=["docs"])


class DiscoveryImport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$", max_length=200)
    source_id: Literal["gdocs", "gsheets", "gslides", "gdrive"]


class DiscoveryResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["indexed", "registered", "index_failed", "existing", "retired", "disabled"]
    doc_id: UUID | None = None
    warnings: int = 0


def _existing_result(doc: Doc, storage: StorageAdapter) -> DiscoveryResult:
    if not doc.active:
        return DiscoveryResult(status="retired", doc_id=doc.id)
    content = storage.get_doc_content_meta(doc.id)
    if content is None:
        return DiscoveryResult(status="registered", doc_id=doc.id)
    index = storage.get_doc_index_state(doc.id)
    current = (
        index is not None
        and index.source_content_hash == content.content_hash
        and index.source_title_hash == content_hash(doc.title or "")
    )
    return DiscoveryResult(status="existing" if current else "index_failed", doc_id=doc.id)


@router.post("", response_model=DiscoveryResult)
def import_discovered(
    payload: DiscoveryImport,
    storage: StorageAdapter = Depends(get_storage),
    fetchers: FetcherRegistry = Depends(get_fetchers),
    directory: DirectoryClient = Depends(get_directory),
    embedder: Embedder = Depends(get_embedder),
    actor: str = Depends(get_actor),
    _: AuthedKey = Depends(require_scope("docs:write")),
) -> DiscoveryResult:
    source = storage.get_source(payload.source_id)
    if source is None or not source.active or not source.content_fetch_enabled:
        return DiscoveryResult(status="disabled")
    existing = storage.get_doc_by_google_file_id(payload.file_id)
    if existing is not None:
        return _existing_result(existing, storage)
    result = ingest_doc(
        DocIngest(
            url=f"https://drive.google.com/file/d/{payload.file_id}",
            source_id=payload.source_id,
        ),
        storage=storage,
        fetchers=fetchers,
        directory=directory,
        actor=actor,
        source_access_max_age_hours=get_settings().source_access_max_age_hours,
        refresh_existing=False,
    )
    if not result.created:
        return _existing_result(result.doc, storage)
    outcome = index_doc(result.doc.id, storage=storage, embedder=embedder)
    status = (
        "indexed"
        if outcome.status in {"indexed", "unchanged"}
        else "index_failed"
        if outcome.status == "failed"
        else "registered"
    )
    return DiscoveryResult(status=status, doc_id=result.doc.id, warnings=len(result.warnings))
