# Multi-stage build for Agent Grid Coordinator

# Stage 1: Build dependencies
FROM python:3.12-slim AS builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install poetry
RUN pip install --no-cache-dir poetry==1.8.2

# Copy dependency files
COPY pyproject.toml poetry.lock* ./

# Export dependencies to requirements.txt (without dev dependencies)
RUN poetry export -f requirements.txt --output requirements.txt --without-hashes

# Stage 2: Runtime image
FROM python:3.12-slim AS runtime

WORKDIR /app

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN useradd --create-home --shell /bin/bash appuser

# Copy requirements from builder
COPY --from=builder /app/requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY src/ ./src/

# Copy alembic configuration
COPY alembic.ini ./

# Copy startup scripts
COPY scripts/ ./scripts/
RUN chmod +x scripts/*.sh

# Set ownership
RUN chown -R appuser:appuser /app

# Switch to non-root user
USER appuser

# Environment variables (can be overridden at runtime)
ENV AGENT_GRID_HOST=0.0.0.0
ENV AGENT_GRID_PORT=8000
ENV AGENT_GRID_DEPLOYMENT_MODE=coordinator
ENV PYTHONPATH=/app/src
ENV PYTHONUNBUFFERED=1

# Expose the application port
EXPOSE 8000

# Note: App Runner handles health checks via its own configuration
# Do not use Docker HEALTHCHECK as it may conflict with App Runner's health checks

# Run the application (runs migrations first, then starts uvicorn)
CMD ["./scripts/start.sh"]
