# Verified GitHub accounts

## Ownership and cross-service contract

Connectors owns authorization state, provider calls, encrypted tokens, refresh
and revocation. Team-tracking stores verified numeric account ID/current login
in `verified_github_identities`, separate from legacy unverified `github`
identifiers. The bot owns member UX through its existing authenticated commands.
No credential is stored in team-tracking, returned to the browser, or exposed as
an LLM tool. There is no personal repository retrieval in this foundation.

| Service / method | Endpoint | Scope |
|---|---|---|
| Connectors POST | `/github/oauth/start` | `github-links:write` |
| Connectors POST | `/github/oauth/confirm` | `github-links:write` |
| Connectors POST | `/github/oauth/status` | `github-links:read` |
| Connectors POST | `/github/oauth/unlink` | `github-links:write` |
| Connectors GET | `/github/oauth/callback?state=...&code=...` | Public, single-use state |
| Connectors POST | `/github/oauth/revoked` | Public, signed GitHub webhook |
| Directory GET | `/people/{uuid}/github` | `github-identities:read` |
| Directory PUT / DELETE | same | `github-identities:verify` (connector-only key) |

Connector POST bodies are strict `{person_id,discord_id}`; trusted bot/session
context supplies both. Callback state is unpredictable, hashed as a lookup key,
single-use, expires after five minutes, and stores the exact requester binding
and PKCE verifier encrypted. Starting again replaces previous pending state.
There is a global bound of 1,000 pending links. Callback failures consume state;
start again instead of replaying it. OAuth uses fixed callback and S256 PKCE.
UTMIST installs the GitHub App; members authorize it. No additional 2FA flow.

Directory PUT is strict `{discord_id,github_id,login}`. The directory verifies
that the active person still owns the current Discord identifier. Duplicate
numeric accounts across people conflict, even after a login rename; an existing
verified ID can only change after explicit unlink. Reconnect may update a login
for the same ID. Unlink clears the current verified identity but preserves its audited directory
row, the person, and any legacy identifier. Provider tokens never pass through this endpoint.

After OAuth, the callback displays a private completion code. The member runs
`/github action:confirm code:<code>` from the original authenticated Discord/web
session before the identity becomes verified or credentials become usable. This
proves possession of both the browser result and original Misty session; merely
forwarding a connect URL cannot link someone else's GitHub account to the
requester. The code is hashed inside the encrypted vault, expires after five
minutes, permits at most five attempts, and is never echoed in bot receipts.
It is a flow-completion receipt, not an additional GitHub password/2FA challenge.
The confirm body adds `confirmation_code` to the subject fields. Incomplete
confirmations do not authorize synchronization and expire without holding another
member's account indefinitely.
The verification scope must not be granted to the ordinary bot/model consumer.

## Deployment and credential store decision

Apply directory migration 012 before enabling connectors, then configure the
bot. These independent issue branches both initially allocate revision 012:
whichever migration merges second must be renumbered and point to the new head.
Never deploy two different revisions with the same ID.

The chosen store is SQLite owned exclusively by connectors on its own volume,
with Fernet authenticated encryption of each JSON record (tokens and PKCE state).
The encryption key lives in secret configuration, not the database. Person IDs
and hashed state lookup keys are metadata. Use one worker/replica with finite
HTTP timeouts; SQLite immediate transactions serialize callbacks, unlink and
refresh. This deliberately changes connectors from stateless-only to optionally
stateful; no other service may open this vault. The vault creates its table on
first enablement; it does not share the directory's database or migrations.
Back up the vault and encryption key separately and restrict volume access.

Create a GitHub App under UTMIST, restrict installation availability to its owning
account, and install it on explicitly approved organization repositories. Enable
expiring user tokens. Configure the exact callback URL and a webhook secret for
`github_app_authorization` revocation. Configure a narrowly exposed HTTPS ingress
for callback and revocation paths only; keep all internal POST operations private.
This change does not itself publish an ingress or change a live App registration.
Disable query-string access logging at that ingress and run Uvicorn without
access logs on this listener: authorization codes/state must not enter request
logs. Audit middleware records paths, never queries, bodies or provider content.
Provider failures use static messages. Disable HTTP debug/trace logging.

The connector's directory key needs `identifiers:read`, `github-identities:verify`;
read consumers can use `github-identities:read` separately. The bot's connector
key needs only `github-links:read` and `github-links:write`. The App uses its
registered permissions, not broad OAuth `repo` scopes. Browser linking and `/user`
verify identity; they do not grant repository permissions. Future private reads
need approved App repository permissions and their own authorization checks.

## Status, failures and recovery

`status` checks the active person/Discord binding, refreshes near-expiry tokens
under the vault lock, fetches GitHub `/user`, and updates a renamed login. It
returns only status, ID and login. Expired/failed or ambiguous refresh invalidates
local credentials and asks for reauthorization; rotated refresh tokens are never
retried blindly. Revocation verifies HMAC-SHA256 on the bounded raw webhook body,
clears credentials and invalidates directory identity. Repeated events are safe.
Unlink invalidates pending callbacks by generation before attempting remote token
revocation. Missing/unverified links must block future membership sync; do not
infer identity from username/email or fall back to another person's credentials.

Directory and vault writes are separate transactions. If a crash or directory
outage leaves stale directory identity, consumers must require a fresh successful
connector status and matching directory identity before acting; a directory row
alone is not proof of a live authorization. Terminal vault states retain a durable directory-cleanup marker until the
idempotent directory DELETE succeeds. Status and reconnect retry this cleanup
under the vault lock before returning a terminal status or starting a new link.
A cleanup outage returns an error, preserving the marker across restarts. Never issue a second OAuth exchange for a consumed callback. A remote
revocation failure after unlink does not restore local credentials; revoke the
App authorization through GitHub settings if needed. Removed credentials are
not retained in plaintext as a retry queue.

For normal key rotation, stop linking, back up, decrypt/re-encrypt every vault
record with old/new keys using a controlled offline operation, then switch the
configured key and restart. Do not change the key against an existing vault
without re-encryption. If the vault/key is lost, stop syncing, revoke affected
App authorizations, clear stale verified directory links with the connector
scope, initialize a new vault/key, and have members authorize again. Tokens are
never recoverable from team-tracking. Disable the feature and revoke dedicated
consumer keys for rollback; retain the protected volume for recovery.

Test only with fake GitHub/directory responses. In staging, manually verify
connect, same-account reconnect, login rename, duplicate-account denial, unlink,
revocation and disabled handling before enabling any membership automation.
