# Gateway integration audit

[PR #65](https://github.com/UTMIST/Misty/pull/65) adds the external HTTP entry point
and its first curated read operation. The PR records the tested staging revision
and verification results.
The ownership merge preserves staging's service owners and adds the gateway zone.
No private service application code is changed relative to that baseline.

The gateway accepts a separate consumer key, applies rate limits and scope checks,
then resolves GitHub → person → Discord through team-tracking's existing HTTP API.
It owns only the external key registry. The resolver is an example for future
operations, with no current consumer integration. General compute-platform data
access will require additional explicit operations; see [architecture](ARCHITECTURE.md).

## Integration fixes

- Scope rejection precedes construction of the upstream client.
- Malformed upstream JSON/shapes and transport errors produce a static 503.
- Concurrent quota updates are synchronized; deployment remains one replica.
- Readiness requires the migrated key table, matching the current platform's
  distinction between process liveness and database readiness.
- Revocation preserves the first timestamp/actor, and both adapters withhold
  revoked hashes. CLI listing exposes the identifier needed for revocation.
- Key issuance requires a supported scope; blank/default internal secrets fail
  startup outside local mode.
- Local provisioning commands pin the service directory and use its Python module
  to avoid the shared workspace's `src.cli` console-script collision.
- Fast checks include the gateway; CI also exercises its Postgres adapter,
  migration round trip, and the real directory/gateway HTTP integration.
- The wheel includes its runtime `contracts` package. CI installs the built wheel
  in a fresh environment and imports the API and CLI outside the source checkout.
- The container runs as an unprivileged user, including migrations and the key CLI.

## File rationale

Paths below are relative to `services/gateway` unless prefixed with `/`, which
means the repository root. This covers the complete PR delta from staging.

| Files | Why they are needed |
|---|---|
| `/.github/CODEOWNERS`, `/.github/PULL_REQUEST_TEMPLATE.md`, `/.github/labeler.yml`, `/.github/workflows/pr-zone-check.yml`, `/docs/CODE-OWNERSHIP.md` | Register the gateway ownership zone in the existing five-file convention. |
| `/.github/workflows/ci.yml` | Gate gateway lint/tests, isolated wheel imports, disposable Postgres migrations, container build, and cross-service HTTP compatibility; restrict the gateway job's checkout credentials. |
| `/Makefile` | Include gateway in repository fast tests and lint/format checks. |
| `/AGENTS.md` | Add the gateway inventory, database head, public-access rules, and local port conflict. |
| `/README.md`, `/docs/ARCHITECTURE.md`, `/docs/RAILWAY-DEPLOYMENT.md` | Integrate the public entry point, database ownership, and deployment exception into platform documentation. |
| `/uv.lock`, `pyproject.toml` | Register and install the gateway workspace member using the current shared dependency lock, including both runtime Python packages in its wheel. |
| `.dockerignore`, `Dockerfile`, `railway.json`, `docker-compose.yml`, `.env.example` | Package the service with an unprivileged runtime user, supply local defaults/Postgres, and configure migrations, readiness, and one replica. |
| `README.md` | Service overview, configuration, resolver contract, key management, and reproducible validation commands. |
| `docs/API.md`, `docs/ARCHITECTURE.md`, `docs/CONTRIBUTING.md`, `docs/DEPLOYMENT.md` | Document health/API behavior, future consumer extension, contribution checks, and concrete environment/key provisioning. |
| `docs/CHANGE-AUDIT.md` | Explain the integration decisions, verification, and necessity of each changed file. |
| `alembic.ini`, `migrations/env.py`, `migrations/script.py.mako`, `migrations/versions/001_api_keys.py` | Supply the migration runner and reversible initial key-registry schema. |
| `contracts/__init__.py`, `src/__init__.py`, `src/api/__init__.py`, `src/api/routers/__init__.py`, `src/directory/__init__.py`, `src/storage/__init__.py` | Define the service's Python packages using existing repository conventions. |
| `contracts/directory.py`, `contracts/storage.py`, `contracts/types.py` | Define injectable directory/storage interfaces and key metadata without importing implementations. |
| `src/api/app.py` | Assemble routes, middleware, health/readiness, and the static upstream-error response. |
| `src/api/auth.py`, `src/api/hashing.py`, `src/api/middleware.py` | Bind shared authentication, the `gw_` envelope, and audit behavior without implementing another auth system. |
| `src/api/deps.py` | Wire the Postgres adapter and pooled directory client at one dependency injection point. |
| `src/api/ratelimit.py` | Bound per-IP and per-consumer request counts, including concurrent request handling. |
| `src/api/routers/resolve.py` | Expose the scoped, one-field resolver with indistinguishable missing-link responses. |
| `src/cli.py`, `src/config.py` | Manage scoped external keys and configure/redact the internal credential with startup validation. |
| `src/directory/http_client.py` | Authenticate private HTTP calls, encode path inputs, validate responses, and map failures to the domain error. |
| `src/storage/schema.py`, `src/storage/in_memory.py`, `src/storage/postgres.py` | Own the key table and implement equivalent key lifecycle/readiness behavior in both adapters. |
| `tests/conftest.py` | Isolate process-wide quota state between tests. |
| `tests/test_auth.py`, `tests/test_cli.py`, `tests/test_config.py` | Verify authentication/revocation, permitted key issuance, CLI operator behavior, and startup secret handling. |
| `tests/test_directory.py`, `tests/test_resolve.py` | Cover HTTP boundaries, malformed upstream responses, curated data, scope ordering, and lookup errors. |
| `tests/test_health.py`, `tests/test_ratelimit.py` | Verify readiness versus liveness, quota behavior, proxy identity handling, and audit coverage. |
| `tests/storage_cases.py`, `tests/test_storage.py`, `tests/test_postgres_adapter.py` | Run shared storage semantics against both adapters and verify persisted CLI keys through HTTP authentication. |
| `tests/smoke_stack.py` | Exercise the current built directory and gateway over real HTTP with isolated databases and disposable keys, verifying the gateway runs and migrates as non-root. |

## Verification and deployment status

Repository-wide `make check` passes, including all Python fast suites, bot tests,
format/lint gates, and label consistency. The gateway Postgres suite and migration upgrade →
downgrade → upgrade pass. Both service images build with the current lockfile.
The HTTP smoke check verifies readiness before/after migration, the real directory
lookup, credential separation, identical misses, directory outage, and revocation.
It also verifies the API, migrations, and key CLI run as an unprivileged user.
The built wheel's API and CLI import successfully in a fresh environment outside
the source checkout.

Railway inspection found no existing gateway service in the Misty project. The
[deployment runbook](DEPLOYMENT.md) specifies its separate database, scoped
internal key, public HTTPS domain, and consumer-key provisioning. This audit
records local verification; it does not assert a live deployment.
