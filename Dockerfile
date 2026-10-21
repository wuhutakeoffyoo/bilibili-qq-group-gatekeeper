FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --locked --no-dev

FROM python:3.13-slim-bookworm
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY app ./app
COPY src/gatekeeper ./src/gatekeeper
COPY bot.py .env.example .env.prod.example groups.yaml review_pipeline.yaml ./
RUN useradd --system --uid 10001 --create-home gatekeeper \
    && mkdir -p /app/data /app/logs && chown -R gatekeeper:gatekeeper /app
USER gatekeeper
ENV PYTHONUNBUFFERED=1 HOST=0.0.0.0 PORT=1128
EXPOSE 1128
VOLUME ["/app/data", "/app/logs"]
CMD ["/app/.venv/bin/python", "bot.py"]
