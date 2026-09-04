# ── Stage 1: build ────────────────────────────────────────────────────────────
FROM --platform=linux/arm64 python:3.12-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Stage 2: runtime ──────────────────────────────────────────────────────────
FROM --platform=linux/arm64 python:3.12-slim

# Runtime dependencies for TLS downloads and scientific Python.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy compiled Python packages from builder
COPY --from=builder /install /usr/local

# Copy application code
COPY app/ ./app/

# Claude Agent SDK discovers project skills from /app/.claude/skills. The image
# intentionally contains no default skills; an optional S3 sync populates it.
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /app/.claude/skills \
    && chown -R 1000:1000 /app/.claude
USER appuser

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
