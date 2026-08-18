# ============================================================
# WAY TERO — BACKEND DOCKERFILE
# Doc Ref: Docker Architecture — Multi-stage build
# Doc Ref: Base image: python:3.13-slim
# Doc Ref: Non-root user, health check on /health
# ============================================================

# ---- Stage 1: Builder ----
FROM python:3.13-slim AS builder

WORKDIR /build

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies into /install
COPY requirements/ requirements/
WORKDIR /build/requirements
RUN pip install --upgrade pip && \
    pip install --prefix=/install --no-cache-dir -r production.txt
WORKDIR /build


# ---- Stage 2: Runtime ----
FROM python:3.13-slim AS runtime

# Non-root user (Doc: Never run as root)
RUN groupadd -r waytero && useradd -r -g waytero waytero

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Install runtime system deps only
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Copy application code
COPY --chown=waytero:waytero . .

# Switch to non-root user
USER waytero

# Expose port
EXPOSE 8000

# Health check (Doc Ref: Docker Architecture Section 29)
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import httpx; httpx.get(\"http://localhost:8000/health\").raise_for_status()" || exit 1

# Start uvicorn
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--log-level", "info", "--access-log"]
