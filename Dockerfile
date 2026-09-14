# ==============================================================================
# Dockerfile for Ajin Scrap Monitoring Backend Service
# Follows non-root security principles, Python 3.13, and uv package manager.
# ==============================================================================

FROM python:3.13-slim AS builder

# Install uv from official binary
COPY --from=ghcr.io/astral-sh/uv:0.5.15 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

WORKDIR /app

# Install dependencies first for layer caching
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# Copy application source code
COPY src/ ./src/
COPY README.md ./

# Sync project
RUN uv sync --frozen --no-dev


# ==============================================================================
# Runtime Stage
# ==============================================================================
FROM python:3.13-slim AS runner

# Create non-root system user
RUN groupadd -r appgroup && useradd -r -g appgroup -u 1000 appuser

WORKDIR /app

# Copy virtual environment and app code
COPY --from=builder --chown=appuser:appgroup /app /app

# Switch to non-root user
USER appuser

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz')" || exit 1

ENTRYPOINT ["uvicorn", "src.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
