FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Workspace root manifest + lockfile, the shared package(s), and this service.
COPY pyproject.toml uv.lock ./
COPY packages/ ./packages/
COPY services/connectors/ ./services/connectors/

# Sync only this member (+ workspace deps) into the root .venv.
RUN uv sync --frozen --no-dev --package connectors

ENV PATH="/app/.venv/bin:$PATH"
WORKDIR /app/services/connectors

# Run unprivileged (Trivy DS-0002).
RUN useradd --create-home --shell /bin/bash appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8005

CMD ["uvicorn", "src.api.app:app", "--host", "0.0.0.0", "--port", "8005"]
