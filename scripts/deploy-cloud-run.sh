#!/bin/bash
# ==============================================================================
# AgentHarness Lab — Google Cloud Run Production Deployment (OpenRouter)
# ==============================================================================
set -e

PROJECT_ID="${GCP_PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || echo '')}"
REGION="${GCP_REGION:-us-central1}"
ARTIFACT_REPO="${ARTIFACT_REPO:-agentharness}"
BACKEND_SERVICE="${BACKEND_SERVICE:-agentharness-backend}"
FRONTEND_SERVICE="${FRONTEND_SERVICE:-agentharness-frontend}"
OPENROUTER_MODEL="${OPENROUTER_MODEL:-anthropic/claude-3.5-sonnet}"

if [ -z "${PROJECT_ID}" ]; then
    echo "Error: GCP_PROJECT_ID is not set and no default gcloud project found."
    echo "Usage: GCP_PROJECT_ID=your-project-id [GCP_REGION=us-central1] ./scripts/deploy-cloud-run.sh"
    exit 1
fi

echo "=========================================================="
echo "Deploying AgentHarness Lab to Google Cloud Run"
echo "Project:  ${PROJECT_ID}"
echo "Region:   ${REGION}"
echo "Model:    ${OPENROUTER_MODEL}"
echo "=========================================================="

# 1. Enable Required GCP APIs
echo "Step 1: Enabling required GCP APIs..."
gcloud services enable \
    run.googleapis.com \
    artifactregistry.googleapis.com \
    cloudbuild.googleapis.com \
    secretmanager.googleapis.com \
    --project="${PROJECT_ID}"

# 2. Create Artifact Registry Docker repository if not exists
echo "Step 2: Checking Artifact Registry repository..."
if ! gcloud artifacts repositories describe "${ARTIFACT_REPO}" --location="${REGION}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
    echo "Creating Artifact Registry repository '${ARTIFACT_REPO}' in ${REGION}..."
    gcloud artifacts repositories create "${ARTIFACT_REPO}" \
        --repository-format=docker \
        --location="${REGION}" \
        --project="${PROJECT_ID}" \
        --description="AgentHarness Lab container repository"
fi

REGISTRY_BASE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${ARTIFACT_REPO}"
BACKEND_IMAGE="${REGISTRY_BASE}/backend:latest"
FRONTEND_IMAGE="${REGISTRY_BASE}/frontend:latest"

# 3. Configure Docker auth
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet

# 4. Build and Push Backend Image
echo "Step 3: Building and pushing Backend image (${BACKEND_IMAGE})..."
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    docker build -t "${BACKEND_IMAGE}" ./backend
    docker push "${BACKEND_IMAGE}"
else
    echo "Building backend remotely using Cloud Build..."
    gcloud builds submit ./backend --tag "${BACKEND_IMAGE}" --project="${PROJECT_ID}"
fi

# 5. Build and Push Frontend Image
echo "Step 4: Building and pushing Frontend image (${FRONTEND_IMAGE})..."
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    docker build -t "${FRONTEND_IMAGE}" ./frontend
    docker push "${FRONTEND_IMAGE}"
else
    echo "Building frontend remotely using Cloud Build..."
    gcloud builds submit ./frontend --tag "${FRONTEND_IMAGE}" --project="${PROJECT_ID}"
fi

# 6. Deploy Backend Service to Cloud Run
echo "Step 5: Deploying Backend to Cloud Run (${BACKEND_SERVICE})..."
gcloud run deploy "${BACKEND_SERVICE}" \
    --image="${BACKEND_IMAGE}" \
    --region="${REGION}" \
    --project="${PROJECT_ID}" \
    --cpu=2 \
    --memory=4Gi \
    --port=8000 \
    --timeout=600 \
    --set-env-vars="LLM_PROVIDER=openrouter,OPENROUTER_BASE_URL=https://openrouter.ai/api/v1,OPENROUTER_MODEL=${OPENROUTER_MODEL},OPENROUTER_API_KEY=${OPENROUTER_API_KEY:-},AGENTHARNESS_CORS_ORIGINS=*" \
    --allow-unauthenticated

BACKEND_URL=$(gcloud run services describe "${BACKEND_SERVICE}" --region="${REGION}" --project="${PROJECT_ID}" --format='value(status.url)')
echo "Backend deployed at: ${BACKEND_URL}"

# 7. Deploy Frontend Service to Cloud Run
echo "Step 6: Deploying Frontend to Cloud Run (${FRONTEND_SERVICE})..."
gcloud run deploy "${FRONTEND_SERVICE}" \
    --image="${FRONTEND_IMAGE}" \
    --region="${REGION}" \
    --project="${PROJECT_ID}" \
    --cpu=1 \
    --memory=512Mi \
    --port=80 \
    --timeout=300 \
    --set-env-vars="BACKEND_URL=${BACKEND_URL}" \
    --allow-unauthenticated

FRONTEND_URL=$(gcloud run services describe "${FRONTEND_SERVICE}" --region="${REGION}" --project="${PROJECT_ID}" --format='value(status.url)')

echo "=========================================================="
echo "Deployment Complete!"
echo "Frontend Dashboard: ${FRONTEND_URL}"
echo "Backend API:        ${BACKEND_URL}"
echo "Health Check:       ${FRONTEND_URL}/healthz"
echo "=========================================================="
