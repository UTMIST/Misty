# Gateway architecture

External consumers call the gateway with per-consumer keys. The gateway calls
private services over authenticated HTTP and returns explicitly selected fields.
Each private service remains responsible for its own database and business rules.
See the platform's [access architecture](../../../docs/ARCHITECTURE.md#access-architecture-two-doors-not-one-gateway).

The gateway's Postgres database contains only its external key registry. Its
outbound `DIRECTORY_API_KEY` is issued by team-tracking with `identifiers:read`.
Inbound keys use `gw_`; the internal key uses `tt_`. The shared auth library
verifies the envelope, hash, revocation state, and scope. No environment bootstrap
key is accepted, and the CLI allows only explicitly supported external scopes.

The request path is audit middleware → IP limit → authentication → consumer quota
→ endpoint scope → upstream client → curated response. `deps.py` supplies the
pooled HTTP client through the `DirectoryClient` Protocol. JSON and response-shape
failures become `DirectoryUnavailable`, mapped to a static 503 without upstream
content. Authentication failures never construct the directory dependency.

The rate-limit counter synchronizes concurrent worker threads, has bounded
capacity, and lives in one process. Run one replica and one worker; a shared
counter is required before horizontal scaling. At capacity it evicts the oldest
window, which can reset that identity's quota under pressure. These limits are
application safeguards, not a replacement for edge traffic protection.

## Future compute-platform access

The compute platform can become another external consumer with its own key.
Add specific operations as its requirements become concrete: define returned
fields, endpoint scopes, and any per-user or per-team authorization first. Then
call the owning service's API. No database connection string or internal key is
handed to the compute platform.

`resolve:discord` is the only implemented data permission. Issuing a key with that
scope does not grant general directory or document access. Application identity
also does not establish an end user's identity: a future user-specific endpoint
must implement an explicit delegation contract and preserve the owning service's
access checks. Add new endpoints via the [contribution guide](CONTRIBUTING.md).
