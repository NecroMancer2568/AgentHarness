# AgentHarness Lab: benchmark and evaluation

This is an engineering validation workflow. It measures whether the application works on a documented task set; it is not a model leaderboard or a claim of novel research.

## What's included

- **Interactive Benchmark Suite page:** choose a JSONL dataset, repeat each task up to five times, start/cancel runs, view per-sample outcomes, and inspect each underlying execution.
- **Bundled smoke benchmark:** four seeded Python bug-fix tasks with focused tests. The source fixture intentionally contains defects; the harness should fix them in disposable clones. It is for validating the full pipeline and must not be described as a real GitHub benchmark.
- **CLI runner:** launches the same AgentHarness API, enforces per-sample timeouts, and writes JSON, CSV, and Markdown reports.
- **Inspect AI adapter:** runs the same tasks through the same API and scores only verified outcomes.
- **Automated tests:** task manifest validation, shell-injection rejection, metric aggregation, path-boundary checks, and a deterministic fake-LLM end-to-end benchmark test. The fake-LLM test validates wiring only and is not used as model-performance data.

## Start the API and UI

Follow the root README setup instructions, configure `OLLAMA_API_KEY`, and start FastAPI and Vite. The API initializes a local Git baseline for the bundled fixture if the nested `.git` directory is absent.

Open **Benchmark suite** in the UI. Select `tasks.jsonl`, choose repetitions, and start the benchmark. The suite is sequential to avoid unnecessary provider contention. Each sample gets a fresh clone from the committed fixture baseline.

## CLI usage

Run from the `backend/` directory with the backend virtual environment activated.

Validate the dataset without making model calls:

```bash
python -m evaluations.run_benchmark --validate-only
```

Run the suite against the live API:

```bash
python -m evaluations.run_benchmark --repetitions 1 --output-dir workspace/benchmarks-cli
```

The output directory is resolved relative to the project root. The report includes `results.json`, `summary.json`, `results.csv`, and `report.md`. Errors, failures, and timeouts remain in the denominator. The CLI exits with status 0 only if all attempted samples succeed.

Useful options:

- `--dataset PATH`: alternate JSONL manifest.
- `--api-url URL` or `AGENTHARNESS_API_URL`: API base URL.
- `--repetitions 1..5`: repeat every task.
- `--sample-timeout SECONDS`: per-sample timeout.
- `--validate-only`: validate the manifest without running tasks.
- `--fail-fast`: stop after the first API/sample error.

## Add real GitHub repository tasks

1. Clone the target GitHub repositories under `workspace/repositories/` and commit the exact baseline to evaluate.
2. Copy `docs/real_repository_tasks.example.jsonl` into `backend/evaluations/datasets/your-tasks.jsonl`.
3. Replace the repository name with the cloned directory name. Add a separate JSONL row for each task, with a specific prompt and a verifiable expected behavior.
4. Use `auto`, a standard allowlisted runner (`pytest`, `npm test`, `cargo test`, `go test ./...`, `mvn test`, or `gradle test`), or a scoped safe pytest path such as `pytest -q tests/test_specific.py`.
5. Refresh the Benchmark Suite page, select the new dataset, and run it. Or pass the manifest path to the CLI with `--dataset`.
6. Save the dataset, baseline commit hashes, run reports, and success criteria alongside any CV claims.

Task manifests are validated before a run. Dataset names selected by the API must be `.jsonl` basenames inside `backend/evaluations/datasets/`; arbitrary filesystem paths are rejected. The CLI can read an explicitly supplied path because it is a local developer command.

## Scoring rule

A sample is **verified successful** only when all of these are true:

1. AgentHarness reaches the `succeeded` terminal state.
2. The configured test command was actually executed and the latest test execution passed.
3. The independent code reviewer returns `VERDICT: APPROVE`.

This is a practical gate, not a proof of correctness. Test coverage may be incomplete and reviewer judgments can be wrong. Inspect the diff and test output before accepting changes.

## Reported metrics

- attempted, successful, and failed samples; verified success rate;
- test-pass and reviewer-approval counts;
- average, median, and p95 wall-clock duration;
- total and average provider-reported tokens;
- total and average LLM calls;
- per-task breakdowns and raw sample-level outcomes.

The API/UI also expose prompt/completion tokens, tool calls, handoffs, agent timings, and changed-file counts where available. Metrics are computed from recorded runs; empty result sets display no success rate rather than a fabricated zero or 100%.

## Automated tests

```bash
cd backend
source .venv/bin/activate
python -m unittest discover -s tests -v
```

The benchmark execution integration test uses a deterministic fake LLM to validate the orchestration, repository cloning, tool execution, test command, review gate, and metric persistence without paid cloud calls. Its synthetic values are test-only and are never inserted into dashboard history or reports.

## Safety and limits

Repository test commands run on the host. Disposable Git clones protect the original working tree, but they are **not** a hardened OS/container sandbox; malicious test scripts may access host resources available to your user account. Only benchmark repositories you trust. Keep API keys in `backend/.env`, never in frontend code or benchmark datasets.
