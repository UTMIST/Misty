import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.api.deps import get_email_sender, get_storage
from src.email.fake import FakeSender
from src.storage.in_memory import InMemoryVerificationStore

ENV_KEY = "test-vf-key"
AUTH = {"X-API-Key": ENV_KEY}


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch):
    """Never let the suite read a developer's local .env.

    Settings.model_config declares env_file=".env", so any test that builds
    Settings() or calls get_settings() would otherwise silently inherit
    whatever a developer has locally configured (e.g. a real
    GMAIL_CREDENTIALS_JSON), producing failures unrelated to the change under
    test. Neutralizing env_file here — before any cached Settings are built —
    makes the suite hermetic regardless of what's on disk.

    Residual gap, not closed by this fixture: `src/api/app.py` does
    `app = create_app()` at import time, which runs verify_production_secrets()
    and therefore builds Settings() (reading the real `.env`, if any) during
    test collection — before this fixture, or any fixture, has run. This
    fixture also only neutralizes `.env`; it does not unset process-level env
    vars a developer may have exported (e.g. a real GMAIL_CREDENTIALS_JSON in
    their shell). Neither leaks a secret (the credentials no longer
    stringify), so this is a correctness/isolation gap rather than a security
    one, but "hermetic" overstates what's actually guaranteed.
    """
    from src.config import Settings

    monkeypatch.setitem(Settings.model_config, "env_file", None)


@pytest.fixture(autouse=True)
def _clear_settings_cache(_no_dotenv):
    # get_settings() is lru_cached, so a Settings built by an earlier test (or
    # at app-import time, before _no_dotenv could run) would otherwise persist.
    from src.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def store() -> InMemoryVerificationStore:
    return InMemoryVerificationStore()


@pytest.fixture
def email() -> FakeSender:
    return FakeSender()


@pytest.fixture
def client(monkeypatch, store, email, _no_dotenv):
    # Depend on _no_dotenv explicitly so the env_file neutralization is in place
    # before this fixture's setenv/get_settings() rebuild, rather than racing it.
    monkeypatch.setenv("API_KEY", ENV_KEY)
    monkeypatch.setenv("CODE_HMAC_SECRET", "test-hmac-secret")
    from src.config import get_settings

    get_settings.cache_clear()
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: store
    app.dependency_overrides[get_email_sender] = lambda: email
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()
