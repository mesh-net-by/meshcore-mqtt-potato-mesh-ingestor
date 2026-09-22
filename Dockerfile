FROM python:3.11-slim

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.12.17 /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock ./

# Install third-party dependencies from lockfile (pre-built wheels only, no setup scripts)
RUN uv sync --frozen --no-dev --no-build --no-install-project

COPY src/ src/

# Install only the first-party package (--no-deps = no third-party builds)
RUN uv pip install --no-deps --no-build-isolation -e .

RUN useradd --no-create-home --shell /bin/false appuser
USER appuser

CMD ["/app/.venv/bin/python", "-m", "ingestor.main"]
