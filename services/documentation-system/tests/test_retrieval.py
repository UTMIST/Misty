import pytest

from conftest import build_seed_sources
from src.storage.in_memory import InMemoryStorageAdapter
from tests.retrieval_cases import RetrievalCases


class TestRetrieval(RetrievalCases):
    @pytest.fixture
    def adapter(self):
        return InMemoryStorageAdapter(seed_sources=build_seed_sources())
