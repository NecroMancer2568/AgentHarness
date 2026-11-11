#!/usr/bin/env bash
# ==============================================================================
# AgentHarness Lab — Google Cloud Run Automated Deployment Script
# ==============================================================================
set -euo pipefail

# Default configuration values
REGION="${GCP_REGION:-us-central1}"
ARTIFACT_REPO="${ARTIFACT_REPO:-agentharness}"
BACKEND_SERVICE="${BACKEND_SERVICE:-agentharness-backend}"
FRONTEND_SERVICE="${FRONTEND_SERVICE:-agentharness-frontend}"
OLLAMA_SERVICE="${OLLAMA_SERVICE:-agentharness-ollama}"
DEPLOY_OLLAMA_GPU="${DEPLOY_OLLAMA_GPU:-false}"

echo "=============================================================================="
echo "AgentHarness Lab — Production Deployment on Google Cloud Run"
echo "=============================================================================="

# 1. Verify gcloud CLI is installed
if ! command -v gcloud >/dev/null 2>&1; then
    echo "Error: Google Cloud SDK (gcloud) is not installed."
    echo "Install gcloud from https://cloud.google.com/sdk/docs/install and authenticate."
    exit 1
fi

# 2. Verify active project
PROJECT_ID="${GCP_PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
if [ -z "${PROJECT_ID}" ] || [ "${PROJECT_ID}" = "(unset)" ]; then
    echo "Error: No GCP project selected."
    echo "Set GCP_PROJECT_ID in your environment or run: gcloud config set project <PROJECT_ID>"
    exit 1
fi

echo "Active Project:  ${PROJECT_ID}"
echo "Target Region:   ${REGION}"
echo "Artifact Repo:   ${ARTIFACT_REPO}"
echo "Backend Service: ${BACKEND_SERVICE}"
echo "Frontend Service:${FRONTEND_SERVICE}"
echo "------------------------------------------------------------------------------"

# 3. Enable required Google Cloud APIs
echo "Step 1: Enabling required Google Cloud APIs..."
gcloud services enable \
    run.googleapis.com \
    artifactregistry.googleapis.com \
    cloudbuild.googleapis.com \
    secretmanager.googleapis.com \
    --project="${PROJECT_ID}"

# 4. Create Artifact Registry repository if it does not already exist
echo "Step 2: Checking Artifact Registry repository..."
if ! gcloud artifacts repositories describe "${ARTIFACT_REPO}" \
    --location="${REGION}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
    echo "Creating Artifact Registry repository '${ARTIFACT_REPO}' in ${REGION}..."
    gcloud artifacts repositories create "${ARTIFACT_REPO}" \
        --repository-format=docker \
        --location="${REGION}" \
        --description="AgentHarness Lab container images" \
        --project="${PROJECT_ID}"
else
    echo "Artifact Registry repository '${ARTIFACT_REPO}' already exists."
fi

REGISTRY_BASE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${ARTIFACT_REPO}"
BACKEND_IMAGE="${REGISTRY_BASE}/backend:latest"
FRONTEND_IMAGE="${REGISTRY_BASE}/frontend:latest"
OLLAMA_IMAGE="${REGISTRY_BASE}/ollama:latest"

# 5. Build and push backend image
echo "Step 3: Building and pushing Backend image (${BACKEND_IMAGE})..."
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet
    docker build -t "${BACKEND_IMAGE}" ./backend
    docker push "${BACKEND_IMAGE}"
else
    echo "Building backend remotely using Cloud Build..."
    gcloud builds submit ./backend --tag "${BACKEND_IMAGE}" --project="${PROJECT_ID}"
fi

# 6. Build and push frontend image
echo "Step 4: Building and pushing Frontend image (${FRONTEND_IMAGE})..."
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    docker build -t "${FRONTEND_IMAGE}" ./frontend
    docker push "${FRONTEND_IMAGE}"
else
    echo "Building frontend remotely using Cloud Build..."
    gcloud builds submit ./frontend --tag "${FRONTEND_IMAGE}" --project="${PROJECT_ID}"
fi

# 7. Deploy Ollama on Cloud Run if GPU deployment requested
OLLAMA_BASE_URL="${OLLAMA_BASE_URL:-}"
if [ "${DEPLOY_OLLAMA_GPU}" = "true" ]; then
    echo "Step 5a: Deploying self-hosted Ollama with GPU acceleration on Cloud Run..."
    gcloud run deploy "${OLLAMA_SERVICE}" \
        --image="ollama/ollama:latest" \
        --region="${REGION}" \
        --project="${PROJECT_ID}" \
        --cpu=4 \
        --memory=16Gi \
        --gpu=1 \
        --gpu-type=nvidia-l4 \
        --no-cpu-throttling \
        --port=11434 \
        --timeout=600 \
        --allow-unauthenticated

    OLLAMA_URL=$(gcloud run services describe "${OLLAMA_SERVICE}" --region="${REGION}" --project="${PROJECT_ID}" --format='value(status.url)')
    OLLAMA_BASE_URL="${OLLAMA_URL}/v1"
    echo "Ollama deployed at: ${OLLAMA_BASE_URL}"
else
    # Default: use external Ollama endpoint, Ollama Cloud, or user-provided URL
    if [ -z "${OLLAMA_BASE_URL}" ]; then
        OLLAMA_BASE_URL="https://ollama.com/v1"
        echo "Step 5b: Using Ollama Cloud endpoint: ${OLLAMA_BASE_URL}"
    else
        echo "Step 5b: Using configured Ollama endpoint: ${OLLAMA_BASE_URL}"
    fi
fi

# 8. Deploy Backend Service to Cloud Run
echo "Step 6: Deploying Backend to Cloud Run (${BACKEND_SERVICE})..."
gcloud run deploy "${BACKEND_SERVICE}" \
    --image="${BACKEND_IMAGE}" \
    --region="${REGION}" \
    --project="${PROJECT_ID}" \
    --cpu=2 \
    --memory=4Gi \
    --port=8000 \
    --timeout=600 \
    --set-env-vars="OLLAMA_BASE_URL=${OLLAMA_BASE_URL},OLLAMA_MODEL=${OLLAMA_MODEL:-gemma4:31b-cloud},OLLAMA_API_KEY=${OLLAMA_API_KEY:-},AGENTHARNESS_CORS_ORIGINS=*" \
    --allow-unauthenticated

BACKEND_URL=$(gcloud run services describe "${BACKEND_SERVICE}" --region="${REGION}" --project="${PROJECT_ID}" --format='value(status.url)')
echo "Backend deployed at: ${BACKEND_URL}"

# 9. Deploy Frontend Service to Cloud Run
echo "Step 7: Deploying Frontend to Cloud Run (${FRONTEND_SERVICE})..."
gcloud run deploy "${FRONTEND_SERVICE}" \
    --image="${FRONTEND_IMAGE}" \
    --region="${REGION}" \
    --project="${PROJECT_ID}" \
    --cpu=1 \
    --memory=512Mi \
    --port=80 \
    --set-env-vars="BACKEND_URL=${BACKEND_URL}" \
    --allow-unauthenticated

FRONTEND_URL=$(gcloud run services describe "${FRONTEND_SERVICE}" --region="${REGION}" --project="${PROJECT_ID}" --format='value(status.url)')

# Update CORS on Backend to allow the deployed Frontend origin
echo "Step 8: Updating Backend CORS origins with deployed frontend URL..."
gcloud run services update "${BACKEND_SERVICE}" \
    --region="${REGION}" \
    --project="${PROJECT_ID}" \
    --update-env-vars="AGENTHARNESS_CORS_ORIGINS=${FRONTEND_URL}"

echo "=============================================================================="
echo "DEPLOYMENT COMPLETED SUCCESSFULLY!"
echo "Frontend Web App:  ${FRONTEND_URL}"
echo "Backend API:       ${BACKEND_URL}"
echo "Ollama Endpoint:   ${OLLAMA_BASE_URL}"
echo "=============================================================================="

