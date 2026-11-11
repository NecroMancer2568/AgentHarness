# AgentHarness Lab

**A multi-agent engineering harness for real-world repository tasks.**

AgentHarness Lab is a web application that routes a software task through a coordinated team of AI agents. The agents inspect a Git repository, locate relevant code and tests, implement changes, run verification, and produce a reviewable execution report.

The goal is to make agent-driven software engineering observable. A run can expose its status, agent activity, tool calls, token usage, duration, test results, and review outcome—not just a final generated answer.

> **Deployment:** React frontend on Cloudflare Pages, FastAPI backend on Northflank, and model inference through OpenRouter.

## Features

- Submit a repository and describe a bug fix or engineering task.
- Coordinate specialist agents across repository analysis, test discovery, implementation, debugging, and review.
- Clone and work against a repository copy.
- Prepare dependencies and run relevant tests, including pytest for Python repositories.
- Review the resulting changes and verification evidence.
- Inspect run status, agent activity, tool calls, token usage, duration, and test metrics in the dashboard.

A successful run depends on the configured verification and review checks passing. A generated patch alone is not treated as proof of success.

## Agent workflow

| Agent | Responsibility |
|---|---|
| Coordinator | Interprets the task and coordinates the workflow |
| Test Scout | Finds relevant tests and verification targets |
| Repository Analyst | Explores the codebase and traces relevant implementation |
| Coding Agent | Implements the requested changes |
| Test Debug Agent | Runs tests and investigates failures |
| Code Reviewer | Reviews the changes and verification evidence |

The exact execution path may vary by task and orchestration decisions.

## Architecture

```text
┌──────────────────────────────────────┐
│          React + TypeScript          │
│                Web UI                │
└───────────────────┬──────────────────┘
                    │ HTTP API
┌───────────────────▼──────────────────┐
│                FastAPI               │
│               Backend                │
├──────────────────────────────────────┤
│ Agent orchestration                  │
│ Repository / Git operations          │
│ Tool execution                       │
│ Dependency preparation               │
│ Test execution and review            │
│ Run records, metrics and artifacts   │
└───────────────┬──────────────┬───────┘
                │              │
       ┌────────▼──────┐  ┌────▼──────────────┐
       │  OpenRouter   │  │ Repository work  │
       │  Chat models  │  │ Git / subprocess │
       │  + tool calls │  │ / pytest         │
       └───────────────┘  └───────────────────┘
```

## Technology stack

| Layer | Technology |
|---|---|
| Frontend | React, TypeScript, Vite |
| Backend | Python, FastAPI |
| Model access | OpenRouter |
| Repository workflow | Git and subprocess-based tooling |
| Verification | pytest and repository-specific test commands |
| Evaluation | Inspect AI |
| Containerization | Docker |
| Backend hosting | Northflank |
| Frontend hosting | Cloudflare Pages |

## Live deployment

The deployed application uses two services:

- **Frontend:** Cloudflare Pages
- **Backend API:** Northflank

Configure the frontend build variable `VITE_API_BASE_URL` with the public backend URL. Configure the backend CORS policy to allow the exact deployed frontend origin.

The OpenRouter API key must remain in the backend's runtime environment. Never expose it through a `VITE_` variable or commit it to source control.

## Run locally

### Prerequisites

- Git
- Python version supported by the backend dependencies
- Node.js and npm
- OpenRouter API key
- A chat-completion model that supports the tool/function-calling features used by the agents

### 1. Clone the repository

```bash
git clone <YOUR_REPOSITORY_URL>
cd agentharness-lab
```

### 2. Configure the backend

Create the backend environment file using the repository's environment template, if present. Configure:

```env
LLM_PROVIDER=openrouter
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
OPENROUTER_API_KEY=your_openrouter_api_key
OPENROUTER_MODEL=your_chat_model_id
```

**Important:** `OPENROUTER_MODEL` must identify a chat-capable model. An embeddings-only model cannot be used with the chat-completions endpoint. If the project has a separate embedding configuration, configure it independently.

Never commit `.env` files, API keys, or generated credentials.

### 3. Install backend dependencies

```bash
cd backend
python -m venv .venv
```

Activate the environment:

```bash
# macOS / Linux
source .venv/bin/activate

# Windows PowerShell
.venv\Scripts\Activate.ps1
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Start the API. For the standard `app.main` entry point:

```bash
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

If your checkout uses a different ASGI module, use the entry point defined by that backend version.

The API documentation is typically available at `http://127.0.0.1:8000/docs`.

### 4. Install and start the frontend

In a second terminal:

```bash
cd frontend
npm install
```

Create `frontend/.env.local`:

```env
VITE_API_BASE_URL=http://127.0.0.1:8000
```

Start Vite:

```bash
npm run dev
```

Open the local URL printed in the terminal.

### 5. Build the frontend

```bash
cd frontend
npm run build
```

The production build is written to `frontend/dist`.

## Docker

The backend Dockerfile is located at `backend/Dockerfile`. Build using the backend directory as the build context:

```bash
docker build -t agentharness-backend ./backend
```

Run the container:

```bash
docker run --rm -p 8000:8000 \
  -e LLM_PROVIDER=openrouter \
  -e OPENROUTER_BASE_URL=https://openrouter.ai/api/v1 \
  -e OPENROUTER_API_KEY=your_openrouter_api_key \
  -e OPENROUTER_MODEL=your_chat_model_id \
  agentharness-backend
```

For production, inject secrets through the hosting platform's environment-variable or secret manager rather than committing them to files.

## Configuration

| Variable | Purpose |
|---|---|
| `LLM_PROVIDER` | Selects the configured model provider |
| `OPENROUTER_BASE_URL` | OpenRouter API base URL |
| `OPENROUTER_API_KEY` | Backend-only OpenRouter credential |
| `OPENROUTER_MODEL` | Chat-capable model used for agent generation |
| `VITE_API_BASE_URL` | Backend API URL embedded into the frontend build |

The backend may support additional settings. Check the environment template and settings module in your checkout for the complete list.

### Choosing an OpenRouter model

AgentHarness Lab uses models for multi-step agent work and tool calls. Select a model that supports:

- Chat completions
- Tool/function calling
- Sufficient context length for repository and test output
- Reliable instruction following for code changes and review

Free model availability, rate limits, and tool support can change. Verify the selected model's current OpenRouter model page before using it.

## Evaluation

The project includes an evaluation integration using **Inspect AI**. Use the evaluation scripts and task definitions present in the repository for the supported evaluation workflow.

Useful measurements for agent runs include:

- Task success and verification outcome
- Test pass/failure counts
- Total duration and inference time
- LLM calls and token usage
- Tool calls and agent handoffs
- Review verdict
- Infrastructure failures, reported separately from agent failures

Do not treat one successful run as proof of general reliability. Results depend on the task, repository, model, tests, and execution environment.

## Security and operational considerations

This is a development and portfolio demo, **not a hardened public code-execution service**.

- **Treat repository code as untrusted.** Build scripts, dependency installation, and tests can execute arbitrary code.
- Do not allow unrestricted public repository execution without authentication, authorization, quotas, rate limits, and an appropriate isolation boundary.
- Keep provider credentials on the backend only. Rotate any key exposed in a repository, archive, log, or screenshot.
- Restrict production CORS to the exact frontend origin.
- Review the filesystem, process, and network permissions available to repository subprocesses.
- Local SQLite data, workspaces, and generated artifacts may be lost when an ephemeral container is replaced. Configure persistent storage and backups before relying on long-term run history.
- Avoid writing private repository contents or secrets into logs and generated reports.

## Known limitations

- Model quality, tool-calling behavior, rate limits, and availability depend on the selected OpenRouter model.
- Test execution is only as reliable as the repository's test suite and dependency setup.
- Passing tests do not guarantee that a change is correct or secure.
- Arbitrary repository execution needs stronger isolation before broad public use.
- Persistence depends on the backend hosting and storage configuration.

## Roadmap

Potential next improvements:

- Stronger sandboxing for repository execution
- Authentication, per-user authorization, and usage quotas
- Persistent run and artifact storage
- More robust dependency caching and language-specific test runners
- Expanded Inspect AI evaluation suites
- Reproducible run manifests and richer trace inspection
- Security tests for prompt injection and unsafe tool use

## Contributing

Contributions and issue reports are welcome.

1. Fork the repository.
2. Create a focused feature branch.
3. Make the change and add or update tests.
4. Run the relevant checks locally.
5. Open a pull request describing the change, verification performed, and known limitations.

Do not include API keys, private repository data, or generated credentials in commits or issue reports.

## License

Add the license that applies to this repository before distributing the project or accepting external contributions. Until a license is included, do not assume the code is available for unrestricted reuse.

---

**Built as a practical exploration of multi-agent software engineering, repository automation, and measurable agent verification.**
