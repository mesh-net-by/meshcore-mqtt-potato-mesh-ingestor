FROM python:3.11-slim

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock ./
COPY src/ src/

RUN uv sync --frozen --no-dev --no-build-isolation

RUN useradd --no-create-home --shell /bin/false appuser
USER appuser

CMD ["uv", "run", "python", "-m", "ingestor.main"]
