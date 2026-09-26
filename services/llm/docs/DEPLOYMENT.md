# llm — deployment

Service-specific deploy notes. The full platform runbook is [`docs/RAILWAY-DEPLOYMENT.md`](../../../docs/RAILWAY-DEPLOYMENT.md). This page covers only what's different about llm.

## Shape

| | |
|---|---|
| **Database** | None. No Alembic, no `preDeployCommand`. |
| **Builder** | Dockerfile, build context = **repo root** (needs the uv workspace + `packages/auth`) |
| **Install** | `uv sync --frozen --no-dev --package llm` |
| **Healthcheck** | `/health` |
| **Port** | Binds Railway's injected `${PORT}`; 8002 locally |
| **Exposure** | Private. Reached as `llm.railway.internal:${PORT}`. |
| **State** | Stateless. Safe to scale horizontally and restart at any time. |

A redeploy is a plain restart — nothing to migrate, no state to drain. In-flight requests are the only thing lost, and consumers retry.

## Variables

| Var | Staging / production | Notes |
|---|---|---|
| `LLM_ENV` | `staging` / `production` | Anything but `local` turns on the boot check |
| `API_KEY` | a real random string | **Required** by the boot check |
| `CONSUMER_KEYS` | JSON array | Malformed → boot failure |
| `AWS_REGION` | e.g. `us-east-1` | **Required** by the boot check |
| `LLM_PROVIDER` | `bedrock-converse` | Chat backend; leave at default unless model access changed |
| `LLM_MODEL` | `claude-sonnet-4-6` | Default when a `/chat` request omits `model` |
| `REQUEST_TIMEOUT_S` | `60` | SDK timeout setting; see [embedding semantics](API.md#batching-and-timeouts) |
| `THINKING_DEFAULT` | `true` | Applied when a `/chat` request omits `thinking` |

AWS credentials for chat come from the standard chain — `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`, or `AWS_BEARER_TOKEN_BEDROCK`.

**Outside `local`, the boot check requires `API_KEY` (overridden from the dev default), `AWS_REGION`, and `OPENAI_API_KEY`.** It checks configuration, not whether credentials work or the account has model access; those failures surface on a real provider call.

Chat usage bills as **standard Amazon Bedrock** (AWS credits apply), deliberately not Claude Platform on AWS / Marketplace.

## Embeddings (`POST /embed`)

Embeddings bill through **OpenAI, not AWS credits**. Callers need the independent
[`embed` scope](#consumer-keys).

| Variable | Default | Notes |
|---|---|---|
| `EMBED_MODEL` | `openai-embed-3-small` | **1536 dimensions**; see [model choice and compatibility](ARCHITECTURE.md#initial-embedding-model) |
| `OPENAI_API_KEY` | `""` | **Required outside `local`**, even when current consumers only use `/chat` |
| `EMBED_MAX_REQUEST_CHARS` | `400000` | Aggregate character cap; see [API limits](API.md#post-embed) |

**Before merging to an auto-deploy branch or deploying llm, ensure a real `OPENAI_API_KEY`
is configured in the target environment.** A missing key stops non-local startup. Locally,
the service still boots without it: `/embed` returns 503, `/health` stays usable, and
`/chat` continues to use its own Bedrock configuration.

## Consumer keys

llm has no `api_keys` table. Keys live in `CONSUMER_KEYS`, and the CLI only *prints*.
Run from the repo root; provision a new embedding caller only when approved:

```bash
uv --project services/llm run llm-keys --name meeting --scopes chat
# Example embedding-only caller, not an indexing-pipeline deployment:
uv --project services/llm run llm-keys --name embedding-caller --scopes embed
```

`chat` and `embed` are independent; use `--scopes chat embed` only when a caller needs both.

- **stdout** — the plaintext key, shown **once**. Give it to the consumer for `X-API-Key` (`meeting` stores it as `LLM_API_KEY`).
- **stderr** — the JSON object. Append it to llm's `CONSUMER_KEYS` array.

Then redeploy llm. **Revoking is the reverse**: drop the entry and redeploy. There is no revoke command, because there is no database.

> Always pass `--project`. Every service declares a top-level `src` package, so in the shared workspace venv a bare invocation can resolve the wrong service's CLI. (Entry points differ — `src.mint_key:main` here, `src.cli:main` in team-tracking and documentation-system — but the `src` collision is what bites.) Verify the token starts with `llm_`.

## Deploy order

**llm must be deployed before `meeting`.** `meeting` calls `/chat` for minutes generation and its boot check *requires* `LLM_BASE_URL` and `LLM_API_KEY` outside `local` — it will refuse to start without them. So:

1. Deploy llm.
2. Mint a `chat`-scoped key named `meeting`; append the JSON entry to llm's `CONSUMER_KEYS`; redeploy llm.
3. Set that plaintext key as `meeting`'s `LLM_API_KEY`, and `http://llm.railway.internal:${PORT}` as its `LLM_BASE_URL`.
4. Deploy `meeting`.

This is a genuine hard dependency, unlike `documentation-system` → `connectors`, which degrades gracefully.

## Rollback

`git revert` + push. No schema to reverse, no state to reconcile.

Note that **a key rotation is not covered by a code rollback** — `CONSUMER_KEYS` is a Railway variable, not source. Reverting a deploy leaves the current key set in place.

## Cost and quota notes

- Every `/chat` call bills against the AWS account. There is no per-consumer budget or rate limit in this service — a runaway consumer is bounded only by Bedrock's own throttling, which surfaces as **429**.
- `max_tokens` defaults to 16,000 and is capped at 64,000 by the request model. A consumer that doesn't set it gets the default, not the cap.
- `THINKING_DEFAULT=true` means extended thinking is on unless a consumer explicitly sends `"thinking": false`. That's a real cost difference; consumers doing cheap mechanical work should send `false`.
- Requests that fail validation (422) or authorization (403) are rejected before the provider is called and cost nothing.

## Troubleshooting

- **Every `/chat` returns 502.** The usual causes, in order: AWS credentials missing or wrong in the environment; `AWS_REGION` set to a region without model access; the configured `LLM_MODEL` not enabled on the account. All three normalize to `ProviderUnavailable`. `/health` stays green through all of them.
- **502 only for one chat model.** That model is in `ALLOWED_MODELS` but either unmapped in `BedrockConverseProvider`'s profile table or not enabled on the account.
- **Embedding failures.** See the [embedding error reference](API.md#post-embed).
- **429s under load.** Upstream throttling; the consumer decides whether to back off.
- **504s.** Provider timeout or connection failure; see [embedding timeout semantics](API.md#batching-and-timeouts). Large `max_tokens` with thinking enabled can hit chat timeouts.
- **Container dies at boot.** Outside `local`, check for the dev-default `API_KEY`, missing `AWS_REGION`, or missing `OPENAI_API_KEY`. Also check `EMBED_MODEL` is allowed and `CONSUMER_KEYS` is a JSON **array**.
- **A consumer suddenly gets 403.** Its key is valid but lacks the endpoint's `chat` or `embed` scope. Check its `CONSUMER_KEYS` entry — a key minted with the wrong scopes authenticates fine and fails only at authorization.
