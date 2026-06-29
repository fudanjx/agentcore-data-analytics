# ── Stage 1: build ────────────────────────────────────────────────────────────
FROM python:3.12-alpine AS builder

RUN apk add --no-cache gcc musl-dev postgresql-dev libffi-dev nodejs npm

WORKDIR /build

# Install claude CLI binary (required by claude-agent-sdk at runtime)
RUN npm install -g @anthropic-ai/claude-code

# Install Python packages
RUN pip install --no-cache-dir --prefix=/install claude-agent-sdk
COPY requirements.txt .
RUN sed 's/psycopg2-binary.*/psycopg2/' requirements.txt > requirements-alpine.txt && \
    pip install --no-cache-dir --prefix=/install -r requirements-alpine.txt


# ── Stage 2: runtime ──────────────────────────────────────────────────────────
FROM python:3.12-alpine

# Runtime deps: libpq for psycopg2, nodejs to run the claude CLI
RUN apk add --no-cache libpq nodejs

WORKDIR /app

# Copy compiled Python packages from builder
COPY --from=builder /install /usr/local

# Copy claude CLI from builder
COPY --from=builder /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY --from=builder /usr/local/bin/claude /usr/local/bin/claude

# Copy application code
COPY app/ ./app/

RUN adduser -D -u 1000 appuser
USER appuser

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
