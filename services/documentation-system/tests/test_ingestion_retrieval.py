"""Offline index-to-retrieval acceptance; Postgres runs the same cases."""

import pytest

from src.storage.in_memory import InMemoryStorageAdapter
from tests.conftest import build_seed_sources
from tests.ingestion_retrieval_cases import IngestionRetrievalCases


@pytest.fixture
def adapter():
    return InMemoryStorageAdapter(seed_sources=build_seed_sources())


class TestIngestionRetrieval(IngestionRetrievalCases):
    pass
