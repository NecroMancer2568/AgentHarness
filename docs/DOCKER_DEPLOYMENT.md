# AgentHarness Lab — Production Docker & Cloud Run Deployment Guide

This document describes the complete architecture, containerization strategy, local deployment, and Google Cloud Run production deployment for **AgentHarness Lab**.

---

## 1. Architecture Overview

AgentHarness Lab is an autonomous multi-agent platform for repository analysis, automated debugging, test execution, and code review.

```
                          [ Client Browser ]
                                  │
                                  ▼
               ┌──────────────────────────────────────┐
               │         Frontend (Nginx / SPA)       │
               │   - React 18 + Vite (Static Assets)  │
               │   - Dynamic Reverse Proxy (/api/*)   │
               │   - SSE Streaming (text/event-stream)│
               └──────────────────┬───────────────────┘
                                  │ (HTTP / SSE)
                                  ▼
               ┌──────────────────────────────────────┐
               │        Backend (FastAPI Runtime)     │
               │   - Multi-Agent Orchestration Engine │
               │   - Python/Pytest Subprocess Sandbox │
               │   - Dynamic Host-aware Routing       │
               └──────────┬───────────────────┬───────┘
                          │                   │
         (REST / Inference API)       (SQLite / File I/O)
                          ▼                   ▼
       ┌──────────────────────┐    ┌─────────────────────────┐
       │     Ollama Server    │    │   Persistent Storage    │
       │  - LLM Inference     │    │  - SQLite (WAL Mode)    │
       │  - Model Cache Vol   │    │  - Cloned Repositories  │
       │  - Cloud API Proxy   │    │  - Isolated Virtualenvs │
       └──────────────────────┘    └─────────────────────────┘
```

### Components
1. **Frontend Container (`frontend/Dockerfile`)**:
   - Multi-stage build (`node:20-alpine` -> `nginx:alpine`).
   - Serves optimized React single-page app (SPA).
   - Nginx acts as reverse proxy for `/api/` endpoints with SSE streaming buffers disabled (`proxy_buffering off; chunked_transfer_encoding off;`).
   - Resolves dynamic `$BACKEND_URL` and `$PORT` at runtime via `docker-entrypoint.sh` using local DNS resolvers.
   - Non-root runtime configuration with health check at `/healthz`.

2. **Backend Container (`backend/Dockerfile`)**:
   - Python 3.13-slim multi-stage build.
   - System dependencies: `git`, `curl`, `gcc`, `g++`, `libffi-dev`.
   - Security: Runs as non-root user `agentharness` (UID 1000).
   - Manages subprocess execution for pytest and repository tools in isolated virtual environments (`/app/workspace/venvs`).
   - Sanitizes process environments (`_build_env`) preventing secret leakage to untrusted repository code.
   - Exposes health checks at `/health` and `/`.

3. **Ollama Inference Container**:
   - Official image `ollama/ollama:latest`.
   - Dedicated persistent volume mount at `/root/.ollama` to cache weights.
   - Configurable host binding (`OLLAMA_HOST=0.0.0.0:11434`).
   - Supports local GPU/CPU execution or acts as proxy to Ollama Cloud (`-cloud` model descriptors).

---

## 2. Prerequisites

### Local Development
- **Docker Desktop** (macOS / Windows) or **Docker Engine + Docker Compose v2** (Linux).
  - Recommended allocation: At least 4 CPU cores, 8 GB RAM (16 GB+ if running local LLM weights on CPU).
- **Git** (for repository checkout).
- **Curl** (for health checks and test scripts).

### Production (Google Cloud Run)
- A **Google Cloud Platform (GCP)** project with billing enabled.
- **Google Cloud SDK (`gcloud` CLI)** installed and authenticated:
  ```bash
  gcloud auth login
  gcloud config set project YOUR_PROJECT_ID
  ```
- Elevated IAM permissions for Cloud Run, Artifact Registry, and Cloud Build.

---

## 3. Local Docker Setup

The local environment uses `docker-compose.yml` to spin up the entire multi-container network.

```bash
# Clone the repository
git clone <repo-url> agentharness-lab
cd agentharness-lab

# Copy the environment template
cp .env.example .env
```

---

## 4. Environment Configuration

The root `.env` file controls container startup, port bindings, and LLM orchestration settings:

```dotenv
# ==============================================================================
# AgentHarness Lab — Docker & Cloud Run Environment
# ==============================================================================

# Application & Network Ports
PORT=8000
FRONTEND_PORT=5173
OLLAMA_PORT=11434

# Backend Service Connection (Used by Nginx Reverse Proxy)
BACKEND_URL=http://backend:8000

# Backend Configuration
AGENTHARNESS_DATABASE_URL=sqlite:////app/workspace/agentharness.sqlite3
AGENTHARNESS_WORKSPACE_DIR=/app/workspace
AGENTHARNESS_CORS_ORIGINS=["http://localhost:5173","http://localhost:8000","http://127.0.0.1:5173"]

# LLM & Ollama Configuration
# Inside Docker network, use 'http://ollama:11434'. For host Ollama, use 'http://host.docker.internal:11434'
AGENTHARNESS_OLLAMA_BASE_URL=http://ollama:11434
AGENTHARNESS_MODEL=gemma4:31b-cloud

# If using Ollama Cloud proxy models (*-cloud), provide your Ollama Cloud API Key:
OLLAMA_API_KEY=

# Execution Limits & Timeouts
AGENTHARNESS_AGENT_TIMEOUT_SECONDS=180
AGENTHARNESS_MAX_ITERATIONS=10
AGENTHARNESS_TASK_TIMEOUT_SECONDS=600
AGENTHARNESS_DEPENDENCY_INSTALL_TIMEOUT_SECONDS=300
```

---

## 5. Ollama Model Provisioning

### Understanding `-cloud` vs Local Models
The default model configured in AgentHarness Lab is:
```
AGENTHARNESS_MODEL=gemma4:31b-cloud
```
> [!IMPORTANT]
> **What is `-cloud`?**
> Models ending with `-cloud` (such as `gemma4:31b-cloud`) are **Ollama Cloud remote proxy manifests** (~300 bytes), not full local model weights.
> When using a `-cloud` model:
> 1. You must obtain an API key from [Ollama.com](https://ollama.com).
> 2. Set `OLLAMA_API_KEY=<your-key>` in `.env`.
> 3. Ollama routes inference requests securely to Ollama's cloud compute.

### Local Downloadable Weights
If you want to run completely offline without an Ollama Cloud account or external API key, switch `AGENTHARNESS_MODEL` in `.env` to a local weight model:
- `qwen2.5-coder:7b` (recommended for coding tasks, ~4.7 GB)
- `gemma2:9b` (~5.4 GB)
- `llama3.2:3b` (lightweight, ~2.0 GB)

### Provisioning Script
Run the automated model provisioning helper:
```bash
# Provision whatever model is configured in .env
./scripts/init-ollama.sh

# Or provision an explicit model
./scripts/init-ollama.sh qwen2.5-coder:7b
```

Alternatively, use the Docker Compose `setup` profile:
```bash
docker compose --profile setup up model-init
```

---

## 6. Starting the Application

Build and launch all services in detached mode:

```bash
docker compose up -d --build
```

Docker Compose will start:
1. `agentharness-ollama`: Starts the Ollama server on port 11434 with volume mount `agentharness_ollama_data`.
2. `agentharness-backend`: Starts FastAPI on port 8000, runs database migrations, mounts `agentharness_workspace`.
3. `agentharness-frontend`: Starts Nginx on port 5173, reverse proxying `/api/` to `backend:8000`.

Check health:
- **Frontend UI**: [http://localhost:5173](http://localhost:5173)
- **Frontend Health**: [http://localhost:5173/healthz](http://localhost:5173/healthz)
- **Backend Health**: [http://localhost:8000/health](http://localhost:8000/health)
- **Proxy Health**: [http://localhost:5173/api/health](http://localhost:5173/api/health)
- **Ollama API**: [http://localhost:11434/api/version](http://localhost:11434/api/version)

---

## 7. Stopping the Application

To gracefully stop the running containers:
```bash
docker compose stop
```

To stop and remove containers and internal networks while preserving database and model storage:
```bash
docker compose down
```

To wipe all persistent data (database, test workspaces, and downloaded LLM models):
```bash
docker compose down -v
```

---

## 8. Viewing Logs

Stream logs for all services:
```bash
docker compose logs -f
```

Stream logs for an individual service:
```bash
# Backend logs
docker compose logs -f backend

# Frontend Nginx access and error logs
docker compose logs -f frontend

# Ollama inference logs
docker compose logs -f ollama
```

---

## 9. Persistent Storage Architecture

AgentHarness Lab stores critical state in persistent filesystems:
1. **SQLite Database (`agentharness.sqlite3`)**:
   - Location: `/app/workspace/agentharness.sqlite3`
   - Configured with Write-Ahead Logging (WAL) mode for concurrency.
2. **Execution Workspaces & Cloned Repositories**:
   - Location: `/app/workspace/runs/` and `/app/workspace/repos/`
3. **Repository Dependency Virtual Environments**:
   - Location: `/app/workspace/venvs/`
   - Cache hash-keyed environments for fast reuse across evaluation runs.
4. **Ollama Model Weights & Descriptors**:
   - Location: `/root/.ollama`

### Local Mounts
In `docker-compose.yml`, the local host workspace is directly mounted:
- `${WORKSPACE_PATH:-./workspace}:/app/workspace`: Binds the host's `./workspace/` directory into the backend container, so your existing SQLite database (`agentharness.sqlite3`), past evaluation runs, and all registered repositories (`workspace/repositories/*`) are immediately preserved and loaded seamlessly.
- `ollama_data`: Named Docker volume mounted to `/root/.ollama` in the Ollama container to cache model weights across runs.

### Cloud Run Considerations
Google Cloud Run instances have ephemeral, in-memory filesystems (`/tmp`) by default.
For production deployments on Cloud Run requiring state persistence across revisions:
1. **Cloud Storage FUSE Mount**: Cloud Run supports mounting a Google Cloud Storage (GCS) bucket directly as a directory.
2. **Serverless VPC Access + Cloud SQL / Filestore**:
   - For high-concurrency multi-instance production, replace SQLite with Cloud SQL PostgreSQL or mount a Google Cloud Filestore (NFS) share for the workspace directory.

---

## 10. Troubleshooting

### Problem: Frontend shows "Unable to reach AgentHarness backend"
- **Cause**: Nginx cannot resolve `backend:8000`.
- **Fix**: Verify backend is healthy (`docker compose ps backend`). Ensure both containers are on the `agentharness-net` bridge network.

### Problem: SSE events (real-time agent streaming) freeze or arrive in bulk
- **Cause**: Upstream proxy buffering is enabled.
- **Fix**: The included `frontend/nginx.conf.template` disables buffering:
  ```nginx
  proxy_buffering off;
  proxy_cache off;
  proxy_set_header Connection '';
  chunked_transfer_encoding off;
  ```

### Problem: `pull model manifest: 401 Unauthorized` for `gemma4:31b-cloud`
- **Cause**: Cloud models require an Ollama Cloud credential.
- **Fix**: Add `OLLAMA_API_KEY=your_key` to `.env` or change `AGENTHARNESS_MODEL=qwen2.5-coder:7b` for local weight execution.

### Problem: Backend exits with permission denied in `/app/workspace`
- **Cause**: Volume was created by root user on host.
- **Fix**: The backend image runs as non-root UID 1000 (`agentharness`). `docker-entrypoint.sh` automatically checks permissions; ensure host folder permissions grant access to UID 1000.

---

## 11. Google Cloud Setup

1. **Install and Initialize `gcloud`**:
   ```bash
   gcloud components update
   gcloud auth login
   ```

2. **Select or Create a GCP Project**:
   ```bash
   export PROJECT_ID="agentharness-prod"
   gcloud config set project ${PROJECT_ID}
   ```

3. **Enable Required APIs**:
   ```bash
   gcloud services enable \
     run.googleapis.com \
     artifactregistry.googleapis.com \
     cloudbuild.googleapis.com \
     secretmanager.googleapis.com
   ```

---

## 12. Artifact Registry Setup

Create a Docker repository in your target region:

```bash
export REGION="us-central1"
export REPO_NAME="agentharness"

gcloud artifacts repositories create ${REPO_NAME} \
  --repository-format=docker \
  --location=${REGION} \
  --description="AgentHarness Lab container images"
```

Configure Docker authentication for Artifact Registry:
```bash
gcloud auth configure-docker ${REGION}-docker.pkg.dev
```

---

## 13. Cloud Run Deployment

You can deploy automatically using `scripts/deploy-cloud-run.sh` or execute the step-by-step commands below.

### Step 1: Build & Push Images
```bash
export REGISTRY="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO_NAME}"

# Build & push backend
docker build -t ${REGISTRY}/backend:latest ./backend
docker push ${REGISTRY}/backend:latest

# Build & push frontend
docker build -t ${REGISTRY}/frontend:latest ./frontend
docker push ${REGISTRY}/frontend:latest
```

### Step 2: Deploy Backend to Cloud Run
```bash
gcloud run deploy agentharness-backend \
  --image ${REGISTRY}/backend:latest \
  --region ${REGION} \
  --platform managed \
  --allow-unauthenticated \
  --port 8000 \
  --cpu 2 \
  --memory 4Gi \
  --timeout 600 \
  --set-env-vars AGENTHARNESS_DATABASE_URL=sqlite:////app/workspace/agentharness.sqlite3 \
  --set-env-vars AGENTHARNESS_MODEL=gemma4:31b-cloud \
  --set-env-vars AGENTHARNESS_OLLAMA_BASE_URL=https://ollama.com \
  --set-env-vars AGENTHARNESS_CORS_ORIGINS='["*"]'
```

Retrieve the backend URL:
```bash
BACKEND_URL=$(gcloud run services describe agentharness-backend \
  --region ${REGION} \
  --format 'value(status.url)')
echo "Backend URL: ${BACKEND_URL}"
```

### Step 3: Deploy Frontend to Cloud Run
Deploy the frontend container with `BACKEND_URL` pointing to your Cloud Run backend:

```bash
gcloud run deploy agentharness-frontend \
  --image ${REGISTRY}/frontend:latest \
  --region ${REGION} \
  --platform managed \
  --allow-unauthenticated \
  --port 80 \
  --cpu 1 \
  --memory 512Mi \
  --set-env-vars BACKEND_URL=${BACKEND_URL}
```

---

## 14. Ollama Deployment & Connectivity Options

When running on Google Cloud Run, choose between two primary LLM deployment patterns:

### Option A: External Ollama or Ollama Cloud (Recommended for Cost & Simplicity)
Run your backend on Cloud Run and connect to Ollama Cloud or an external GPU VM:
- Set `AGENTHARNESS_OLLAMA_BASE_URL=https://ollama.com` (or your VM IP).
- Store `OLLAMA_API_KEY` securely in Secret Manager:
  ```bash
  echo -n "your-ollama-cloud-api-key" | gcloud secrets create ollama-api-key --data-file=-
  ```
- Bind secret to Cloud Run backend:
  ```bash
  gcloud run services update agentharness-backend \
    --region ${REGION} \
    --set-secrets OLLAMA_API_KEY=ollama-api-key:latest
  ```

### Option B: Cloud Run with GPU (Self-Hosted Ollama on Cloud Run)
Cloud Run supports GPU acceleration (Nvidia L4):
```bash
gcloud beta run deploy agentharness-ollama \
  --image ollama/ollama:latest \
  --region us-central1 \
  --cpu 4 \
  --memory 16Gi \
  --gpu 1 \
  --gpu-type nvidia-l4 \
  --no-cpu-throttling \
  --port 11434 \
  --allow-unauthenticated
```

---

## 15. Cost & Hardware Considerations

| Configuration | Compute Type | Memory | Estimated Cost | Best For |
|---|---|---|---|---|
| **Cloud Run Backend + Frontend + Ollama Cloud** | Standard Cloud Run (CPU) | Backend: 4Gi, Frontend: 512Mi | ~$5 – $25 / month (scale to zero) | Production on budget, development |
| **Cloud Run + Nvidia L4 GPU (Self-Hosted)** | GPU-accelerated Cloud Run | 16Gi RAM + 1x L4 (24GB VRAM) | ~$0.60 – $0.90 / instance-hour | Self-contained on-premise privacy |
| **Local Docker** | Local Workstation CPU/Metal | 8GB – 16GB RAM | Free | Offline development, debugging |

> [!TIP]
> Use Cloud Run's `--min-instances 0` setting for development to automatically scale to zero when idle, minimizing costs.

---

## 16. Security & Execution Sandbox Limitations

AgentHarness Lab executes untrusted code from checked-out Git repositories to run tests and verify fixes. The containerization architecture provides defense-in-depth:

1. **Non-Root Execution**:
   - The backend runs as unprivileged user `agentharness` (UID 1000).
   - Root privilege escalation inside the container is blocked.
2. **Subprocess Environment Sanitization**:
   - In `backend/app/repository.py`, the `_build_env()` function explicitly filters process environment variables.
   - Credentials such as `OLLAMA_API_KEY`, tokens, and database passwords are removed from pytest execution environments.
3. **Execution Timeouts**:
   - Dependency installations are bounded by `AGENTHARNESS_DEPENDENCY_INSTALL_TIMEOUT_SECONDS` (default: 300s).
   - Test runs are bounded by `AGENTHARNESS_TASK_TIMEOUT_SECONDS` (default: 600s).
4. **Cloud Run gVisor Sandboxing**:
   - Google Cloud Run executes containers inside the **gVisor** container sandbox.
   - gVisor intercepts and virtualizes Linux kernel syscalls, providing a strong security boundary between the container and the host kernel.

---

## 17. Updating Images & Redeploying

To deploy code updates to production:

```bash
# 1. Pull latest code
git pull origin main

# 2. Run the deployment script
./scripts/deploy-cloud-run.sh
```

Or trigger automated updates through your CI/CD pipeline (e.g., GitHub Actions) by tagging commits and triggering `gcloud run deploy`.

