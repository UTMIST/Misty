from conftest import build_seed_providers
from github_identity_contract import check_github_identity_contract
from src.storage.in_memory import InMemoryStorageAdapter


def test_verified_account_contract():
    check_github_identity_contract(InMemoryStorageAdapter(seed_providers=build_seed_providers()))
