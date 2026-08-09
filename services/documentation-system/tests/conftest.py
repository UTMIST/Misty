from datetime import datetime, timezone

import pytest

from contracts.types import Source


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch):
    """Never let the suite read a developer's local .env.

    Settings.model_config declares env_file=".env", so any test that builds
    Settings() or calls get_settings() would otherwise silently inherit
    whatever a developer has locally configured (e.g. a real API_KEY,
    DIRECTORY_API_KEY or DATABASE_URL), producing failures unrelated to the
    change under test — and, before these fields became SecretStr, printing
    those real values into assertion diffs. Neutralizing env_file here — before
    any cached Settings are built — makes the suite hermetic regardless of
    what's on disk.

    Residual gap, not closed by this fixture: `src/api/app.py` does
    `app = create_app()` at import time, which calls verify_production_secrets()
    and so builds Settings() (reading the real `.env`, if any) during test
    collection — before this fixture, or any fixture, has run. This fixture
    also only neutralizes `.env`; it does not unset process-level env vars a
    developer may have exported (e.g. a real DOCS_ENV or API_KEY in their
    shell). Neither leaks a secret any more (the three credentials no longer
    stringify), so this is a correctness/isolation gap rather than a security
    one, but "hermetic" overstates what's actually guaranteed.
    """
    from src.config import Settings

    monkeypatch.setitem(Settings.model_config, "env_file", None)


@pytest.fixture(autouse=True)
def _clear_settings_cache(_no_dotenv):
    """Rebuild Settings per test, and only ever after _no_dotenv has run.

    Depends on _no_dotenv rather than merely coexisting with it: get_settings
    is lru_cached, so clearing the cache while `.env` is still live would just
    rebuild Settings from the developer's real file. Test-module fixtures that
    set env vars and then call get_settings.cache_clear() themselves still work
    — this only guarantees they start and end from a clean cache.
    """
    from src.api import deps
    from src.config import get_settings

    get_settings.cache_clear()
    deps._directory_client.cache_clear()
    yield
    get_settings.cache_clear()
    deps._directory_client.cache_clear()


# (id, label, url_patterns, requires_auth, has_api, content_fetch_enabled) — matches migration 002.
_SEED = [
    ("web", "Web page", [], False, False, True),
    ("github", "GitHub", ["github.com"], False, True, True),
    ("gdrive", "Google Drive", ["drive.google.com"], True, True, False),
    ("gdocs", "Google Docs", ["docs.google.com/document"], True, True, False),
    ("gsheets", "Google Sheets", ["docs.google.com/spreadsheets"], True, True, False),
    ("gslides", "Google Slides", ["docs.google.com/presentation"], True, True, False),
    ("notion", "Notion", ["notion.so", "notion.site"], True, True, False),
    ("youtube", "YouTube", ["youtube.com", "youtu.be"], False, True, False),
]


def build_seed_sources() -> list[Source]:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        Source(
            id=s[0],
            label=s[1],
            url_patterns=s[2],
            requires_auth=s[3],
            has_api=s[4],
            content_fetch_enabled=s[5],
            active=True,
            created_at=now,
            updated_at=now,
            created_by="system",
            updated_by="system",
        )
        for s in _SEED
    ]
