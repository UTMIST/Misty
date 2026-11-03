import pytest

from login_storage_cases import LoginStorageCases
from src.storage.in_memory import InMemoryStorageAdapter


@pytest.fixture
def adapter():
    return InMemoryStorageAdapter()


class TestLoginStorage(LoginStorageCases):
    pass
