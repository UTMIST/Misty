# Gateway API

The public data API is versioned under `/v1`. `/docs` serves Swagger and
`/openapi.json` is the generated contract.

## Discord resolver

`GET /v1/resolve/discord/{github_login}` requires an issued `X-API-Key` with
`resolve:discord`. The [README's resolver reference](../README.md#the-resolver-endpoint)
documents its one-field response, errors, rate limits, and case sensitivity.
The scope permits lookup of any linked GitHub login; it does not restrict a
consumer to a particular person or team.

There are no public person, membership, document, database-query, or write routes.
External identity headers do not grant delegated user access to private services.

## Health

| Route | Authentication | Behavior |
|---|---|---|
| `GET /health` | None | Process liveness; `200 {"status":"ok"}` even if a dependency is unavailable. |
| `GET /health/ready` | None | Checks the migrated key table; caches either result for five seconds. Returns 200 when ready, otherwise `503 {"detail":"gateway database unavailable"}`. |

Both health routes are exempt from the IP limit. Readiness permits only one
database probe at a time per app, with at least five seconds between a completed
probe and the next one. Concurrent callers receive a valid cached result or 503
while an expired result is being refreshed; they never wait for the probe.
After a migration, outage, or recovery, allow the cache to expire before expecting
the response to change. `/health` does no database work.

Readiness checks only the gateway's own database, not team-tracking. A resolver
smoke request separately verifies the internal URL, internal key, and directory data.
