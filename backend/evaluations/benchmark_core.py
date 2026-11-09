"""Dataset validation and metric aggregation shared by CLI and API evaluation."""
from __future__ import annotations

import json
import math
import shlex
from pathlib import Path
from statistics import mean, median
from typing import Any

ALLOWED_TEST_COMMANDS = {
    "auto", "pytest", "python -m pytest",
    "pytest -q tests/test_is_even.py",
    "pytest -q tests/test_normalize_email.py",
    "pytest -q tests/test_safe_divide.py",
    "pytest -q tests/test_unique_preserving_order.py",
    "npm test", "npm run test", "cargo test", "go test ./...", "mvn test", "gradle test",
}


def is_allowed_manifest_test_command(command: str) -> bool:
    if command in ALLOWED_TEST_COMMANDS:
        return True
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    if len(tokens) == 3 and tokens[:2] == ["pytest", "-q"]:
        target = tokens[2]
    elif len(tokens) == 5 and tokens[:4] == ["python", "-m", "pytest", "-q"]:
        target = tokens[4]
    else:
        return False
    path = Path(target)
    return (not path.is_absolute() and ".." not in path.parts and path.suffix == ".py"
            and bool(path.parts) and all(part not in {"", "."} for part in path.parts)
            and not any(char in target for char in ";&|`$<>\n\r"))


def load_dataset(path: str | Path) -> list[dict[str, Any]]:
    dataset_path = Path(path).expanduser().resolve()
    if not dataset_path.is_file():
        raise FileNotFoundError(f"Benchmark dataset not found: {dataset_path}")
    tasks: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for line_number, raw in enumerate(dataset_path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        try:
            item = json.loads(raw)
            if not isinstance(item, dict):
                raise ValueError("each JSONL row must be an object")
            task_id = str(item.get("id", "")).strip()
            repository = str(item.get("repository", "")).strip()
            prompt = str(item.get("input") or item.get("task") or "").strip()
            test_command = str(item.get("test_command", "auto")).strip()
            if not task_id or not repository or len(prompt) < 10:
                raise ValueError("requires non-empty id, repository, and input/task of at least 10 characters")
            if task_id in seen_ids:
                raise ValueError(f"duplicate task id: {task_id}")
            if not is_allowed_manifest_test_command(test_command):
                raise ValueError(f"unsupported test_command {test_command!r}; choose one of {sorted(ALLOWED_TEST_COMMANDS)} or a scoped pytest command such as 'pytest -q tests/test_example.py'")
            steps = int(item.get("max_agent_steps", 8))
            if not 1 <= steps <= 20:
                raise ValueError("max_agent_steps must be between 1 and 20")
            item.update({"id": task_id, "repository": repository, "input": prompt,
                         "test_command": test_command, "max_agent_steps": steps,
                         "category": str(item.get("category", "general")),
                         "expected_behavior": str(item.get("expected_behavior", ""))})
            seen_ids.add(task_id)
            tasks.append(item)
        except Exception as exc:
            raise ValueError(f"Invalid benchmark dataset line {line_number}: {exc}") from exc
    if not tasks:
        raise ValueError(f"Benchmark dataset has no tasks: {dataset_path}")
    return tasks


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(p * len(ordered)) - 1))
    return ordered[index]


def summarize_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute transparent summaries from completed sample records only."""
    attempted = len(results)
    successful = sum(bool(row.get("success")) for row in results)
    durations = [float(row["duration_ms"]) for row in results if row.get("duration_ms") is not None]
    tokens = [int(row.get("total_tokens") or 0) for row in results]
    calls = [int(row.get("llm_calls") or 0) for row in results]
    test_passed = sum(bool(row.get("test_passed")) for row in results)
    reviewer_approved = sum(bool(row.get("review_approved")) for row in results)
    return {
        "attempted_samples": attempted,
        "successful_samples": successful,
        "failed_samples": attempted - successful,
        "success_rate_percent": round(successful / attempted * 100, 2) if attempted else None,
        "test_pass_count": test_passed,
        "review_approval_count": reviewer_approved,
        "average_duration_ms": round(mean(durations), 2) if durations else None,
        "median_duration_ms": round(median(durations), 2) if durations else None,
        "p95_duration_ms": round(percentile(durations, 0.95), 2) if durations else None,
        "average_tokens_per_sample": round(mean(tokens), 2) if tokens else None,
        "total_tokens": sum(tokens),
        "average_llm_calls_per_sample": round(mean(calls), 2) if calls else None,
        "total_llm_calls": sum(calls),
    }
