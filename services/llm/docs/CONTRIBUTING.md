# Contributing

Task walkthroughs for working on `llm`. Assumes you've read the [README](../README.md) and skimmed [ARCHITECTURE.md](ARCHITECTURE.md) — that doc explains the *why*; this one is the *how*.

## Conventions you need to know first

- **The Protocol is the hinge.** `src/providers/base.py` defines `LLMProvider` (one method: `chat(LLMRequest) -> LLMResult`). The router depends on that, never on a concrete provider.
- **Providers never import FastAPI; the router never imports a vendor SDK.** If you need `boto3` in `src/api/`, or `HTTPException` in `src/providers/`, the layering is wrong.
- **Two type families, mapped in one place.** `contracts/` holds Pydantic wire models; `src/providers/base.py` holds plain dataclasses. `src/api/routers/chat.py` is the only translator. Don't pass a `ChatRequest` into a provider.
- **Raise the normalized error.** Providers raise `ProviderRateLimited` / `ProviderTimeout` / `ProviderUnavailable`. A raw vendor exception escaping a provider becomes a 500 instead of a clean 502 — that's a bug.
- **Validate before you spend.** Anything checkable without calling the model belongs in the Pydantic model or a dependency, so it 422s/403s before a paid call.
- **Credentials are `SecretStr`.** Every credential field in `Settings` is `pydantic.SecretStr`; unwrap with `.get_secret_value()` at the boundary. See [`packages/auth/README.md`](../../../packages/auth/README.md#credential-config-convention).

## Local setup

```bash
cd services/llm
cp .env.example .env
uv sync --extra dev
uv run pytest          # confirm the environment works — no Docker, no network, no AWS
```

You only need AWS credentials to make *real* Bedrock calls. The suite uses a fake provider.

## Walkthrough: add a provider backend

Say you're adding a direct-to-Anthropic-API backend alongside the two Bedrock ones.

1. **Implement the Protocol** at `src/providers/anthropic_direct.py`. Copy `bedrock_converse.py` for shape:

   ```python
   class AnthropicDirectProvider:
       def __init__(self, *, api_key: str, default_model: str, timeout_s: float) -> None:
           ...

       def chat(self, request: LLMRequest) -> LLMResult:
           try:
               resp = self._client.messages.create(...)
           except RateLimitError as e:
               raise ProviderRateLimited(str(e)) from e
           except (APITimeoutError, APIConnectionError) as e:
               raise ProviderTimeout(str(e)) from e
           except Exception as e:
               raise ProviderUnavailable(str(e)) from e
           return LLMResult(content=..., model=..., stop_reason=...,
                            input_tokens=..., output_tokens=...)
   ```

   Import the vendor SDK **lazily inside the method or the constructor** if it's heavy — `GoogleSource` in `connectors` does this so the dependency tree never loads unless that backend is actually used.

2. **Register it** in `src/providers/registry.py`:

   ```python
   def _build_anthropic_direct(settings: Settings) -> LLMProvider:
       return AnthropicDirectProvider(
           api_key=settings.anthropic_api_key.get_secret_value(),
           default_model=settings.llm_model,
           timeout_s=settings.request_timeout_s,
       )

   PROVIDERS: dict[str, Callable[[Settings], LLMProvider]] = {
       "bedrock": _build_bedrock,
       "bedrock-converse": _build_bedrock_converse,
       "anthropic-direct": _build_anthropic_direct,
   }
   ```

   `get_provider` raises `ValueError` for an unknown `LLM_PROVIDER`, so a typo fails at wiring time.

3. **Add its config** to `src/config.py` (`SecretStr` for the key) and `.env.example`. Decide whether `verify_production_secrets()` should require it — see the config walkthrough below.

4. **Write tests** at `tests/test_anthropic_direct.py`. Copy `test_bedrock_converse.py`: construct the provider with a stubbed client, assert the neutral-type mapping in both directions, and assert each vendor exception maps to the right `ProviderError` subclass. Add a case to `tests/test_registry.py` asserting the new name resolves.

5. **Update the README's provider table** and the `LLM_PROVIDER` row in its config table.

## Walkthrough: allow a new model

Model names are an allowlist, not a passthrough.

1. Add it to `ALLOWED_MODELS` in `contracts/chat.py`.
2. **Map it in each provider that needs a mapping.** `BedrockConverseProvider` keeps an explicit neutral-name → inference-profile-id table; a name in `ALLOWED_MODELS` with no entry there will fail at call time rather than validation time.
3. Confirm the AWS account actually has access to it in the configured region — this is the usual cause of a 502 on a newly added model.
4. Add a validation test (the new name is accepted, a bogus one still 422s) and update the `model` row in [API.md](API.md) and the README.

## Walkthrough: add a config setting

1. Add the field to `Settings` in `src/config.py` (`SecretStr` if credential-shaped).
2. Add it to `.env.example` with a working local default and a comment.
3. Decide whether `verify_production_secrets()` should require it outside `local`. Ask: *would a deploy missing this be broken in a confusing way at request time?* If yes, add it — a misconfigured deploy should die at boot. `API_KEY` and `AWS_REGION` are required for exactly this reason.
4. Add a row to the README's configuration table.
5. Add a case to `tests/test_config.py` — there are existing ones for both "boot check passes" and "boot check refuses".

## Testing

Single-mode. No Docker, no database, no network, no AWS credentials:

```bash
uv run pytest
```

A `_FakeProvider` implementing `LLMProvider` is injected via `app.dependency_overrides[get_llm]`. Coverage spans the `/chat` happy path and neutral-type mapping, auth (missing key → 401, no `chat` scope → 403, `chat` → 200, `admin` → 200), validation (empty messages, unknown model → 422), provider-error → status mapping, the key store, the `llm-keys` CLI, config/boot checks, both Bedrock adapters against stubbed clients, the audit log, and the OpenAPI schema.

Useful invocations:

```bash
uv run pytest tests/test_chat.py     # one file
uv run pytest -k provider            # by name substring
uv run pytest -x -q                  # stop at first failure, quiet
```

**Never write a test that makes a real Bedrock call.** The suite must stay runnable offline and free.

## Linting and formatting

ruff (line length 100, target py311):

```bash
uv run ruff check .
uv run ruff format .
uv run ruff format --check .   # CI-style
```

CI runs `llm-test`: `uv sync --extra dev`, `uv run pytest`, `ruff check`, **and** `ruff format --check`. Both are enforced here.

## Checklist before you push

- [ ] New provider implements `LLMProvider` and is registered in `PROVIDERS`.
- [ ] Every vendor exception is normalized to a `ProviderError` subclass — none escape raw.
- [ ] No FastAPI import under `src/providers/`; no vendor SDK import under `src/api/`.
- [ ] A new model is in `ALLOWED_MODELS` **and** mapped in every provider that needs it.
- [ ] Anything checkable without a paid call fails at validation/auth time, not after.
- [ ] Credential settings are `SecretStr`, unwrapped only at the boundary.
- [ ] Tests run offline with no AWS credentials.
- [ ] `uv run pytest`, `uv run ruff check .`, and `uv run ruff format --check .` are clean.
