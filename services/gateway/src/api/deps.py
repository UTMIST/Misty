from contextlib import asynccontextmanager
from functools import lru_cache
from threading import Lock

from fastapi import FastAPI, Request
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from starlette.concurrency import run_in_threadpool

from contracts.directory import DirectoryClient
from contracts.documents import DocumentsClient
from contracts.storage import StorageAdapter
from contracts.login import VerificationClient
from src.config import get_settings


@lru_cache(maxsize=1)
def _default_engine() -> Engine:
    return create_engine(get_settings().database_url, future=True, pool_pre_ping=True)


def _build_directory() -> DirectoryClient:
    from src.directory.http_client import HttpDirectoryClient

    s = get_settings()
    # .get_secret_value() at the boundary: HttpDirectoryClient puts this
    # straight into an outbound header, which needs the raw str. This is one of
    # the two sanctioned unwrap sites (the other is verify_production_secrets);
    # everywhere else the field stays wrapped.
    return HttpDirectoryClient(s.directory_base_url, s.directory_api_key.get_secret_value())


def get_storage() -> StorageAdapter:
    from src.storage.postgres import PostgresStorageAdapter

    return PostgresStorageAdapter(_default_engine())


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.directory = None
    app.state.directory_lock = Lock()
    app.state.verification = None
    app.state.verification_lock = Lock()
    app.state.documents = None
    app.state.documents_lock = Lock()
    try:
        yield
    finally:
        if app.state.directory is not None:
            await run_in_threadpool(app.state.directory.close)
            app.state.directory = None
        if app.state.verification is not None:
            await run_in_threadpool(app.state.verification.close)
            app.state.verification = None
        if app.state.documents is not None:
            await run_in_threadpool(app.state.documents.close)
            app.state.documents = None


def get_directory(request: Request) -> DirectoryClient:
    # Construct lazily after scope checks, once even for concurrent first calls.
    # The app owns the pool and closes it after in-flight requests have drained.
    with request.app.state.directory_lock:
        if request.app.state.directory is None:
            request.app.state.directory = _build_directory()
        return request.app.state.directory


def get_verification(request: Request) -> VerificationClient:
    from src.verification.http_client import HttpVerificationClient

    with request.app.state.verification_lock:
        if request.app.state.verification is None:
            settings = get_settings()
            request.app.state.verification = HttpVerificationClient(
                settings.verification_base_url,
                settings.verification_api_key.get_secret_value(),
            )
        return request.app.state.verification


def get_documents(request: Request) -> DocumentsClient:
    # Same lazy, once-per-lifespan pool as get_directory. The doc_ key is
    # unwrapped only here, where it becomes the outbound header.
    from src.documents.http_client import HttpDocumentsClient

    with request.app.state.documents_lock:
        if request.app.state.documents is None:
            settings = get_settings()
            request.app.state.documents = HttpDocumentsClient(
                settings.doc_base_url, settings.doc_api_key.get_secret_value()
            )
        return request.app.state.documents
