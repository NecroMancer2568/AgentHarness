"""Run AgentHarness against a JSONL task set and write machine-readable reports.

The API must be running. Tasks must reference repositories already present under the
configured repository root. No benchmark scores are pre-filled or simulated.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from evaluations.benchmark_core import load_dataset, summarize_results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate AgentHarness on a real repository task set")
    parser.add_argument("--dataset", default=str(Path(__file__).with_name("tasks.jsonl")), help="JSONL task manifest")
    parser.add_argument("--api-url", default=os.getenv("AGENTHARNESS_API_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--output-dir", default="workspace/benchmarks-cli", help="Directory for JSON/CSV/Markdown reports")
    parser.add_argument("--repetitions", type=int, default=1, help="Repeat every task 1-5 times")
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--sample-timeout", type=float, default=1800.0, help="Maximum seconds per sample")
    parser.add_argument("--validate-only", action="store_true", help="Validate manifest without launching runs")
    parser.add_argument("--fail-fast", action="store_true", help="Stop after the first API/sample error")
    args = parser.parse_args()
    if not 1 <= args.repetitions <= 5:
        parser.error("--repetitions must be between 1 and 5")
    if args.poll_interval <= 0 or args.sample_timeout <= 0:
        parser.error("poll interval and sample timeout must be positive")
    return args


async def wait_for_run(client: httpx.AsyncClient, base: str, run_id: str, poll_interval: float, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = await client.get(f"{base}/api/runs/{run_id}")
        response.raise_for_status()
        run = response.json()
        if run.get("status") in {"succeeded", "failed", "cancelled"}:
            return run
        await asyncio.sleep(poll_interval)
    try:
        await client.post(f"{base}/api/runs/{run_id}/cancel")
    except Exception:
        pass
    raise TimeoutError(f"Run {run_id} exceeded the {timeout:g}s sample timeout")


async def execute(args: argparse.Namespace, task_defs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    base = args.api_url.rstrip("/")
    timeout = httpx.Timeout(connect=10.0, read=max(args.sample_timeout, 30.0), write=30.0, pool=10.0)
    results: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=timeout) as client:
        health = await client.get(f"{base}/api/health")
        health.raise_for_status()
        print(f"API ready · model={health.json().get('model')} · tasks={len(task_defs)} · repetitions={args.repetitions}")
        for repetition in range(1, args.repetitions + 1):
            for index, item in enumerate(task_defs, 1):
                print(f"[{len(results)+1}/{len(task_defs)*args.repetitions}] {item['id']} · repetition {repetition}")
                row: dict[str, Any] = {
                    "task_id": item["id"], "category": item.get("category", "general"),
                    "repository": item["repository"], "repetition": repetition, "run_id": None,
                    "status": "error", "success": False, "duration_ms": None, "total_tokens": 0,
                    "prompt_tokens": 0, "completion_tokens": 0, "llm_calls": 0,
                    "test_passed": False, "review_approved": False, "error": None,
                }
                try:
                    created = await client.post(f"{base}/api/runs", json={
                        "repository": item["repository"], "task": item["input"],
                        "test_command": item["test_command"], "max_agent_steps": item["max_agent_steps"],
                    })
                    created.raise_for_status()
                    run_id = created.json()["id"]
                    row["run_id"] = run_id
                    run = await wait_for_run(client, base, run_id, args.poll_interval, args.sample_timeout)
                    metrics = run.get("metrics") or {}
                    detail = run.get("result") or {}
                    dep_prep = metrics.get("dependency_preparation") or detail.get("dependency_preparation") or {}
                    dep_status = metrics.get("dependency_status") or dep_prep.get("status")
                    row.update({
                        "status": run.get("status", "failed"), "success": bool(run.get("success")),
                        "duration_ms": run.get("duration_ms"), "total_tokens": run.get("total_tokens", 0),
                        "prompt_tokens": run.get("prompt_tokens", 0), "completion_tokens": run.get("completion_tokens", 0),
                        "llm_calls": run.get("llm_calls", 0),
                        "test_passed": bool(metrics.get("test_passed", detail.get("test_passed", False))),
                        "review_approved": bool(metrics.get("review_approved", detail.get("review_approved", False))),
                        "verification_state": metrics.get("verification_state") or detail.get("verification_state"),
                        "dependency_status": dep_status,
                        "dependency_preparation": dep_prep,
                        "error": run.get("error"),
                    })
                    print(f"  -> {row['status']} · verified={row['success']} · {row['duration_ms']} ms · {row['total_tokens']} tokens")
                except Exception as exc:
                    row["error"] = f"{type(exc).__name__}: {exc}"
                    print(f"  -> ERROR: {row['error']}", file=sys.stderr)
                    if args.fail_fast:
                        results.append(row)
                        return results
                results.append(row)
    return results


def write_reports(output_dir: Path, task_defs: list[dict[str, Any]], results: list[dict[str, Any]], model: str | None = None) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    summary = summarize_results(results)
    summary.update({
        "benchmark_name": "AgentHarness Lab repository bug-fix benchmark",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "dataset_task_count": len(task_defs),
        "repetitions": max((int(row.get("repetition", 1)) for row in results), default=1),
        "results_file": "results.json",
        "scoring_rule": "A sample succeeds only when AgentHarness reports succeeded after its configured test command passed and the code reviewer approved the diff.",
    })
    per_task: list[dict[str, Any]] = []
    for task in task_defs:
        matching = [row for row in results if row.get("task_id") == task["id"]]
        task_summary = summarize_results(matching)
        per_task.append({"task_id": task["id"], "category": task.get("category", "general"),
                         "repository": task["repository"], "summary": task_summary})
    summary["per_task"] = per_task
    (output_dir / "results.json").write_text(json.dumps({"summary": summary, "results": results}, indent=2, ensure_ascii=False), encoding="utf-8")
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    evaluation_data = {
        "summary": summary,
        "dependency_preparation": {
            "status_counts": {
                status: sum(1 for r in results if r.get("dependency_status") == status)
                for status in {"DEPENDENCIES_READY", "DEPENDENCY_INSTALL_FAILED", "MISSING_DEPENDENCY", "DEPENDENCY_INSTALL_BLOCKED"}
                if sum(1 for r in results if r.get("dependency_status") == status) > 0
            },
            "per_task_status": [
                {
                    "task_id": r.get("task_id"),
                    "repository": r.get("repository"),
                    "dependency_status": r.get("dependency_status"),
                    "dependency_preparation": r.get("dependency_preparation"),
                }
                for r in results
            ],
        },
        "test_results": {
            "passed_count": summary.get("test_passed_samples", 0),
            "verification_states": {
                state: sum(1 for r in results if r.get("verification_state") == state)
                for state in {"PASS", "FAIL", "NO_TESTS", "BLOCKED", "MISSING_DEPENDENCY", "DEPENDENCY_INSTALL_FAILED", "DEPENDENCY_INSTALL_BLOCKED", "REVIEW_REJECTED", "REVIEW_PENDING"}
                if sum(1 for r in results if r.get("verification_state") == state) > 0
            },
        },
        "results": results,
    }
    (output_dir / "evaluation.json").write_text(json.dumps(evaluation_data, indent=2, ensure_ascii=False), encoding="utf-8")

    columns = ["task_id", "category", "repository", "repetition", "run_id", "status", "success", "verification_state", "dependency_status", "test_passed", "review_approved", "duration_ms", "total_tokens", "prompt_tokens", "completion_tokens", "llm_calls", "error"]
    with (output_dir / "results.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)
    lines = [
        "# AgentHarness Lab Benchmark Report", "",
        f"- Created: {summary['created_at']}", f"- Model: {model or 'not reported'}",
        f"- Tasks defined: {len(task_defs)}", f"- Samples attempted: {summary['attempted_samples']}",
        f"- Verified successes: {summary['successful_samples']}", f"- Success rate: {summary['success_rate_percent']}%" if summary['success_rate_percent'] is not None else "- Success rate: not available",
        f"- Average duration: {summary['average_duration_ms']} ms", f"- Median duration: {summary['median_duration_ms']} ms",
        f"- p95 duration: {summary['p95_duration_ms']} ms", f"- Total tokens: {summary['total_tokens']}",
        f"- Total LLM calls: {summary['total_llm_calls']}", "",
        "## Per-task results", "", "| Task | Category | Attempts | Successes | Success rate | Avg duration (ms) | Avg tokens |", "|---|---|---:|---:|---:|---:|---:|",
    ]
    for item in per_task:
        stat = item["summary"]
        lines.append(f"| {item['task_id']} | {item['category']} | {stat['attempted_samples']} | {stat['successful_samples']} | {stat['success_rate_percent']}% | {stat['average_duration_ms']} | {stat['average_tokens_per_sample']} |")
    lines += ["", "## Interpretation", "", "These values are generated from actual API runs recorded in results.json/results.csv. A failed or timed-out sample remains in the denominator. A passing result is an operational verification signal, not a guarantee that all possible defects are absent.", ""]
    (output_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")
    return summary


async def main_async(args: argparse.Namespace) -> int:
    task_defs = load_dataset(args.dataset)
    print(f"Validated {len(task_defs)} tasks from {Path(args.dataset).resolve()}")
    if args.validate_only:
        for item in task_defs:
            print(f"- {item['id']} · repository={item['repository']} · test={item['test_command']}")
        return 0
    results: list[dict[str, Any]] = []
    model = None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(f"{args.api_url.rstrip('/')}/api/health")
            response.raise_for_status()
            model = response.json().get("model")
        results = await execute(args, task_defs)
    except Exception as exc:
        print(f"Benchmark execution error: {type(exc).__name__}: {exc}", file=sys.stderr)
        if args.fail_fast and not results:
            return 2
    output = Path(args.output_dir).expanduser()
    if not output.is_absolute():
        output = Path(__file__).resolve().parents[2] / output
    output = output.resolve()
    summary = write_reports(output, task_defs, results, model)
    print(f"Reports written to {output}")
    print(f"Verified success rate: {summary['success_rate_percent']}% ({summary['successful_samples']}/{summary['attempted_samples']})")
    return 0 if results and summary["failed_samples"] == 0 else 1


def main() -> None:
    args = parse_args()
    raise SystemExit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
