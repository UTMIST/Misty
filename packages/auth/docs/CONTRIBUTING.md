# Contributing

Task walkthroughs for working on `platform_auth`. Assumes you've read the [README](../README.md), which documents the full export surface and the credential convention.

> **Read this first: blast radius.** This is a shared leaf that **all six services** import. A change here can break every service at once, and CI is the only thing that will tell you. There is no gradual rollout — the workspace resolves one version of this package for everyone. Treat every change as a platform-wide change.

## Conventions you need to know first

- **Pure leaf, and it must stay pure.** `platform_auth` depends only on `fastapi`, `starlette`, `argon2`, and stdlib. It **never** imports service code — not `src/`, not `contracts/`, not another package. If you need something from a service, invert it: take it as a parameter to `build_auth(...)` or as a Protocol the service satisfies.
- **Protocols, not base classes.** `ApiKeyStore` and `ApiKeyRow` are `Protocol`s satisfied *structurally*. A service's DB-backed `StorageAdapter` satisfies `ApiKeyStore` without inheriting anything, and that's the point — this package must never require a service to subclass it.
- **This package takes plain `str`.** Services hold credentials as `SecretStr` and unwrap at the call site. Don't add `SecretStr` handling here — it would drag `pydantic` into a package that deliberately doesn't depend on it. `secret_guard.reject_secret_wrapper` is duck-typed on `get_secret_value` for exactly this reason.
- **Fail closed.** An unknown scope, a missing row, a revoked key, an inactive key — all deny. When adding a branch, the default must be denial.
- **Constant-time comparison for anything secret.** `verify_key` and the bootstrap-key comparison use constant-time primitives. Never introduce a `==` on key material.

## Local setup

```bash
cd packages/auth
uv sync --extra dev
uv run pytest
```

No Docker, no database, no network.

## Walkthrough: add an export

1. **Implement it** in the appropriate module — `hashing.py` (key material), `models.py` (Protocols and value types), `factory.py` (FastAPI dependency construction), `memory_store.py` (the DB-free store), `audit.py` (middleware), `secret_guard.py` (unwrap guards).
2. **Export it** from `platform_auth/__init__.py` — both the import and the `__all__` entry. Anything not in `__all__` is not part of the contract.
3. **Add a row to the README's export table.** That table is the published API surface; an undocumented export will get reimplemented in a service shim by the next person.
4. **Test it** in `tests/test_<module>.py`.
5. **Consider whether services should now delete code.** The rule stated in the root README is that a per-service file earns its place only by *binding* something — if this package now exposes something ready-to-use, the per-service copy should go. `AuditLogMiddleware` is the model: it's imported directly from `platform_auth`, not re-wrapped per service.

## Walkthrough: change `build_auth` or the `AuthDeps` bundle

This is the highest-risk change in the repo. `build_auth` is called by every service's `src/api/auth.py`.

1. **Add parameters, don't reorder or remove them.** Give new parameters defaults that preserve today's behavior, so a service that hasn't been updated keeps working.
2. **If a behavior change is unavoidable**, update all six service shims in the same PR:
   ```
   services/team-tracking/src/api/auth.py
   services/documentation-system/src/api/auth.py
   services/verification/src/api/auth.py
   services/llm/src/api/auth.py
   services/meeting/src/api/auth.py
   services/connectors/src/api/auth.py
   ```
   Note this makes the PR span every zone — that's the rare case where `pr-zone-check`'s multi-zone warning is expected. Say so in the PR description.
3. **Check the odd ones out.** Two services deviate and are easy to break:
   - **`verification`** uses `NullApiKeyStore` — no per-consumer keys exist, only the bootstrap env key. Any change assuming a real store must tolerate one that always returns `None`.
   - **`meeting`** authenticates its WebSocket in `routers/meetings.py` with `_authenticate_ws`, which **mirrors** `require_api_key`'s logic standalone rather than reusing the `Depends()` chain (a WS handshake can't). A change to the HTTP auth path that isn't mirrored there silently diverges the two.
4. **Run every affected service's suite locally**, not just this package's.

## Walkthrough: change key format or hashing

Key material changes are **not backward compatible** with keys already issued. Before touching `hashing.py`:

1. Work out what happens to existing keys. Keys live in three places: `api_keys` tables (team-tracking, documentation-system), `CONSUMER_KEYS` env vars (llm, meeting, connectors), and a bare env var (verification). All of them would need reissuing.
2. Preserve `parse_prefix`'s envelope contract. Each service binds its own envelope (`tt_`, `doc_`, `llm_`, `meeting_`, `connectors_`, `vf_` reserved), and the envelope check is what stops a key minted for one service from being accepted by another. That cross-service rejection is a security property, not an inconvenience — the `doc_`-key-into-team-tracking mistake documented in [`docs/DEVELOPMENT.md`](../../../docs/DEVELOPMENT.md#troubleshooting) is it working correctly.
3. If you must proceed, write the migration path into [`docs/RAILWAY-DEPLOYMENT.md`](../../../docs/RAILWAY-DEPLOYMENT.md) as part of the same PR.

Realistically: **don't**. Adding a scope, a store implementation, or a dependency is cheap; changing key format is a coordinated re-issue across six services and two environments.

## Walkthrough: add a scope concept

Scopes are strings; this package only defines the two special ones (`ADMIN_SCOPE`, `DEV_SPOOF_SCOPE`). Ordinary scopes (`people:read`, `chat`, `fetch`) live in the services.

Add a scope *here* only if it needs special handling — a wildcard, or an environment-gated one like `dev:spoof`, which `_enforce_dev_scope_environment` rejects outside dev/local. Then test both the allowed and the rejected environment.

## Testing

```bash
uv run pytest
uv run ruff check .
```

CI runs `auth-lib-test`: `uv sync --extra dev`, `uv run pytest`, `ruff check`, and `ruff format --check`.

**Local green is not enough for this package.** Because six services import it, run their suites too before pushing anything non-trivial:

```bash
# from the REPO ROOT, not packages/auth
for s in team-tracking documentation-system verification llm meeting connectors; do
  (cd "services/$s" && uv run pytest --ignore=tests/test_postgres_adapter.py -q) || echo "FAILED: $s"
done
```

The `--ignore` keeps it fast; the DB-free services simply have no such file.

## Checklist before you push

- [ ] No new dependency beyond fastapi / starlette / argon2 / stdlib.
- [ ] No import of any service's code.
- [ ] New exports are in `__all__` **and** the README's export table.
- [ ] `build_auth` signature changes are additive with behavior-preserving defaults, or all six shims are updated in the same PR.
- [ ] `verification`'s null store and `meeting`'s standalone WS auth both still work.
- [ ] New branches fail closed; no `==` on key material.
- [ ] All six service test suites pass, not just this package's.
- [ ] `uv run pytest`, `uv run ruff check .`, and `uv run ruff format --check .` are clean.
