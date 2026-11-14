# Production-ready Dockerfile for FastAPI backend
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

# Install essential system dependencies:
# - git: for cloning, inspecting, and managing repositories
# - curl: for container healthchecks
# - build-essential: for compiling C-extensions in Python wheels
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Create dedicated non-root application user
RUN useradd -u 1000 -m -s /bin/bash agentharness

WORKDIR /app

# Install Python dependencies first for build cache efficiency
COPY requirements.txt .
RUN pip install --no-cache-dir --default-timeout=100 --retries=10 -r requirements.txt

# Copy application sources
COPY --chown=agentharness:agentharness backend/app/ /app/app/
COPY --chown=agentharness:agentharness backend/evaluations/ /app/evaluations/
COPY --chown=agentharness:agentharness workspace/repositories/agentharness-benchmark-fixture/ /app/workspace/repositories/agentharness-benchmark-fixture/
COPY backend/docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

# Create workspace directory structure and assign ownership to agentharness
RUN mkdir -p /app/workspace/repositories /app/workspace/runs /app/workspace/venvs \
    && chown -R agentharness:agentharness /app/workspace

# Configure git identity for non-root user
USER agentharness
RUN git config --global user.email "agentharness@local.internal" \
    && git config --global user.name "AgentHarness Lab" \
    && git config --global init.defaultBranch main

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://127.0.0.1:${PORT:-8000}/health || exit 1

ENTRYPOINT ["/docker-entrypoint.sh"]

