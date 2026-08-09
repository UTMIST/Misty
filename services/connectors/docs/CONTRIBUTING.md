# Contributing

Task walkthroughs for working on `connectors`. Assumes you've read the [README](../README.md) and skimmed [ARCHITECTURE.md](ARCHITECTURE.md) — that doc explains the *why*; this one is the *how*.

Written for rotating contributors. When in doubt, copy the shape of the nearest existing example; the codebase is intentionally repetitive so patterns are easy to imitate.

## Conventions you need to know first

- **The Protocol is the hinge.** `src/sources/base.py` defines `SourceFetcher` (one method: `fetch(url) -> SourceResult`) and the five-error hierarchy. The router depends on those, never on `GoogleSource`.
- **Sources never import FastAPI.** Anything under `src/sources/` is a plain library. If you find yourself wanting `HTTPException` in there, you want a `SourceError` subclass instead — the router maps it.
- **Raise the normalized error, never the vendor one.** Route every Google API call through `execute()` (`google_extractors/base.py`) so `HttpError` → `SourceForbidden`/`SourceNotFound`/`SourceUnavailable` happens in one place.
- **Extractors declare their own needs.** `scopes` and `services` are attributes on the extractor. `required_scopes()` / `required_services()` union across the registry — never hard-code a scope at the build site.
- **Warnings, not silent loss.** Any partial extraction (a truncated sheet tab, a PDF with no text layer) appends to `ExtractedText.warnings`, which propagates to the response. Losing content silently is the bug this exists to prevent.
- **Credentials are `SecretStr`.** Every credential field in `Settings` is `pydantic.SecretStr`; unwrap with `.get_secret_value()` at the boundary. See [`packages/auth/README.md`](../../../packages/auth/README.md#credential-config-convention) for how forgetting to unwrap fails silently.

## Local setup

```bash
cd services/connectors
cp .env.example .env
uv sync --extra dev
uv run pytest          # confirm the environment works — no Docker, no network
```

No database, no Docker, no Google credentials needed. The whole suite runs against fakes.

## Walkthrough: add an extractor for a new MIME type

The most common change. Say you're adding support for uploaded `.rtf` files.

1. **Write the extractor** at `src/sources/google_extractors/rtf.py`. Copy `pdf.py` (local byte parsing) or `docs.py` (native Google API) depending on which shape yours is. It must satisfy the `Extractor` protocol:

   ```python
   RTF_MIME = "application/rtf"

   class RtfExtractor:
       scopes = (DRIVE_READONLY,)      # only if you need a NEW scope
       services = ("drive",)           # Google API clients you need

       def extract(self, services: dict, file_id: str, mime: str) -> ExtractedText:
           data = execute(services["drive"].files().get_media(fileId=file_id))
           text, warnings = _parse(data)
           return ExtractedText(text=text, warnings=warnings)
   ```

2. **Register it** in `src/sources/google.py`:

   ```python
   from src.sources.google_extractors.rtf import RTF_MIME, RtfExtractor

   EXTRACTORS: dict[str, Extractor] = {
       ...
       RTF_MIME: RtfExtractor(),
   }
   ```

   That is the whole wiring. `required_scopes()` and `required_services()` pick up your declared tuples automatically.

3. **Only if you're using a Google API that isn't already wired**, add its discovery version to `_API_VERSIONS` in the same file:

   ```python
   _API_VERSIONS = {"drive": "v3", "docs": "v1", "slides": "v1", "sheets": "v4", "forms": "v1"}
   ```

   Forms was the first extractor to hit this — it added `"forms": "v1"` alongside its registry entry. Miss this and `_build_services` raises `SourceNotConfigured` at fetch time.

   **Downloading bytes through Drive needs no new API and no new scope.** `drive.readonly` already covers the media path used by the PDF and `.docx` extractors. Only a native Google API (Docs/Slides/Sheets/Forms/…) adds either.

4. **If you added a scope**, say so in the README's "Scopes" section *and* the setup runbook's "enable these APIs" list. A scope added in code but not in the runbook means the next person's service account silently lacks it.

5. **Write tests** at `tests/test_rtf_extractor.py`. Copy `test_pdf_extractor.py`. Cover:
   - the happy path against a fake Drive client,
   - the partial/lossy case, asserting the warning text,
   - a 403 and a 404 from the fake, asserting they surface as `SourceForbidden` / `SourceNotFound`.

6. **Update the supported-types table** in the README. It's the table consumers actually read.

## Walkthrough: add a whole new source (non-Google)

Say you're adding Notion.

1. **Implement `SourceFetcher`** at `src/sources/notion.py`. One public method, `fetch(url) -> SourceResult`. Raise the `SourceError` subclasses — do not invent new ones without also adding a router mapping.

2. **Add a builder and register the ids** in `src/sources/registry.py`:

   ```python
   def _build_notion(settings: Settings) -> SourceFetcher:
       return NotionSource(token=settings.notion_token.get_secret_value(), ...)

   SOURCE_BUILDERS: dict[str, Callable[[Settings], SourceFetcher]] = {
       **{sid: _build_google for sid in GOOGLE_SOURCE_IDS},
       "notion": _build_notion,
   }
   ```

   `build_registry` already dedupes by builder, so several source ids sharing one instance is handled for you.

3. **Add config** to `src/config.py` — `SecretStr` for anything credential-shaped. Decide deliberately whether it belongs in `verify_production_secrets()`. Google's does *not*, because "no credential" is a supported running state; if your source is a hard dependency for a consumer, yours might.

4. **Tests**: a `tests/test_notion_source.py` with a fake client, plus a case in `tests/test_registry.py` asserting the new id resolves.

5. **Document it** in the README's `source_id` list — the caller supplies `source_id`, so an unregistered one is a 422 they'll hit immediately.

## Walkthrough: add a config setting

1. Add the field to `Settings` in `src/config.py` (`SecretStr` if credential-shaped).
2. Add it to `.env.example` with a working local default and a comment explaining what it's for.
3. Decide whether `verify_production_secrets()` should require it outside `local`. Ask: *would a deploy missing this be broken in a way that's confusing at request time?* If yes, add it — fail at boot instead.
4. Add a row to the README's configuration table.
5. Add a case to `tests/test_config.py` — there are existing ones for both "boot check passes" and "boot check refuses".

## Testing

Single-mode. No Docker, no database, no network, no Google credentials:

```bash
uv run pytest
```

Google API clients are injected as fakes — either through `app.dependency_overrides[get_source_registry]` for route-level tests, or via `GoogleSource(services={...})` for extractor-level ones. That constructor parameter exists precisely so tests can bypass credential building.

Useful invocations:

```bash
uv run pytest tests/test_fetch_route.py     # one file
uv run pytest -k extractor                  # by name substring
uv run pytest -x -q                         # stop at first failure, quiet
```

## Linting and formatting

ruff (line length 100, target py311; see `pyproject.toml`):

```bash
uv run ruff check .            # lint
uv run ruff check --fix .      # lint + auto-fix the safe ones
uv run ruff format .           # format
uv run ruff format --check .   # verify without writing (CI-style)
```

CI runs `connectors-test`: `uv sync --extra dev`, `uv run pytest`, `ruff check`, **and** `ruff format --check`. Both are enforced on every Python service.

## Checklist before you push

- [ ] New extractor is registered in `EXTRACTORS`, and declares its own `scopes` / `services`.
- [ ] Any genuinely new Google API has an `_API_VERSIONS` entry.
- [ ] A new scope is reflected in the README's Scopes list **and** the service-account runbook.
- [ ] Every Google API call goes through `execute()`; no bare `HttpError` escapes.
- [ ] Lossy extraction appends a warning rather than silently dropping content.
- [ ] Credential settings are `SecretStr`, unwrapped only at the boundary.
- [ ] Supported-types table in the README matches the registry.
- [ ] `uv run pytest`, `uv run ruff check .`, and `uv run ruff format --check .` are clean.
