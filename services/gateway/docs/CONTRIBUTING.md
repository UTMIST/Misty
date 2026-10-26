# Contributing

Read the [README](../README.md) and [architecture](ARCHITECTURE.md) first.
The gateway is a public security boundary. Keep each external operation explicit
and keep the internal services' data ownership intact.

## Add an external operation

1. Define the consumer's required fields and permission. Copy the resolver's
   explicit response model; do not return an upstream response wholesale.
2. Add upstream operations to a `contracts/` Protocol and implement them in an
   HTTP client. Wire concrete clients only in `src/api/deps.py`.
3. Require the new scope before constructing an upstream dependency. Mount the
   route under the `/v1` router so it inherits the authenticated key quota.
4. Add the scope to the CLI's allowed choices and document it in the API and
   deployment guides. Grant only the needed internal scopes to the gateway.
5. Test authentication, scope rejection before upstream work, response fields,
   upstream outages, and malformed upstream data. Keep all fixtures synthetic.

Storage methods belong in the Protocol and both adapters. Shared behavior tests
live in `tests/storage_cases.py`; Postgres tests require a dedicated disposable
database and explicitly opt in. See [testing](../README.md#testing).

The Docker HTTP smoke check runs the current team-tracking and gateway images
with separate disposable databases. It never imports another service's code or
uses live credentials. CI runs it after building both images.

## Before pushing

- Run `uv run ruff check .` and `uv run ruff format --check .`.
- Run the fast tests and the Postgres suite when storage changes.
- Verify migration upgrade → downgrade → upgrade on a disposable database.
- Run the Docker HTTP smoke check for changes to outbound calls or deployment.
- Keep [API](API.md), [deployment](DEPLOYMENT.md), and root deployment docs true.
- Describe why each changed file is needed in the PR description.
