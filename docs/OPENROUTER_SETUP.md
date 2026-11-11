# AgentHarness Lab — OpenRouter API Setup & Deployment Guide

This guide walks through configuring, testing, and deploying **AgentHarness Lab** using the **OpenRouter API** as the unified LLM inference provider.

---

## 1. Creating an OpenRouter Account

1. Visit [https://openrouter.ai](https://openrouter.ai).
2. Sign in with GitHub, Google, or your email.
3. OpenRouter provides a single, unified OpenAI-compatible endpoint with access to dozens of state-of-the-art models (Anthropic Claude, OpenAI GPT-4o, DeepSeek, Qwen Coder, Google Gemini, Meta Llama, etc.).

---

## 2. Generating an API Key

1. Navigate to **Account Settings** $\to$ **Keys** ([https://openrouter.ai/settings/keys](https://openrouter.ai/settings/keys)).
2. Click **Create Key**.
3. Give your key a descriptive name (e.g. `agentharness-lab`).
4. Optionally configure a credit limit for the key to avoid unexpected spend.
5. Copy the generated API key (format: `sk-or-v1-...`).

> [!WARNING]
> Keep your API key secret. Never commit your `.env` file to version control, bake it into Docker image layers, or expose it in client-side code.

---

## 3. Finding Model Identifiers

OpenRouter models use the format `provider/model-name`.

You can browse all models and their capabilities at [https://openrouter.ai/models](https://openrouter.ai/models).

### Recommended Coding Models (Tool/Function-Calling Supported)

AgentHarness Lab agents use tool calling (`read_file`, `write_file`, `run_tests`, `git_diff`, `list_files`, `search_text`). Make sure to choose a model that natively supports **tools/function calling**:

| Model ID | Provider | Context Window | Capabilities & Best Use Case |
|---|---|---|---|
| `anthropic/claude-3.7-sonnet` | Anthropic | 200k | Top-tier reasoning, hybrid thinking, and outstanding code generation |
| `anthropic/claude-3.5-sonnet` | Anthropic | 200k | Industry benchmark for agentic coding and precise tool execution |
| `openai/gpt-4o` | OpenAI | 128k | Fast, excellent JSON structured output and tool selection |
| `openai/gpt-4o-mini` | OpenAI | 128k | Budget-friendly option for simple debugging passes |
| `qwen/qwen-2.5-coder-32b-instruct` | Qwen | 128k | High-performance open-weights model fine-tuned for software engineering |
| `deepseek/deepseek-chat` | DeepSeek | 64k | Extremely cost-effective reasoning and code generation |
| `google/gemini-2.0-flash-001` | Google | 1M | Ultra-fast inference with massive context capacity |
| `meta-llama/llama-3.3-70b-instruct` | Meta | 128k | High-capability open-weight flagship |

---

## 4. Configuring Environment Variables

Create or edit your local environment file (`.env` in the root or `backend/.env`):

```dotenv
# LLM Provider Configuration (OpenRouter)
LLM_PROVIDER=openrouter
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
OPENROUTER_API_KEY=sk-or-v1-your-actual-api-key
OPENROUTER_MODEL=anthropic/claude-3.5-sonnet

# Request Timeouts and Retries
OPENROUTER_TIMEOUT_SECONDS=180
OPENROUTER_MAX_RETRIES=2

# Optional OpenRouter Application Metadata Headers
OPENROUTER_HTTP_REFERER=https://github.com/agentharness/agentharness-lab
OPENROUTER_TITLE=AgentHarness Lab

# Workspace Storage
WORKSPACE_PATH=./workspace

# Execution Limits
AGENTHARNESS_MAX_AGENT_STEPS=8
AGENTHARNESS_MAX_PARALLEL_READERS=2
AGENTHARNESS_TEST_TIMEOUT_SECONDS=180
AGENTHARNESS_DEPENDENCY_TIMEOUT_SECONDS=300
```

---

## 5. Running Locally (Python + Node.js)

### 1. Test OpenRouter Connectivity
Verify your key and model before starting the full application:
```bash
python3 scripts/test-openrouter.py
```

### 2. Start the Backend (FastAPI)
```bash
cd backend
source .venv/bin/activate
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Verify backend health:
```bash
curl http://127.0.0.1:8000/api/health
```

Expected response:
```json
{
  "status": "ok",
  "model": "anthropic/claude-3.5-sonnet",
  "provider_configured": true,
  "repository_root": "..."
}
```

### 3. Start the Frontend (Vite)
In another terminal:
```bash
cd frontend
npm install
npm run dev
```

Open [http://localhost:5173](http://localhost:5173).

---

## 6. Running Through Docker Compose

The Docker Compose configuration includes only the `frontend` and `backend` containers, communicating directly with OpenRouter via HTTPS.

```bash
# 1. Ensure .env has your OPENROUTER_API_KEY and OPENROUTER_MODEL
cp .env.example .env
# Edit .env with your credentials

# 2. Build and start containers
docker compose up -d --build

# 3. View live logs
docker compose logs -f backend

# 4. Check services health
docker compose ps
```

- **Frontend Dashboard**: [http://localhost:5173](http://localhost:5173)
- **Backend API Docs**: [http://localhost:8000/docs](http://localhost:8000/docs)
- **Health Check**: [http://localhost:5173/healthz](http://localhost:5173/healthz)

---

## 7. Deploying on Render

AgentHarness Lab includes a production-ready [`render.yaml`](file:///Users/enigma/Downloads/agentharness-lab/render.yaml) Blueprint.

### Option A: Using Render Blueprints (Recommended)
1. Push your repository to GitHub.
2. In the [Render Dashboard](https://dashboard.render.com), click **New +** $\to$ **Blueprint**.
3. Connect your repository. Render automatically reads `render.yaml` and sets up both:
   - `agentharness-backend` (Docker Web Service)
   - `agentharness-frontend` (Docker Web Service reverse-proxying API calls)
4. Render will prompt you for secret environment variables:
   - `OPENROUTER_API_KEY`: Paste your OpenRouter API key.
   - `OPENROUTER_MODEL`: Enter your desired model (e.g. `anthropic/claude-3.5-sonnet`).
5. Click **Apply**.

### Option B: Manual Web Service Setup on Render
1. Click **New +** $\to$ **Web Service**.
2. Connect your GitHub repository.
3. Configure settings:
   - **Name**: `agentharness-backend`
   - **Runtime**: `Docker`
   - **Docker Context**: `./backend`
   - **Dockerfile Path**: `./backend/Dockerfile`
   - **Health Check Path**: `/health`
4. Add Persistent Disk (recommended for production):
   - **Mount Path**: `/app/workspace`
   - **Size**: 10 GB
5. Configure Environment Variables:
   - `PORT`: `10000`
   - `LLM_PROVIDER`: `openrouter`
   - `OPENROUTER_BASE_URL`: `https://openrouter.ai/api/v1`
   - `OPENROUTER_API_KEY`: *(your OpenRouter API key)*
   - `OPENROUTER_MODEL`: *(e.g. `anthropic/claude-3.5-sonnet`)*
   - `AGENTHARNESS_DATABASE`: `/app/workspace/agentharness.sqlite3`
   - `AGENTHARNESS_REPOS_ROOT`: `/app/workspace/repositories`
   - `AGENTHARNESS_RUNS_ROOT`: `/app/workspace/runs`
   - `AGENTHARNESS_VENV_CACHE_ROOT`: `/app/workspace/venvs`
   - `AGENTHARNESS_CORS_ORIGINS`: `*`

---

## 8. Configuring Render Secrets

To update your OpenRouter API key or model in Render without rebuilding images:
1. Go to your service in the Render Dashboard.
2. Click **Environment**.
3. Under **Secret Files** or **Environment Variables**, update `OPENROUTER_API_KEY` or `OPENROUTER_MODEL`.
4. Click **Save Changes** — Render will automatically perform a zero-downtime rolling restart.

---

## 9. Troubleshooting Authentication Errors

### Error: `OPENROUTER_API_KEY is missing`
- **Cause**: No key was found in the environment or `.env` file.
- **Solution**: Export `OPENROUTER_API_KEY="sk-or-v1-..."` or add it to `backend/.env`.

### Error: `OpenRouter authentication failed: 401 Unauthorized`
- **Cause**: Invalid API key, key revoked, or insufficient account balance.
- **Solution**:
  1. Test the key directly:
     ```bash
     curl -H "Authorization: Bearer $OPENROUTER_API_KEY" https://openrouter.ai/api/v1/auth/key
     ```
  2. Confirm your OpenRouter account has positive credits at [https://openrouter.ai/settings/credits](https://openrouter.ai/settings/credits).

---

## 10. Troubleshooting Unsupported Tool Calls

### Error: `Model xyz does not support tools/function calling`
- **Cause**: The model specified in `OPENROUTER_MODEL` is a pure completion or reasoning model without tool-use fine-tuning.
- **Solution**: Switch to a verified tool-calling model:
  - `anthropic/claude-3.5-sonnet`
  - `openai/gpt-4o`
  - `qwen/qwen-2.5-coder-32b-instruct`
  - `deepseek/deepseek-chat`
  - `google/gemini-2.0-flash-001`

---

## 11. Rate Limits and Free Model Restrictions

- **Free Tier Models (`:free`)**: Models ending in `:free` on OpenRouter (e.g. `meta-llama/llama-3.3-70b-instruct:free`) share a global rate limit (typically 20 requests/minute) and may intermittently reject tool calling or experience queue delays.
- **Paid Models**: Paid models have high dedicated concurrency limits. Ensure you have purchased credits if running large benchmark sweeps.

---

## 12. Monitoring Token Usage and Inference Costs

AgentHarness Lab automatically tracks:
- **Prompt tokens**
- **Completion tokens**
- **Total tokens**
- **LLM invocation count**
- **Wall-clock latency** per agent call

These metrics are recorded per run in SQLite and displayed live on the dashboard **Overview** and **Metrics** views. You can also inspect real-time usage and cost per model in your [OpenRouter Activity Dashboard](https://openrouter.ai/activity).

