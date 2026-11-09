"""Local benchmark orchestration using the same production run manager as the UI."""
from __future__ import annotations

import asyncio
import json
import re
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evaluations.benchmark_core import load_dataset, summarize_results
from .config import Settings
from .db import Database, utcnow
from .repository import RepositoryManager, WorkspaceError
from .runner import RunManager


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


class BenchmarkManager:
    def __init__(self, settings: Settings, db: Database, run_manager: RunManager, repos: RepositoryManager):
        self.settings = settings
        self.db = db
        self.run_manager = run_manager
        self.repos = repos
        self.evaluations_root = Path(__file__).resolve().parents[1] / "evaluations"
        self.dataset_path = self.evaluations_root / "tasks.jsonl"
        self.datasets_root = self.evaluations_root / "datasets"
        self.datasets_root.mkdir(parents=True, exist_ok=True)
        self.root = (settings.runs_root.parent / "benchmarks").resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.tasks: dict[str, asyncio.Task] = {}
        self._ensure_bundled_fixture_repository()

    def _ensure_bundled_fixture_repository(self) -> None:
        """Create a local Git baseline for the bundled fixture if its nested .git was not packaged."""
        fixture = (self.settings.repos_root / "agentharness-benchmark-fixture").resolve()
        if fixture.parent != self.settings.repos_root.resolve() or not fixture.is_dir() or (fixture / ".git").exists():
            return
        commands = [
            ["git", "init", "-q"],
            ["git", "config", "user.name", "AgentHarness Benchmark"],
            ["git", "config", "user.email", "benchmark@example.invalid"],
            ["git", "add", "README.md", "src", "tests"],
            ["git", "commit", "-q", "-m", "Add intentionally buggy benchmark baseline"],
        ]
        for command in commands:
            try:
                subprocess.run(command, cwd=fixture, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
            except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                raise RuntimeError(f"Could not initialize the bundled benchmark Git fixture: {exc}") from exc

    def resolve_dataset(self, dataset_name: str) -> Path:
        # Dataset names are basenames only; arbitrary filesystem paths are never accepted.
        if Path(dataset_name).name != dataset_name or not dataset_name.endswith(".jsonl"):
            raise ValueError("Dataset must be a .jsonl filename, not a filesystem path")
        candidate = self.dataset_path if dataset_name == "tasks.jsonl" else (self.datasets_root / dataset_name).resolve()
        allowed_parent = self.evaluations_root.resolve() if dataset_name == "tasks.jsonl" else self.datasets_root.resolve()
        if candidate.parent.resolve() != allowed_parent:
            raise ValueError("Dataset path is outside the allowed evaluation datasets directory")
        if not candidate.is_file() or dataset_name.endswith(".example.jsonl"):
            raise FileNotFoundError(f"Benchmark dataset not found: {dataset_name}")
        return candidate

    def list_datasets(self) -> list[dict[str, Any]]:
        paths = [self.dataset_path] + sorted(self.datasets_root.glob("*.jsonl"))
        result = []
        for path in paths:
            if path.name.endswith(".example.jsonl"):
                continue
            try:
                tasks = load_dataset(path)
                result.append({"name": path.name, "task_count": len(tasks), "is_default": path == self.dataset_path})
            except Exception as exc:
                result.append({"name": path.name, "task_count": None, "is_default": path == self.dataset_path, "error": str(exc)})
        return result

    def task_catalog(self, dataset_name: str = "tasks.jsonl") -> list[dict[str, Any]]:
        tasks = load_dataset(self.resolve_dataset(dataset_name))
        return [{key: task.get(key) for key in ("id", "repository", "category", "input", "test_command", "max_agent_steps", "expected_behavior")} for task in tasks]

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        found = []
        for path in self.root.glob("*/benchmark.json"):
            item = _read_json(path)
            if item:
                item.pop("results", None)
                found.append(item)
        return sorted(found, key=lambda x: x.get("created_at", ""), reverse=True)[:max(1, min(limit, 100))]

    def get(self, benchmark_id: str) -> dict[str, Any] | None:
        if not re.fullmatch(r"[a-f0-9]{12}", benchmark_id):
            return None
        return _read_json(self.root / benchmark_id / "benchmark.json")

    def start(self, repetitions: int = 1, dataset_name: str = "tasks.jsonl") -> dict[str, Any]:
        if not 1 <= repetitions <= 5:
            raise ValueError("repetitions must be between 1 and 5")
        selected_dataset = self.resolve_dataset(dataset_name)
        task_defs = load_dataset(selected_dataset)
        # Preflight every repository before accepting a benchmark; avoid partial runs caused by typos.
        for item in task_defs:
            self.repos.resolve_repository(item["repository"])
        benchmark_id = uuid.uuid4().hex[:12]
        directory = self.root / benchmark_id
        directory.mkdir(parents=True, exist_ok=False)
        record: dict[str, Any] = {
            "id": benchmark_id, "status": "queued", "created_at": utcnow(), "updated_at": utcnow(),
            "model": self.settings.ollama_model, "dataset": str(selected_dataset), "dataset_name": dataset_name,
            "task_count": len(task_defs), "repetitions": repetitions,
            "planned_samples": len(task_defs) * repetitions, "completed_samples": 0,
            "results": [], "summary": None, "error": None,
        }
        self._save(record)
        task = asyncio.create_task(self._execute(benchmark_id, task_defs, repetitions), name=f"benchmark-{benchmark_id}")
        self.tasks[benchmark_id] = task
        return record

    def _save(self, record: dict[str, Any]) -> None:
        record["updated_at"] = utcnow()
        path = self.root / record["id"] / "benchmark.json"
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
        temp.replace(path)

    async def _execute(self, benchmark_id: str, task_defs: list[dict[str, Any]], repetitions: int) -> None:
        record = self.get(benchmark_id)
        if not record:
            return
        record["status"] = "running"
        self._save(record)
        try:
            for repetition in range(1, repetitions + 1):
                for definition in task_defs:
                    task_run_id = uuid.uuid4().hex[:12]
                    self.db.create_run(task_run_id, definition["repository"], definition["input"], definition["test_command"], definition["max_agent_steps"])
                    self.db.add_event(task_run_id, "benchmark_sample_started", "benchmark", "Benchmark sample started", {
                        "benchmark_id": benchmark_id, "task_id": definition["id"], "repetition": repetition,
                    })
                    sample_task = asyncio.create_task(self.run_manager.execute(task_run_id), name=f"benchmark-sample-{task_run_id}")
                    self.run_manager.tasks[task_run_id] = sample_task
                    try:
                        await asyncio.wait_for(sample_task, timeout=self.settings.agentharness_benchmark_sample_timeout_seconds)
                    except asyncio.TimeoutError:
                        self.db.update_run(task_run_id, status="failed", finished_at=utcnow(), error="Benchmark sample timed out.")
                        self.db.add_event(task_run_id, "benchmark_sample_timeout", "benchmark", "Benchmark sample exceeded its timeout", {
                            "benchmark_id": benchmark_id, "timeout_seconds": self.settings.agentharness_benchmark_sample_timeout_seconds,
                        })
                    row = self.db.get_run(task_run_id) or {}
                    try:
                        metrics = json.loads(row.get("metrics_json") or "{}")
                        details = json.loads(row.get("result_json") or "{}")
                    except json.JSONDecodeError:
                        metrics, details = {}, {}
                    result = {
                        "task_id": definition["id"], "category": definition.get("category", "general"),
                        "repository": definition["repository"], "repetition": repetition, "run_id": task_run_id,
                        "status": row.get("status", "failed"), "success": bool(row.get("success")),
                        "duration_ms": row.get("duration_ms"), "total_tokens": row.get("total_tokens", 0),
                        "prompt_tokens": row.get("prompt_tokens", 0), "completion_tokens": row.get("completion_tokens", 0),
                        "llm_calls": row.get("llm_calls", 0), "test_passed": bool(metrics.get("test_passed", details.get("test_passed", False))),
                        "review_approved": bool(metrics.get("review_approved", details.get("review_approved", False))),
                        "error": row.get("error"),
                    }
                    record = self.get(benchmark_id) or record
                    record["results"].append(result)
                    record["completed_samples"] = len(record["results"])
                    record["summary"] = summarize_results(record["results"])
                    self._save(record)
            record = self.get(benchmark_id) or record
            record["status"] = "completed"
            record["summary"] = summarize_results(record["results"])
            self._save(record)
        except asyncio.CancelledError:
            record = self.get(benchmark_id) or record
            record["status"] = "cancelled"
            record["summary"] = summarize_results(record["results"])
            self._save(record)
            raise
        except Exception as exc:
            record = self.get(benchmark_id) or record
            record["status"] = "failed"
            record["error"] = f"{type(exc).__name__}: {exc}"
            record["summary"] = summarize_results(record["results"])
            self._save(record)

    def cancel(self, benchmark_id: str) -> bool:
        task = self.tasks.get(benchmark_id)
        if task and not task.done():
            task.cancel()
            # Persist immediately as cancellation can arrive before the coroutine's first step.
            record = self.get(benchmark_id)
            if record:
                record["status"] = "cancelled"
                record["summary"] = summarize_results(record.get("results", []))
                self._save(record)
            return True
        return False
