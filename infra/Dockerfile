# Build from this repo root:
#   docker build -f infra/Dockerfile -t polymaker:latest .
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        libnss3 \
        libnspr4 \
        libatk1.0-0 \
        libatk-bridge2.0-0 \
        libcups2 \
        libdrm2 \
        libxkbcommon0 \
        libxcomposite1 \
        libxdamage1 \
        libxfixes3 \
        libxrandr2 \
        libgbm1 \
        libasound2 \
        libpango-1.0-0 \
        libcairo2 \
        fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv

WORKDIR /app

COPY . /app

RUN uv sync --frozen --no-dev \
    && uv run playwright install chromium \
    && mkdir -p journal logs data-aggregation/output poly-sharp-finder/signals poly-sharp-finder/intents

ENV PATH="/app/.venv/bin:$PATH"

ENTRYPOINT ["uv", "run"]
CMD ["python", "scripts/run_sharp_pipeline.py", "--help"]
