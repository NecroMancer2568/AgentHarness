# AgentHarness Lab

A local-first multi-agent software-engineering workbench. It runs LLM-powered agents against real Git repositories, executes allowlisted test commands in disposable clones, records tool traces and token/latency metrics, and exposes the results through a React dashboard.

**Model:** DeepSeek V4 Pro through Ollama Cloud (`deepseek-v4-pro:cloud`).  
**Backend:** Python, FastAPI, OpenAI-compatible Ollama Cloud client, SQLite.  
**Frontend:** React, TypeScript, Vite, Recharts.  
**Validation:** backend unit tests, real test-runner execution, recorded run-verification rules, and an included Inspect AI task adapter for repeatable task sets.

This is a practical engineering project, not a claim of novel research. The metrics are operational measurements from actual executions, not benchmark results baked into the UI.

## What is implemented

- LLM-powered coordinator, repository analyst, test scout, coding agent, test/debug agent, and independent code reviewer. A security reviewer is routed in when the coordinator flags security-sensitive work.
- Dependency-aware execution: repository analysis and test discovery can run concurrently; coding, testing, and review run in order.
- Adaptive routing, compact structured handoffs, bounded reader concurrency, timeouts, and provider-reported token accounting.
- Real repository inspection and edits through constrained tools: `list_files`, `read_file`, `search_text`, `write_file`, `run_tests`, and `git_diff`.
- Disposable Git clone per run. The original repository is not edited by the harness.
- Explicit test-command allowlist and repository-path boundary checks.
- SQLite-backed run history, structured events, agent outputs, changed files, diffs, and live polling/SSE endpoints.
- Dashboard pages for overview, single-task runs, a repeatable benchmark suite, live execution inspector, history, metrics, repositories, model configuration, and settings.

## Requirements

- Python 3.11+ recommended.
- Node.js 20+ and npm.
- Git.
- An Ollama Cloud account and API key. The model identifier and endpoint are configurable.
- Required test runtimes for the repositories you intend to evaluate (for example, Python/pytest, Node/npm, Go, Rust, Maven, or Gradle).

## Quick start (macOS / Linux)

### Option A: Docker Compose (Recommended for reproducible local deployment)

AgentHarness Lab includes a complete multi-container Docker Compose setup for the Frontend, Backend, and Ollama:

```bash
# 1. Copy environment template
cp .env.example .env

# 2. Build and launch all services in detached mode
docker compose up -d --build

# 3. (Optional) Provision the local or cloud LLM model
./scripts/init-ollama.sh
```

- **Frontend Dashboard**: <http://localhost:5173>
- **Backend API & Docs**: <http://localhost:8000/docs>
- **Backend Health Check**: <http://localhost:8000/health>
- **Detailed Guide & Cloud Run Deployment**: See [docs/DOCKER_DEPLOYMENT.md](file:///Users/enigma/Downloads/agentharness-lab/docs/DOCKER_DEPLOYMENT.md).

### Option B: Local Virtualenv Setup

From the project root:

### 1. Configure the backend

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `backend/.env` and set `OLLAMA_API_KEY` to your Ollama Cloud API key. Do not commit `.env` or put the key in the frontend.

Defaults are configured for:

```dotenv
OLLAMA_BASE_URL=https://ollama.com/v1
OLLAMA_MODEL=deepseek-v4-pro:cloud
AGENTHARNESS_REPOS_ROOT=workspace/repositories
AGENTHARNESS_RUNS_ROOT=workspace/runs
AGENTHARNESS_DATABASE=workspace/agentharness.sqlite3
```

These relative paths resolve from the project root, not from the shell's current directory.

### 2. Add a real Git repository

Clone the target project into `workspace/repositories/` and commit the starting state you want to evaluate. Example:

```bash
cd ..
git clone https://github.com/OWNER/REPOSITORY.git workspace/repositories/my-repository
cd workspace/repositories/my-repository
git status
```

The harness clones the committed repository state into `workspace/runs/<run-id>/repo` for each run. Commit any local changes you want included before starting the run. The original repository remains untouched.

### 3. Start FastAPI

In a terminal from the project root:

```bash
cd backend
source .venv/bin/activate
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

API docs: <http://127.0.0.1:8000/docs>  
Health check: <http://127.0.0.1:8000/api/health>

### 4. Start React

In another terminal from the project root:

```bash
cd frontend
npm install
npm run dev
```

Open <http://127.0.0.1:5173>.

### 5. Run your first task

1. Open **Repositories** and confirm the Git repository is listed.
2. Open **Run evaluation**.
3. Describe one specific bug or change, including expected behavior.
4. Leave the test command on **Auto-detect** or choose an allowlisted runner.
5. Start the run and inspect the real agent/tool timeline.
6. Inspect the diff and test output before accepting any changes.

## API endpoints

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/api/health` | API/provider configuration status |
| GET | `/api/repositories` | List Git repos under allowed root |
| POST | `/api/repositories/register` | Register a repo path inside allowed root |
| POST | `/api/runs` | Queue an agent run |
| GET | `/api/runs` | List recorded runs |
| GET | `/api/runs/{id}` | Get run details and metrics |
| GET | `/api/runs/{id}/events` | Retrieve structured events |
| GET | `/api/runs/{id}/stream` | Server-Sent Events stream |
| POST | `/api/runs/{id}/cancel` | Request cancellation |
| GET | `/api/metrics` | Aggregate metrics from SQLite |
| GET | `/api/benchmarks/datasets` | List selectable JSONL benchmark datasets |
| GET | `/api/benchmarks/tasks?dataset=tasks.jsonl` | Preview benchmark task definitions |
| POST | `/api/benchmarks` | Start a repeatable benchmark suite |
| GET | `/api/benchmarks/{id}` | Benchmark progress, per-sample results, and summary |
| POST | `/api/benchmarks/{id}/cancel` | Cancel a running benchmark suite |
| GET | `/api/settings` | Public runtime settings (no secrets) |

## Metrics and success criteria

The dashboard calculates its metrics from recorded runs. It does not prepopulate sample numbers.

- **Task verification success:** a configured test command was actually executed and the latest test execution passed, and the independent code reviewer explicitly approved the diff.
- **Success rate:** successful completed runs divided by completed runs.
- **Wall-clock duration:** time from orchestration start to final status, including provider calls and local tool execution.
- **Token usage / LLM calls:** usage reported by the provider API.
- **Prompt/completion tokens:** provider-reported input and output token counts.
- **Tool calls / handoffs / changed files:** execution events and workspace inspection.

This is a useful operational signal, not a mathematical guarantee of correctness. A passing test suite can be incomplete; always inspect the diff and reviewer findings. For CV claims, report the task set size and test criteria alongside the measured result.

## Overhead choices

1. **Compact handoffs and context control:** agents receive relevant reports and task-specific context, not a full transcript of every agent.
2. **Structured agent communication:** roles have explicit instructions and tools; output is recorded in a trace.
3. **Adaptive routing:** the coordinator chooses whether the security reviewer is needed; read-only specialists run concurrently.
4. **Inference efficiency:** one shared client/model, bounded concurrency, configurable step limits, and per-call token/latency accounting. Provider-specific caching and reasoning settings should only be enabled when documented and supported by the selected API.

## Safety and limitations

- Only use repositories and dependencies you trust. Test commands execute on your machine and **are not a hardened OS/container sandbox**. The disposable clone protects the original working tree, but malicious project scripts can still access host resources available to your user account.
- Test commands are selected from an explicit allowlist. Do not add arbitrary shell execution without proper isolation.
- Repository paths must remain under `AGENTHARNESS_REPOS_ROOT`.
- The agent's file-writing tool is constrained to the disposable repository and rejects `.env` and `.git` paths.
- Never expose the API key in frontend code, browser storage, screenshots, or Git.
- The current run queue is in-process. If the API server stops, an active run will stop; use a durable job queue if you later need long-running production service.
- Model/provider API availability, quotas, token reporting, and concurrency policies can change. Confirm the Ollama Cloud model is enabled for your account.
- Automatic test discovery is intentionally conservative. Review the selected command before running unfamiliar repositories.

## Development checks

Backend tests:

```bash
cd backend
source .venv/bin/activate
python -m unittest discover -s tests -v
```

Frontend production build:

```bash
cd frontend
npm install
npm run build
```

## Repository layout

```text
agentharness-lab/
├── backend/
│   ├── app/              # FastAPI, agents, LLM adapter, repository tools, runner, SQLite
│   ├── tests/            # Unit tests for parsing and tool safety
│   ├── requirements.txt
│   └── .env.example
├── frontend/
│   └── src/              # React UI, API client, pages/components, styles
├── docs/
│   └── EVALUATION.md
└── workspace/
    └── repositories/     # Place/clone your real Git repositories here
```

### Benchmark suite (included and runnable)

The **Benchmark suite** page runs the bundled four-task bug-fix smoke suite against a committed, intentionally buggy local fixture repository. It is designed to validate the complete evaluation pipeline end-to-end; it is not a claim about performance on arbitrary GitHub repositories. Each sample runs from the same clean committed baseline in a disposable clone, and the UI shows actual tests, reviewer gates, duration, tokens, and LLM calls.

The default tasks cover parity correctness, email normalization, zero-division error handling, and stable de-duplication. They each have a focused test command. The suite runs sequentially by default to avoid provider contention and can repeat tasks up to five times. Results are stored under `workspace/benchmarks/<benchmark-id>/benchmark.json` and are based on real runs; no score is prefilled.

To run the backend unit tests:

```bash
cd backend
source .venv/bin/activate
python -m unittest discover -s tests -v
```

To validate the default manifest without calling the model:

```bash
cd backend
source .venv/bin/activate
python -m evaluations.run_benchmark --validate-only
```

To execute it against the running API and write JSON, CSV, and Markdown reports:

```bash
python -m evaluations.run_benchmark --repetitions 1 --output-dir workspace/benchmarks-cli
```

The CLI keeps failed and timed-out samples in the denominator. It exits with status 0 only if all attempted samples succeed; a nonzero exit is a valid benchmark result, not necessarily a runner crash.

#### Evaluate real GitHub repositories

1. Clone and commit the target repositories under `workspace/repositories/`.
2. Copy `docs/real_repository_tasks.example.jsonl` to `backend/evaluations/datasets/<your-name>.jsonl`.
3. Replace the repository name and write one specific task per JSONL line. Select an allowlisted test command; use `auto` only if the repository has a recognized test runner.
4. Open **Benchmark suite**, select your dataset, and start the run. Or pass the manifest path to the CLI with `--dataset`.
5. Keep the task list, repository commit hashes, and test criteria with any CV metrics you report.

The local fixture provides deterministic pipeline coverage. For credible portfolio metrics on GitHub projects, use multiple real repositories and task prompts with a committed starting state and known regression tests.

### Inspect AI evaluation adapter (included)

The included Inspect AI task file drives the **same AgentHarness API** using the bundled JSONL task set and scores only runs that pass the configured test command and reviewer gate. It does not run a second LLM benchmark or compare model families.

1. Keep the FastAPI backend running.
2. Use `backend/evaluations/tasks.jsonl` for the bundled fixture suite or provide a custom JSONL dataset with real repository names already cloned under `workspace/repositories/`.
3. In a terminal from the `backend/` directory, activate the virtual environment and run:

```bash
inspect eval evaluations/agentharness_eval.py
```

To point to a different task file:

```bash
inspect eval evaluations/agentharness_eval.py -T dataset_path=evaluations/datasets/my_tasks.jsonl
```

Set `AGENTHARNESS_API_URL` if the API is running somewhere other than `http://127.0.0.1:8000`. Inspect writes evaluation logs that can be reviewed with `inspect view`. Each sample triggers a real AgentHarness run and may incur provider usage costs; use a small task set first.
