import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("command,revision", [("upgrade", "head"), ("downgrade", "001:base")])
def test_offline_migrations_accept_encoded_database_credentials(command, revision):
    url = "postgresql+psycopg://gateway:synthetic%40%25%2Fpassword@localhost:5435/gateway"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", command, revision, "--sql"],
        cwd=Path(__file__).resolve().parents[1],
        env=os.environ | {"DATABASE_URL": url},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "api_keys" in result.stdout
    assert url not in result.stdout + result.stderr
    assert "synthetic" not in result.stdout + result.stderr
