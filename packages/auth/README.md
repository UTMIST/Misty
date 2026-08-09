# platform-auth

Shared API-key auth (keys, scopes, audit) for UTMIST platform services. A pure leaf package: depends only on `fastapi`/`starlette`/`argon2` plus stdlib, never on any service's code. Each service wires it in via `build_auth(...)`, passing its own storage (or in-memory) key store and config.

## Exports (`platform_auth/__init__.py`)

| Name | From | What it is |
|------|------|------------|
| `build_auth` | `factory.py` | Builds a service's `require_api_key` / `require_scope` / `get_actor` / `get_on_behalf_actor` FastAPI dependencies from its key store + config. |
| `AuthDeps` | `factory.py` | Dataclass bundling the four dependencies `build_auth` returns. |
| `AuditLogMiddleware` | `audit.py` | One structured JSON audit line per request to stdout, keyed off `request.state.auth_key`. |
| `generate_key` | `hashing.py` | Generates a new `<envelope><prefix>_<secret>` API key; returns `(plaintext, prefix, key_hash)`. |
| `verify_key` | `hashing.py` | Constant-time verify of a candidate plaintext against an argon2 hash. |
| `parse_prefix` | `hashing.py` | Extracts the prefix segment from a candidate key for a given envelope, or `None`. |
| `PREFIX_LENGTH` | `hashing.py` | Length in chars of the public prefix segment. |
| `ApiKeyRow` | `models.py` | Protocol for the subset of a stored key row the auth path reads. |
| `ApiKeyStore` | `models.py` | Protocol a service's key store must satisfy (`get_api_key_hash`, `get_api_key_by_prefix`, `touch_api_key_last_used`). A DB-backed `StorageAdapter` satisfies this structurally; so does `InMemoryKeyStore` below. |
| `AuthedKey` | `models.py` | Resolved caller identity: `name` (the audit actor), `scopes`, `is_bootstrap`. |
| `ADMIN_SCOPE` | `models.py` | The wildcard scope (`"admin"`) that satisfies any `require_scope` check. |
| `DEV_SPOOF_SCOPE` | `models.py` | The `"dev:spoof"` scope, rejected outside dev/local by `_enforce_dev_scope_environment`. |
| `ConsumerKeyRow` | `memory_store.py` | Frozen dataclass row shape backing `InMemoryKeyStore` — `id`, `name`, `scopes`, `active`, `revoked_at`. |
| `InMemoryKeyStore` | `memory_store.py` | DB-free `ApiKeyStore` implementation. Used by services with no `api_keys` table (`llm`, `meeting`, `connectors`) — keys are seeded from config at boot rather than persisted. |
| `key_store_from_config` | `memory_store.py` | Parses a `CONSUMER_KEYS` JSON array (`[{"name","prefix","key_hash","scopes"}]`) into a populated `InMemoryKeyStore`. Raises `RuntimeError` on a malformed value so a bad env var fails a service's boot, not its first request. |

## Credential config convention

**Every credential field in a service's `Settings` is `pydantic.SecretStr`, never `str`.** This is not a style preference — a plain `str` credential prints in full from anything that stringifies the settings object: a failing test's assertion diff, a debug `print`, an exception repr, a log line. That is not hypothetical; a routine assertion in `services/connectors` once dumped a real Google service-account private key into a terminal and a session transcript, and the key had to be rotated. `SecretStr` renders as `**********` everywhere, so no future test or traceback can print it regardless of who writes it.

```python
from pydantic import SecretStr

class Settings(BaseSettings):
    api_key: SecretStr = SecretStr(DEFAULT_DEV_API_KEY)
```

Keep the `SecretStr` boundary at the *edge*. `platform_auth` takes plain `str` on purpose — unwrap at the call site so nothing behind the boundary has to care:

```python
deps = build_auth(..., get_env_key=lambda: get_settings().api_key.get_secret_value())
store = key_store_from_config(get_settings().consumer_keys.get_secret_value())
```

Forgetting to unwrap usually fails *loudly* — `AttributeError: 'SecretStr' object has no attribute 'encode'` or `'strip'`. There is one exception, and it is the dangerous one:

- **Equality never matches.** `SecretStr("x") == "x"` is `False`. So `settings.api_key == DEFAULT_DEV_API_KEY` in `verify_production_secrets` silently stops firing, and the service happily boots to production on the committed dev secret. Nothing raises, and a test that only asserts the *happy* path will not notice.

A related claim is worth writing down because it is easy to assume and is **false**: `SecretStr` does not define `__bool__`, but it *does* define `__len__`, which Python falls back on. So `bool(SecretStr(""))` is `False` and `if not settings.resend_api_key:` keeps working. Unwrap those anyway — `__len__` is an implementation detail, not a documented guarantee — but do not "fix" them believing they were broken.

Each service's `tests/test_config.py` therefore carries a regression test asserting the value stays out of `repr()`/`str()` *and* that the production guards still raise — the first pins the type, the second pins the unwrap that the equality trap would otherwise eat.

At the two call sites inside this package, `secret_guard.reject_secret_wrapper` turns a missed unwrap into a `TypeError` naming the parameter and the fix, instead of an `AttributeError` that reads like a bug in this library rather than in the service's wiring. It is duck-typed on `get_secret_value` so this package keeps its fastapi/starlette/argon2-only dependency set.

New service? Write the credentials as `SecretStr` from the start, and give the test suite an autouse fixture neutralizing `env_file` (`services/connectors/tests/conftest.py` is the model) so no test ever reads a developer's real `.env`.

## Two key-store shapes

- **DB-backed** (`team-tracking`, `documentation-system`, `verification`): the service's own `StorageAdapter` already satisfies `ApiKeyStore` structurally — no adapter class from this package needed.
- **DB-free** (`llm`, `meeting`, `connectors`): `key_store_from_config(settings.consumer_keys)` builds an `InMemoryKeyStore` at boot from the `CONSUMER_KEYS` env var. There is no revoke call — revocation is dropping the entry from `CONSUMER_KEYS` and redeploying.

## Testing

```bash
cd packages/auth
uv run pytest
```
