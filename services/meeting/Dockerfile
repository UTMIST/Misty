FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.26 /uv /usr/local/bin/uv

WORKDIR /app

# No ffmpeg binary needed: Opus decode runs in-process via PyAV (which bundles
# its own ffmpeg libraries), and nothing shells out to ffmpeg any more now that
# audio is never mixed or persisted.

# Workspace root manifest + lockfile, the shared package(s), and this service.
COPY pyproject.toml uv.lock ./
COPY packages/ ./packages/
COPY services/meeting/ ./services/meeting/

# Sync only this member (+ workspace deps) into the root .venv.
RUN uv sync --frozen --no-dev --package meeting

ENV PATH="/app/.venv/bin:$PATH"
WORKDIR /app/services/meeting

# Run unprivileged (Trivy DS-0002).
RUN useradd --create-home --shell /bin/bash appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8004

# --ws-ping-*: uvicorn defaults to a 20s keepalive ping with a 20s pong
# deadline, so a client that is briefly slow to answer gets its socket closed
# mid-recording -- a leading suspect for two production meetings that dropped
# with a clean close and no error on either side. 60s is still well inside any
# proxy idle timeout. Keep this in step with railway.json's startCommand, which
# overrides this CMD on Railway.
CMD ["uvicorn", "src.api.app:app", "--host", "0.0.0.0", "--port", "8004", \
     "--ws-ping-interval", "60", "--ws-ping-timeout", "60"]
