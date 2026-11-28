# SCOWL English word lists used by the weekly vocabulary command.
FROM debian:bookworm-slim AS vocabulary-words
RUN apt-get update && apt-get install -y --no-install-recommends \
    wamerican-large=2020.12.07-2 wbritish-large=2020.12.07-2 wcanadian-large=2020.12.07-2

FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY --from=vocabulary-words /usr/share/dict/*-english-large /usr/share/dict/
COPY --from=vocabulary-words /usr/share/doc/wamerican-large/copyright /usr/share/doc/scowl/copyright
RUN mkdir -p /usr/share/misty-vocabulary
ADD --chmod=0444 --checksum=sha256:8061b9f768a9b9ec70b0468431278aa7b0e30a65d024f39042f7b26295b71378 https://raw.githubusercontent.com/streetsidesoftware/cspell-dicts/498d2b4fe97816860beae66463131fd59be9d133/dictionaries/software-terms/dict/softwareTerms.txt /usr/share/misty-vocabulary/softwareTerms.txt
ADD --chmod=0444 --checksum=sha256:3654d7893d7803ec3682c9ffa19a0bff092e4cb6d4caef7d41804c37e3baf18a https://raw.githubusercontent.com/streetsidesoftware/cspell-dicts/498d2b4fe97816860beae66463131fd59be9d133/dictionaries/software-terms/dict/computing-acronyms.txt /usr/share/misty-vocabulary/computing-acronyms.txt
ADD --chmod=0444 --checksum=sha256:6245ecd81789d113a600fa0e4f5f503e6d1b4f425860f6047dc945560dfcf13c https://raw.githubusercontent.com/streetsidesoftware/cspell-dicts/498d2b4fe97816860beae66463131fd59be9d133/dictionaries/software-terms/LICENSE /usr/share/misty-vocabulary/LICENSE

WORKDIR /app

# Workspace root manifest + lockfile, the shared package(s), and this service.
COPY pyproject.toml uv.lock ./
COPY packages/ ./packages/
COPY services/documentation-system/ ./services/documentation-system/

# Sync only this member (+ workspace deps) into the root .venv.
RUN uv sync --frozen --no-dev --package documentation-system

ENV PATH="/app/.venv/bin:$PATH"
WORKDIR /app/services/documentation-system

EXPOSE 8001

# Migrations run as a separate deploy step, NOT here.
CMD ["uvicorn", "src.api.app:app", "--host", "0.0.0.0", "--port", "8001"]
