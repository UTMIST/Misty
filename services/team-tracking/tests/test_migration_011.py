"""Exercise provider cleanup with FK enforcement and vocabulary rollback."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text


def _load_migration(filename):
    path = Path(__file__).resolve().parents[1] / "migrations" / "versions" / filename
    spec = spec_from_file_location(filename.removesuffix(".py"), path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migration = _load_migration("011_remove_retired_provider.py")


def test_cleanup_deletes_only_retired_identifiers_and_round_trips(monkeypatch):
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys = ON"))
        conn.execute(
            text(
                "CREATE TABLE providers (id TEXT PRIMARY KEY, label TEXT, "
                "created_by TEXT, updated_by TEXT)"
            )
        )
        conn.execute(text("CREATE TABLE people (id TEXT PRIMARY KEY)"))
        conn.execute(
            text(
                "CREATE TABLE person_identifiers (person_id TEXT REFERENCES people(id), "
                "provider TEXT REFERENCES providers(id), external_id TEXT)"
            )
        )
        conn.execute(text("INSERT INTO people VALUES ('person')"))
        conn.execute(
            text("INSERT INTO providers VALUES (:id, :id, 'system', 'system')"),
            [{"id": pid} for pid in [migration.RETIRED_PROVIDER_ID, "discord", "email"]],
        )
        conn.execute(
            text("INSERT INTO person_identifiers VALUES ('person', :provider, 'external')"),
            [{"provider": pid} for pid in [migration.RETIRED_PROVIDER_ID, "discord", "email"]],
        )
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(conn)))
        for _ in range(2):
            migration.upgrade()
            assert conn.execute(text("SELECT id FROM providers ORDER BY id")).scalars().all() == [
                "discord",
                "email",
            ]
            assert conn.execute(
                text("SELECT provider FROM person_identifiers ORDER BY provider")
            ).scalars().all() == ["discord", "email"]
            assert conn.execute(text("SELECT id FROM people")).scalar_one() == "person"
            migration.downgrade()
            assert (
                conn.execute(
                    text("SELECT id FROM providers WHERE id = :id"),
                    {"id": migration.RETIRED_PROVIDER_ID},
                ).scalar_one()
                == migration.RETIRED_PROVIDER_ID
            )
        migration.upgrade()
    engine.dispose()
