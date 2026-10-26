# Gateway integration audit

The original PR65 record below is historical. The email-login extension and its
current file rationale are recorded at the end of this document.

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
- Each app lifespan owns one lazily constructed directory pool, shared across
  concurrent requests and closed at shutdown without affecting other app instances.
- Malformed upstream JSON/shapes and transport errors produce a static 503.
- Concurrent quota updates are synchronized; deployment remains one replica.
- Railway client buckets use validated `X-Real-IP`, ignoring shared proxy hops in
  `X-Forwarded-For`; direct deployments use the socket peer.
- Readiness requires the migrated key table, matching the current platform's
  distinction between process liveness and database readiness. A per-app cache
  retains both outcomes for five seconds and allows only one database probe at a
  time; concurrent requests never block on it or receive expired success.
- Revocation preserves the first timestamp/actor, and both adapters withhold
  revoked hashes. Both adapters return creation/update audit metadata; CLI listing
  exposes that metadata and the identifier needed for revocation.
- Migration configuration escapes URL percent signs for Alembic interpolation,
  accepting encoded credentials without printing the connection URL in an error.
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
| `/AGENTS.md` | Add the gateway inventory, database head, public-access rules, proxy/readiness invariants, and local port conflict. |
| `/README.md`, `/docs/ARCHITECTURE.md`, `/docs/RAILWAY-DEPLOYMENT.md` | Integrate the public entry point, database ownership, and deployment exception into platform documentation. |
| `/uv.lock`, `pyproject.toml` | Register and install the gateway workspace member using the current shared dependency lock, including both runtime Python packages in its wheel. |
| `.dockerignore`, `Dockerfile`, `railway.json`, `docker-compose.yml`, `.env.example` | Package the service with an unprivileged runtime user, supply local defaults/Postgres, and configure migrations, readiness, and one replica. |
| `README.md` | Service overview, configuration, resolver contract, key management, and reproducible validation commands. |
| `docs/API.md`, `docs/ARCHITECTURE.md`, `docs/CONTRIBUTING.md`, `docs/DEPLOYMENT.md` | Document health/API behavior, future consumer extension, contribution checks, and concrete environment/key provisioning. |
| `docs/CHANGE-AUDIT.md` | Explain the integration decisions, verification, and necessity of each changed file. |
| `alembic.ini`, `migrations/env.py`, `migrations/script.py.mako`, `migrations/versions/001_api_keys.py` | Supply the migration runner, safely handle encoded connection URLs, and define the reversible initial key-registry schema. |
| `contracts/__init__.py`, `src/__init__.py`, `src/api/__init__.py`, `src/api/routers/__init__.py`, `src/directory/__init__.py`, `src/storage/__init__.py` | Define the service's Python packages using existing repository conventions. |
| `contracts/directory.py`, `contracts/storage.py`, `contracts/types.py` | Define injectable directory/storage interfaces and key metadata without importing implementations. |
| `src/api/app.py` | Assemble routes, middleware, health/readiness, and the static upstream-error response. |
| `src/api/auth.py`, `src/api/hashing.py`, `src/api/middleware.py` | Bind shared authentication, the `gw_` envelope, and audit behavior without implementing another auth system. |
| `src/api/deps.py` | Wire the Postgres adapter and lazy directory client at one dependency injection point; own and close each app's HTTP pool. |
| `src/api/ratelimit.py` | Bound per-IP and per-consumer request counts, including concurrent request handling. |
| `src/api/readiness.py` | Bound public readiness database work with a per-app cache and a nonblocking probe lock. |
| `src/api/routers/resolve.py` | Expose the scoped, one-field resolver with indistinguishable missing-link responses. |
| `src/cli.py`, `src/config.py` | Manage scoped external keys and configure/redact the internal credential with startup validation. |
| `src/directory/http_client.py` | Authenticate private HTTP calls, encode path inputs, validate responses, and map failures to the domain error. |
| `src/storage/schema.py`, `src/storage/in_memory.py`, `src/storage/postgres.py` | Own the key table and implement equivalent key lifecycle/readiness behavior in both adapters. |
| `tests/conftest.py` | Isolate process-wide quota state between tests. |
| `tests/test_auth.py`, `tests/test_cli.py`, `tests/test_config.py` | Verify authentication/revocation, permitted key issuance, CLI operator behavior, and startup secret handling. |
| `tests/test_directory.py`, `tests/test_resolve.py` | Cover HTTP boundaries, malformed upstream responses, curated data, scope ordering, and lookup errors. |
| `tests/test_lifespan.py` | Verify lazy creation, concurrent pool reuse, per-app ownership, and shutdown cleanup. |
| `tests/test_migrations.py` | Exercise offline upgrade/downgrade with encoded credentials and check that output omits the connection URL. |
| `tests/test_health.py`, `tests/test_ratelimit.py` | Verify bounded readiness work under repeated/concurrent requests, cache expiry/recovery, independent Railway client quotas, proxy validation, and audit coverage. |
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


## Email-login extension

Misty now provides scoped server-to-server email login for active directory
members. This intentionally touches gateway, private verification authentication,
and the platform documentation/CI that describe their boundary. Compute sessions
and deployment are separate integration steps. Existing Discord code replay is
preserved; gateway login redemption is atomic and single-use.

This change is opt-in (`EMAIL_LOGIN_ENABLED=false`). Gateway migration002 adds
login challenges and durable quota windows without modifying existing key rows.
Delivery happens after generic202; failed replacement delivery preserves the old
challenge. Confirmation checks code ownership, the original person/email, current
active state, consumer binding, expiry, and atomic redemption. Unknown/inactive
challenges take the same verification call path to avoid an outage-status oracle.

### File rationale for this extension

Paths are repository-relative; grouped paths all share the stated purpose.

| Files | Necessity |
|---|---|
| `services/gateway/contracts/login.py` | Strict request/response models, secret-wrapped submitted codes, challenge state, private verification protocol/errors. |
| `services/gateway/contracts/directory.py`, `services/gateway/contracts/storage.py` | Add scoped lookup and atomic login-state contracts before adapter implementations. |
| `services/gateway/src/login_policy.py` | Centralize send/guess/request windows and normalized email/IP bucket identities. |
| `services/gateway/src/storage/schema.py`, `services/gateway/migrations/versions/002_email_login.py` | Define and reversibly migrate gateway-owned challenge and quota tables/indexes. |
| `services/gateway/src/storage/in_memory.py`, `services/gateway/src/storage/postgres.py` | Equivalent synchronized admission, activation, cleanup and single-use redemption; readiness includes the new schema. |
| `services/gateway/src/api/routers/email_login.py` | Implement start, confirm, status, active-member checks and post-response private delivery. |
| `services/gateway/src/api/app.py`, `services/gateway/src/api/middleware.py` | Register routes, sanitize validation/upstream errors, bound body size, prevent response caching. |
| `services/gateway/src/api/deps.py` | Own and close a lazily constructed private verification HTTP pool. |
| `services/gateway/src/directory/http_client.py` | Use existing authenticated directory email/person lookup routes. |
| `services/gateway/src/verification/__init__.py`, `services/gateway/src/verification/http_client.py` | Package the private verification client and sanitize provider/transport failures. |
| `services/gateway/src/config.py`, `services/gateway/src/cli.py` | Add the opt-in feature, redacted internal credential/boot validation, and two external scopes. |
| `services/gateway/tests/test_email_login.py`, `services/gateway/tests/test_verification.py` | Prove HTTP eligibility, replay/concurrency, generic responses, scope ordering, failure masking, resend recovery and private client contract. |
| `services/gateway/tests/login_storage_cases.py`, `services/gateway/tests/test_login_storage.py`, `services/gateway/tests/test_postgres_adapter.py` | Exercise the same quotas, expiry, races, replacement and redemption semantics against both adapters, plus persistence across adapter recreation. |
| `services/gateway/tests/test_config.py` | Check the new secret is redacted and required when non-local login is enabled. |
| `services/gateway/tests/smoke_stack.py`, `.github/workflows/ci.yml` | Verify real gateway → directory/verification HTTP using disposable DBs, scoped keys, a fake sender and test-container-only deterministic codes; build verification before this check. |
| `services/verification/src/config.py`, `services/verification/src/api/deps.py`, `services/verification/src/api/app.py` | Support a separate scoped gateway identity through config-backed keys and boot validation. |
| `services/verification/src/storage/null_keys.py` (removed) | Remove the unused adapter that rejected all scoped keys. |
| `services/verification/tests/conftest.py`, `services/verification/tests/test_consumer_auth.py` | Isolate cached key configuration and prove scoped auth, denial without scope, and malformed-config boot rejection. |
| `services/gateway/.env.example`, `services/gateway/README.md`, `services/gateway/docs/API.md`, `services/gateway/docs/ARCHITECTURE.md`, `services/gateway/docs/CONTRIBUTING.md`, `services/gateway/docs/DEPLOYMENT.md` | Document new settings, API/session handoff, storage/trust decisions, tests, provisioning and rollback. |
| `services/verification/.env.example`, `services/verification/README.md`, `services/verification/docs/API.md`, `services/verification/docs/ARCHITECTURE.md`, `services/verification/docs/DEPLOYMENT.md` | Replace obsolete bootstrap-only descriptions and document scoped keys plus retention needs of per-login subjects. |
| `AGENTS.md`, `README.md`, `docs/ARCHITECTURE.md`, `docs/DEVELOPMENT.md`, `docs/RAILWAY-DEPLOYMENT.md`, `packages/auth/README.md` | Keep schema heads, service ownership, key-storage descriptions and platform enablement instructions consistent. |
| `services/gateway/docs/CHANGE-AUDIT.md` | Record the intentional cross-zone scope, file rationale, verification and integration boundary. |

### Verification boundary

Run gateway and verification full suites with separate disposable databases,
Ruff lint/format checks, migration002 downgrade/upgrade with existing key rows,
the three-service Docker HTTP smoke, and the isolated gateway wheel import check.
No live mail provider is used. These checks do not establish live Railway
configuration, real mailbox delivery, or compute session integration.

Verified locally: both full service suites with disposable Postgres, Ruff lint
and format checks, migration002 round trip preserving external keys, all three
container builds and the HTTP smoke, and isolated gateway wheel/CLI imports.
Independent review findings were fixed and retested. The test runner reports an
existing Starlette/httpx deprecation warning; no checks failed. A separate local
browser check of the companion compute integration also passed against the real
gateway, directory and verification containers with disposable databases and a
fake mail sender. It covered login, rejection, replay, preserved compute identity,
and retirement after the status cache expires. No live deployment or real email
delivery was performed as part of these checks.
