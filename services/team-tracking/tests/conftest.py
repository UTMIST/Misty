from collections.abc import Iterator
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from contracts.types import Provider, RoleKind
from src.config import get_settings


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch):
    """Never let the suite read a developer's local .env.

    Settings.model_config declares env_file=".env", so any test that builds
    Settings() or calls get_settings() would otherwise silently inherit
    whatever a developer has locally configured (e.g. a real API_KEY, or a
    DATABASE_URL pointing at staging), producing failures — or leaks —
    unrelated to the change under test. Neutralizing env_file here makes the
    suite hermetic regardless of what's on disk.

    Residual gaps, not closed by this fixture:
    - `src/api/app.py` does `app = create_app()` at import time, which builds
      Settings() (reading the real `.env`, if any) and runs
      verify_production_secrets() during test collection — before this
      fixture, or any fixture, has run.
    - It only neutralizes `.env`; it does not unset process-level env vars a
      developer may have exported (e.g. a real API_KEY in their shell). CI
      relies on exactly that: it passes DATABASE_URL as a process env var.
    - The session-scoped `engine` fixture below is instantiated before any
      function-scoped fixture, so the Postgres tests resolve DATABASE_URL
      with `.env` still live. That is deliberate — CI supplies DATABASE_URL
      via the environment, and a local run should keep pointing at whatever
      dev database the developer configured.
    None of these leaks a secret (api_key no longer stringifies), so they are
    isolation gaps rather than security ones, but "hermetic" overstates what
    is actually guaranteed.
    """
    from src.config import Settings

    monkeypatch.setitem(Settings.model_config, "env_file", None)


@pytest.fixture(autouse=True)
def _clear_settings_cache(_no_dotenv):
    """Rebuild Settings for every test, after .env has been neutralized.

    Depends on _no_dotenv (rather than merely coexisting with it) so the
    ordering is explicit: the cache is only ever repopulated once the env file
    is out of the picture. Individual tests still call get_settings.cache_clear()
    themselves after monkeypatching env vars mid-fixture; this just guarantees
    no Settings instance survives across test boundaries.
    """
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def build_seed_providers() -> list[Provider]:
    """Standard providers seed for tests — matches migrations 004 and 006."""
    now = datetime.now(timezone.utc)
    return [
        Provider(
            id=pid,
            label=plabel,
            description=None,
            active=True,
            created_at=now,
            updated_at=now,
            created_by="system",
            updated_by="system",
        )
        for pid, plabel in [
            ("discord", "Discord"),
            ("github", "GitHub"),
            ("notion", "Notion"),
            ("uoft_email", "UofT Email"),
            ("email", "Email"),
        ]
    ]


def build_seed_role_kinds() -> list[RoleKind]:
    """Standard role_kinds seed set for tests — matches migration 002."""
    now = datetime.now(timezone.utc)
    return [
        RoleKind(
            id=rid,
            label=rlabel,
            description=None,
            active=True,
            created_at=now,
            updated_at=now,
            created_by="system",
            updated_by="system",
        )
        for rid, rlabel in [
            ("executive", "Executive"),
            ("director", "Director"),
            ("lead", "Lead"),
            ("member", "Member"),
        ]
    ]


@pytest.fixture(scope="session")
def engine() -> Engine:
    return create_engine(get_settings().database_url, future=True)


@pytest.fixture
def clean_db(engine: Engine) -> Iterator[Engine]:
    """Truncate mutable tables before each Postgres adapter test.

    Keeps role_kinds intact (seeded via migration).
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE person_identifiers, team_memberships, teams, people, api_keys "
                "RESTART IDENTITY CASCADE"
            )
        )
    yield engine
