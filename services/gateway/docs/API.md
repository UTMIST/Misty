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
| `GET /health/ready` | None | Reads the gateway's migrated key table; 200 when readable, otherwise `503 {"detail":"gateway database unavailable"}`. |

Both health routes are exempt from the IP limit. Readiness checks the gateway's
own database; it does not check team-tracking. A resolver smoke request separately
verifies the internal URL, internal key, and directory data.
