# Pinned rather than tracking whatever is local: the checked-in .venv is
# 3.14, and the image should not change under us when that does.
FROM python:3.12-slim

# zoneinfo has no database of its own on slim, and the whole point of the
# TZ setting is that the SRS day rolls over where the learner is.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies before source, so editing a tool doesn't re-resolve them.
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    TZ=Asia/Singapore

RUN useradd --create-home --uid 1000 app && chown -R app:app /app
USER app

EXPOSE 8080

CMD ["bunpro-mcp"]
