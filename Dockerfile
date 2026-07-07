# ── Stage 1: build ────────────────────────────────────────────────────────────
FROM --platform=linux/arm64 python:3.12-alpine AS builder

RUN apk add --no-cache gcc musl-dev libffi-dev nodejs npm

WORKDIR /build

# Install claude CLI binary (required by claude-agent-sdk at runtime)
RUN npm install -g @anthropic-ai/claude-code

# Install Python packages
RUN pip install --no-cache-dir --prefix=/install claude-agent-sdk
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Stage 2: runtime ──────────────────────────────────────────────────────────
FROM --platform=linux/arm64 python:3.12-alpine

# Runtime deps: nodejs to run the claude CLI
RUN apk add --no-cache nodejs

WORKDIR /app

# Copy compiled Python packages from builder
COPY --from=builder /install /usr/local

# Copy claude CLI from builder
COPY --from=builder /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY --from=builder /usr/local/bin/claude /usr/local/bin/claude

# Copy application code
COPY app/ ./app/

# Skills directory (populated at container startup from S3)
RUN mkdir -p /app/skills && chown 1000:1000 /app/skills

RUN adduser -D -u 1000 appuser
USER appuser

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
