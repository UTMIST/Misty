import pytest

from src.storage.in_memory import InMemoryStorageAdapter
from storage_cases import StorageCases


@pytest.fixture
def adapter():
    return InMemoryStorageAdapter()


class TestInMemoryStorage(StorageCases):
    pass
