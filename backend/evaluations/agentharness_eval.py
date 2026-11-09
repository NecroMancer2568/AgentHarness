"""Inspect AI adapter for actual AgentHarness API runs on a JSONL task set."""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx
from inspect_ai import Task, task
from inspect_ai.dataset import Sample
from inspect_ai.model import ModelOutput
from inspect_ai.scorer import CORRECT, INCORRECT, Score, Target, accuracy, scorer, stderr
from inspect_ai.solver import Generate, TaskState, solver

from evaluations.benchmark_core import load_dataset

API_BASE = os.getenv("AGENTHARNESS_API_URL", "http://127.0.0.1:8000").rstrip("/")
DEFAULT_DATASET = Path(__file__).with_name("tasks.jsonl")
SAMPLE_TIMEOUT_SECONDS = float(os.getenv("AGENTHARNESS_SAMPLE_TIMEOUT_SECONDS", "1800"))


def load_samples(path: Path) -> list[Sample]:
    items = load_dataset(path)
    return [Sample(
        input=item["input"], target=item.get("target", "verified_success"), id=item["id"],
        metadata={key: item.get(key) for key in ("repository", "test_command", "max_agent_steps", "category", "expected_behavior")},
    ) for item in items]


@solver
def run_agentharness() -> Any:
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        if state.metadata is None:
            state.metadata = {}
        metadata = state.metadata
        repository = metadata.get("repository")
        if not repository:
            raise ValueError("Sample metadata must contain a repository name.")
        payload = {
            "repository": repository, "task": state.input_text,
            "test_command": metadata.get("test_command", "auto"),
            "max_agent_steps": int(metadata.get("max_agent_steps", 8)),
        }
        timeout = httpx.Timeout(30.0, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            created = await client.post(f"{API_BASE}/api/runs", json=payload)
            created.raise_for_status()
            run_id = created.json()["id"]
            state.metadata["agentharness_run_id"] = run_id
            deadline = time.monotonic() + SAMPLE_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                await asyncio.sleep(2)
                response = await client.get(f"{API_BASE}/api/runs/{run_id}")
                response.raise_for_status()
                run = response.json()
                if run.get("status") in {"succeeded", "failed", "cancelled"}:
                    break
            else:
                try:
                    await client.post(f"{API_BASE}/api/runs/{run_id}/cancel")
                finally:
                    summary = {"run_id": run_id, "status": "timeout", "success": False,
                               "duration_ms": None, "total_tokens": 0, "llm_calls": 0,
                               "error": f"Sample exceeded {SAMPLE_TIMEOUT_SECONDS:g}s timeout"}
                    state.metadata["agentharness_result"] = summary
                    state.output = ModelOutput.from_content(model="agentharness-lab", content=json.dumps(summary))
                    return state
            metrics = run.get("metrics") or {}
            details = run.get("result") or {}
            summary = {
                "run_id": run_id, "status": run.get("status"), "success": bool(run.get("success")),
                "duration_ms": run.get("duration_ms"), "total_tokens": run.get("total_tokens", 0),
                "prompt_tokens": run.get("prompt_tokens", 0), "completion_tokens": run.get("completion_tokens", 0),
                "llm_calls": run.get("llm_calls", 0), "test_passed": bool(metrics.get("test_passed", details.get("test_passed", False))),
                "review_approved": bool(metrics.get("review_approved", details.get("review_approved", False))),
                "error": run.get("error"),
            }
            state.metadata["agentharness_result"] = summary
            state.output = ModelOutput.from_content(model="agentharness-lab", content=json.dumps(summary))
            return state
    return solve


@scorer(metrics=[accuracy(), stderr()])
def verified_run_score():
    async def score(state: TaskState, target: Target) -> Score:
        try:
            result = json.loads(state.output.completion)
            success = bool(result.get("success")) and result.get("status") == "succeeded" and bool(result.get("test_passed")) and bool(result.get("review_approved"))
        except Exception:
            result = {}
            success = False
        return Score(
            value=CORRECT if success else INCORRECT,
            answer="verified_success" if success else "not_verified",
            explanation=json.dumps(result, ensure_ascii=False),
            metadata={key: result.get(key) for key in ("run_id", "duration_ms", "total_tokens", "llm_calls", "test_passed", "review_approved")},
        )
    return score


@task
def agentharness_eval(dataset_path: str = str(DEFAULT_DATASET)) -> Task:
    """Score only real runs that pass the configured test command and reviewer gate."""
    return Task(dataset=load_samples(Path(dataset_path).expanduser().resolve()), solver=run_agentharness(), scorer=verified_run_score())
