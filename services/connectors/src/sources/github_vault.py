"""Connector-owned encrypted SQLite vault. One volume, one worker; no plaintext tokens."""

import json
import os
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from contracts.github_oauth import GitHubOAuthError


class VaultTransaction:
    def __init__(self, conn, cipher):
        self.conn, self.cipher = conn, cipher

    def get(self, key):
        row = self.conn.execute("SELECT value FROM github_vault WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        try:
            return json.loads(self.cipher.decrypt(row[0]))
        except (InvalidToken, ValueError):
            raise GitHubOAuthError("vault_unavailable") from None

    def set(self, key, data):
        value = self.cipher.encrypt(json.dumps(data).encode())
        self.conn.execute(
            "INSERT INTO github_vault(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def delete(self, key):
        self.conn.execute("DELETE FROM github_vault WHERE key = ?", (key,))

    def keys(self, prefix):
        return [
            r[0]
            for r in self.conn.execute(
                "SELECT key FROM github_vault WHERE key LIKE ?", (prefix + "%",)
            )
        ]


class GitHubVault:
    def __init__(self, path: str, key: str):
        if not path or not key:
            raise GitHubOAuthError("not_configured")
        self.path = path
        try:
            self.cipher = Fernet(key.encode())
        except (ValueError, TypeError):
            raise GitHubOAuthError("vault_key_invalid") from None
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        # Create with restrictive mode; don't chmod an unrelated pre-existing file.
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        with closing(sqlite3.connect(path)) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS github_vault (key TEXT PRIMARY KEY, value BLOB NOT NULL)"
            )
            conn.commit()

    @contextmanager
    def transaction(self):
        # BEGIN IMMEDIATE also serializes refreshes, callbacks and unlink across processes.
        # All external calls have finite timeouts; no transaction is shared between threads.
        conn = sqlite3.connect(self.path, timeout=5)
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield VaultTransaction(conn, self.cipher)
            conn.commit()
        except sqlite3.Error:
            conn.rollback()
            raise GitHubOAuthError("vault_unavailable") from None
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
