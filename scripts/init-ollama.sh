#!/bin/sh
set -e

OLLAMA_HOST="${OLLAMA_HOST:-http://ollama:11434}"
OLLAMA_MODEL="${OLLAMA_MODEL:-gemma4:31b-cloud}"
MAX_WAIT_SECONDS=60

echo "=========================================================="
echo "AgentHarness Lab — Ollama Model Initialization"
echo "Target Ollama Host: ${OLLAMA_HOST}"
echo "Configured Model:   ${OLLAMA_MODEL}"
echo "=========================================================="

# 1. Wait for Ollama service to become healthy
echo "Waiting for Ollama service at ${OLLAMA_HOST} to be ready..."
elapsed=0
until curl -sf "${OLLAMA_HOST}/api/version" > /dev/null 2>&1; do
    sleep 2
    elapsed=$((elapsed + 2))
    if [ "$elapsed" -ge "$MAX_WAIT_SECONDS" ]; then
        echo "Error: Ollama service at ${OLLAMA_HOST} did not become ready within ${MAX_WAIT_SECONDS} seconds."
        exit 1
    fi
done

echo "Ollama service is up and responsive."

# 2. Check if model is already loaded
TAGS_JSON=$(curl -sf "${OLLAMA_HOST}/api/tags" 2>/dev/null || echo "{}")
if echo "$TAGS_JSON" | grep -q "\"name\":\"${OLLAMA_MODEL}\""; then
    echo "Model '${OLLAMA_MODEL}' is already initialized in Ollama. Skipping pull."
    exit 0
fi

# 3. Detect -cloud models vs self-hosted weights
if echo "${OLLAMA_MODEL}" | grep -q -- "-cloud$"; then
    echo "----------------------------------------------------------"
    echo "NOTICE: Model '${OLLAMA_MODEL}' is tagged with '-cloud'."
    echo "This model routes inference to Ollama Cloud rather than storing"
    echo "standalone multi-gigabyte local weights on disk."
    echo ""
    echo "To use this model:"
    echo "1. Set OLLAMA_API_KEY in your .env file."
    echo "2. The backend LLMClient will proxy requests using this key."
    echo ""
    echo "For 100% offline self-hosted local inference, configure a local model:"
    echo "  OLLAMA_MODEL=qwen2.5-coder:7b   (Recommended for coding tasks on 16GB+ RAM / GPU)"
    echo "  OLLAMA_MODEL=llama3.2:3b        (Lightweight for CPU / memory-constrained environments)"
    echo "  OLLAMA_MODEL=gemma2:9b          (High quality reasoning and tool use)"
    echo "----------------------------------------------------------"
    exit 0
fi

# 4. Pull local model weights into persistent volume
echo "Pulling model '${OLLAMA_MODEL}' into Ollama storage..."
curl -X POST "${OLLAMA_HOST}/api/pull" -d "{\"name\": \"${OLLAMA_MODEL}\", \"stream\": false}" || {
    echo "Warning: Direct pull via API returned non-zero. Attempting pull via Ollama CLI if available..."
    if command -v ollama >/dev/null 2>&1; then
        OLLAMA_HOST="${OLLAMA_HOST}" ollama pull "${OLLAMA_MODEL}"
    else
        echo "Failed to pull model '${OLLAMA_MODEL}'. Ensure network access is available."
        exit 1
    fi
}

echo "Model '${OLLAMA_MODEL}' successfully initialized and ready."

