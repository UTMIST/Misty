FROM python:3.11-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY packages/ ./packages/
COPY services/gateway/ ./services/gateway/
RUN uv sync --frozen --no-dev --package gateway
ENV PATH="/app/.venv/bin:$PATH"
WORKDIR /app/services/gateway
EXPOSE 8000
CMD ["uvicorn", "src.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
