# GRINDER - Adaptive Grid Trading System
# Multi-stage build for minimal production image

# Builder stage
FROM python:3.11-slim AS builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy project files needed for install
COPY pyproject.toml README.md ./
COPY src/ src/

RUN pip install --no-cache-dir build && \
    pip install --no-cache-dir -e ".[dev]"

# Runtime stage
FROM python:3.11-slim

WORKDIR /app

# Create non-root user
RUN useradd -m -u 1000 appuser

# Copy from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code
COPY src/ src/
COPY scripts/ scripts/
COPY monitoring/ monitoring/

# Set ownership
RUN chown -R appuser:appuser /app

USER appuser

# Expose metrics port
EXPOSE 9090

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:9090/healthz || exit 1

# Entry point
ENTRYPOINT ["python", "-m", "scripts.run_live"]

# Default arguments
CMD ["--symbols", "BTCUSDT,ETHUSDT", "--duration-s", "0", "--metrics-port", "9090"]
