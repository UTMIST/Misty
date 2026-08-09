# Contributing

Task walkthroughs for working on `verification`. Assumes you've read the [README](../README.md) and skimmed [ARCHITECTURE.md](ARCHITECTURE.md) — that doc explains the *why*; this one is the *how*.

> **Read this first.** This is the platform's most security-sensitive service. The confirm-code path was hardened deliberately, and several branches that look redundant are load-bearing. Before changing `src/api/routers/verification.py`, read [ARCHITECTURE.md → The confirm state machine](ARCHITECTURE.md#the-confirm-state-machine) and understand why each branch exists. If you can't explain what a branch prevents, don't remove it.

## Conventions you need to know first

- **The Protocol is the hinge.** `contracts/storage.py` defines `VerificationStore`. Every method must exist in **both** adapters (`in_memory.py`, `postgres.py`) with identical behavior — that's what the shared tests check.
- **Policy is centralized.** `src/policy.py` holds `CODE_LENGTH`, `CODE_TTL`, `RATE_LIMIT_WINDOW`, `MAX_ATTEMPTS`. Never inline one of these numbers at a call site.
- **The plaintext code never persists.** Only `hash_code(...)` output goes to storage; comparison is `verify_code(...)`, which is constant-time. Never add a log line, error detail, or audit field carrying the code.
- **Atomicity is a security control, not a performance concern.** `increment_attempts` must stay a single atomic read-and-increment. A read-then-write loses updates under concurrency and defeats the attempt cap entirely.
- **DTOs forbid extra fields.** Both request models set `extra="forbid"`.
- **Credentials are `SecretStr`.** `API_KEY`, `CODE_HMAC_SECRET`, `RESEND_API_KEY`, `GMAIL_CREDENTIALS_JSON` — unwrap with `.get_secret_value()` only at the boundary. See [`packages/auth/README.md`](../../../packages/auth/README.md#credential-config-convention) for how forgetting fails silently.

## Local setup

```bash
cd services/verification
cp .env.example .env
docker compose up -d postgres
uv sync --extra dev
uv run alembic upgrade head
uv run pytest --ignore=tests/test_postgres_adapter.py   # fast run
```

> ⚠️ **Port clash.** verification's dev Postgres binds host **5434**, the same port `documentation-system` uses. They cannot both run as configured. Remap one (`docker compose run -p 5435:5432 postgres`) and update that service's `DATABASE_URL`.

`EMAIL_BACKEND=fake` is the default, so no mail credentials are needed locally — the fake captures messages in memory, and you read the code out of the service logs.

## Walkthrough: change a policy constant

Treat this as a security change, not a tuning change.

1. Edit the constant in `src/policy.py` — never at a call site.
2. **Reason about the interaction.** The four constants only make sense together: a 6-digit code (10⁶ space) is safe *because* of the 5-attempt cap. Raising `MAX_ATTEMPTS` without lengthening `CODE_LENGTH` weakens the whole thing. Raising `CODE_TTL` widens the replay window.
3. Update the tests that assert the boundary — several tests loop to `MAX_ATTEMPTS` and assert the transition to 429.
4. Update the numbers in the README, [API.md](API.md), and [ARCHITECTURE.md](ARCHITECTURE.md). They're quoted in all three.
5. If you changed `CODE_TTL`, update the email body text in the router — it says "expires in 10 minutes" as a literal string.

## Walkthrough: add an email backend

0. **Widen the `EMAIL_BACKEND` literal first.** `src/config.py` types it as `Literal["fake", "resend", "gmail"]`. Skip this and the service dies at import the moment anyone sets your new value — `create_app()` calls `get_settings()`, and pydantic rejects it before your code ever runs.

1. **Implement the Protocol** at `src/email/<name>.py`. `EmailSender` has one method:

   ```python
   class PostmarkSender:
       def __init__(self, *, api_key: str, from_addr: str) -> None:
           ...

       def send(self, *, to: str, subject: str, body: str) -> None:
           try:
               ...
           except SomeVendorError as e:
               raise EmailSendError(str(e)) from e
   ```

   **Normalize every failure to `EmailSendError`.** The router catches only that; anything else becomes a 500 instead of a clean 502. Import a heavy vendor SDK lazily (as `gmail.py` does) so it never loads unless selected.

2. **Wire it** in `src/api/deps.py`'s backend selection, keyed by `EMAIL_BACKEND`.

3. **Add config** to `src/config.py` (`SecretStr` for credentials) and `.env.example`.

4. **Add it to the boot check.** `verify_production_secrets()` must refuse to start outside `local` if your backend is selected but its credentials are missing. This is the point of the guard — a mail backend that can't send makes the service 202 every request while delivering nothing.

5. **Tests** at `tests/test_email_<name>.py`. Copy `test_email_resend.py`: assert a successful send calls the vendor correctly, and assert each vendor failure surfaces as `EmailSendError`. Never make a real network call.

6. Document it in the README's "Email providers" section.

## Walkthrough: add a storage method

1. **Declare it on the Protocol** in `contracts/storage.py`, with a docstring stating semantics, return value, and edge cases (what happens when no row exists).
2. **Implement in both adapters** with identical behavior:
   - `src/storage/in_memory.py` — manipulate the dicts; enforce the same invariants Postgres would (the one-live-row-per-subject uniqueness in particular).
   - `src/storage/postgres.py` — SQLAlchemy Core against `schema.py`.
3. **If it mutates a counter or does read-modify-write, make it one statement.** `increment_attempts` is the model: `UPDATE ... RETURNING`. This is not optional — see the atomicity note above.
4. **Test both adapters.** Behavioral assertions go in `tests/test_in_memory_adapter.py`; if the behavior depends on real Postgres semantics (a constraint, an atomic update, an FK), add a case to `tests/test_postgres_adapter.py` too.

## Walkthrough: write a migration

Migrations are **hand-written** with sequential revision ids (`001`, `002`, …), not autogenerated hashes. Current head is **001**.

1. **Change `src/storage/schema.py` first** — the SQLAlchemy Core tables are the schema source of truth.
2. **Create `migrations/versions/002_<short_name>.py`.** Copy `001_initial_schema.py` for the header shape:

   ```python
   """add attempts_last_at to verification_codes

   Revision ID: 002
   Revises: 001
   Create Date: 2026-08-07
   """
   revision = "002"
   down_revision = "001"
   branch_labels = None
   depends_on = None
   ```

   - `down_revision` must point at the current head.
   - Write a real `downgrade()` that exactly reverses `upgrade()`.
   - New column on an existing table? Make it nullable or give it a `server_default`, and mirror that in the Pydantic model and both adapters.

3. **Apply and verify the round trip:**

   ```bash
   uv run alembic upgrade head
   uv run alembic downgrade -1
   uv run alembic upgrade head
   ```

Alembic reads `DATABASE_URL` via `migrations/env.py` → `src/config`, so it targets whatever your `.env` points at.

## Testing

**Fast (in-memory, no Docker) — your default while iterating:**

```bash
uv run pytest --ignore=tests/test_postgres_adapter.py -q
```

Runs against `InMemoryVerificationStore` and `FakeSender`, injected via `app.dependency_overrides`. Covers both endpoints, the full confirm state machine, rate limiting, the HMAC helpers, all three email senders, config/boot guards, and auth.

**Full (adds Postgres) — run before you push:**

```bash
docker compose up -d postgres
uv run alembic upgrade head
uv run pytest
```

Adds `tests/test_postgres_adapter.py`, which replays the adapter's assertions — including the atomic increment — against live Postgres. Note those assertions are sequential: they pin behavior, not concurrency (see [ARCHITECTURE.md](ARCHITECTURE.md#atomic-increments)).

**When you touch the confirm path, add a test per branch.** The state machine has nine outcomes; a change that collapses two of them will pass a test suite that only covers the happy path. In particular, keep the tests that assert:
- a wrong code against a *consumed* subject returns 400 and leaks no email,
- attempts on the consumed branch still hit the 429 cap,
- the correct code replays idempotently within the TTL.

## Linting and formatting

```bash
uv run ruff check .
uv run ruff format .
uv run ruff format --check .   # CI-style
```

CI's `verification-test` job runs Postgres 16, migrations, the **full** suite, and both `ruff check` and `ruff format --check`. Both are enforced here.

## Checklist before you push

- [ ] New storage behavior is on the `VerificationStore` Protocol and in **both** adapters.
- [ ] Anything read-modify-write is a single atomic statement.
- [ ] Policy numbers changed only in `src/policy.py`, and the interaction was reasoned about.
- [ ] No code plaintext in logs, audit fields, or error details.
- [ ] New email backend normalizes every failure to `EmailSendError` **and** is covered by `verify_production_secrets()`.
- [ ] `schema.py` and the new migration agree; `upgrade`/`downgrade` both work.
- [ ] Confirm-path changes have a test per branch, including the consumed-subject cases.
- [ ] Credential settings are `SecretStr`, unwrapped only at the boundary.
- [ ] Full `uv run pytest` passes with Postgres up; `ruff check` and `ruff format --check` are clean.
