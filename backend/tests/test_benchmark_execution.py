"""End-to-end benchmark orchestration test using a deterministic fake LLM client.

This validates orchestration, tool execution, Git workspaces, scoring, and report persistence
without making paid cloud calls. It does not contribute model-performance measurements.
"""
import asyncio
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.benchmarks import BenchmarkManager
from app.config import Settings
from app.db import Database
from app.repository import RepositoryManager
from app.runner import RunManager

FIXTURE_REPO = Path(__file__).resolve().parents[2] / "workspace" / "repositories" / "agentharness-benchmark-fixture"
FIXED_SOURCE = '''"""Fixed utility implementation for deterministic harness integration tests."""

def is_even(value: int) -> bool:
    return value % 2 == 0

def normalize_email(email: str) -> str:
    return email.strip().lower()

def safe_divide(numerator: float, denominator: float) -> float:
    if denominator == 0:
        raise ValueError("denominator cannot be zero")
    return numerator / denominator

def unique_preserving_order(values: list[int]) -> list[int]:
    return list(dict.fromkeys(values))
'''


class FakeToolCall:
    def __init__(self, name, arguments):
        self.id = "fake-call-1"
        self.type = "function"
        self.function = SimpleNamespace(name=name, arguments=json.dumps(arguments))

    def model_dump(self, exclude_none=True):
        return {"id": self.id, "type": self.type, "function": {"name": self.function.name, "arguments": self.function.arguments}}


class FakeMessage:
    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


class FakeLLM:
    async def chat(self, *, messages, tools=None, temperature=0.1, max_tokens=4096):
        prompt = messages[0]["content"].lower()
        has_tool_result = any(message.get("role") == "tool" for message in messages)
        if "coordinator of a multi-agent" in prompt:
            content = json.dumps({"summary": "Fix one focused utility bug", "steps": ["inspect", "fix", "test", "review"], "likely_files": ["src/miniapp.py"], "include_test_scout": True, "include_security_review": False, "risks": []})
            tool_calls = []
        elif "repository analyst" in prompt and not has_tool_result:
            content, tool_calls = "", [FakeToolCall("list_files", {"path": "src"})]
        elif "test discovery agent" in prompt and not has_tool_result:
            content, tool_calls = "", [FakeToolCall("list_files", {"path": "tests"})]
        elif "implementation agent" in prompt and not has_tool_result:
            content, tool_calls = "", [FakeToolCall("write_file", {"path": "src/miniapp.py", "content": FIXED_SOURCE})]
        elif "test and debugging agent" in prompt and not has_tool_result:
            content, tool_calls = "", [FakeToolCall("run_tests", {"command": "auto"})]
        elif "independent code reviewer" in prompt and not has_tool_result:
            content, tool_calls = "", [FakeToolCall("git_diff", {})]
        elif "independent code reviewer" in prompt:
            content, tool_calls = "The diff is focused and the configured regression test passed. VERDICT: APPROVE", []
        elif "repository analyst" in prompt:
            content, tool_calls = "Inspected source file paths with the repository listing tool.", []
        elif "test discovery agent" in prompt:
            content, tool_calls = "Inspected test file paths with the repository listing tool.", []
        elif "implementation agent" in prompt:
            content, tool_calls = "Updated the utility implementation.", []
        elif "test and debugging agent" in prompt:
            content, tool_calls = "Ran the configured focused test command; the command output is recorded by the harness.", []
        else:
            content, tool_calls = "VERDICT: APPROVE", []
        message = FakeMessage(content, tool_calls)
        return {"message": message, "elapsed_ms": 1, "prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}


class BenchmarkExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_benchmark_records_verified_sample_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repos_root = root / "repositories"
            repos_root.mkdir()
            shutil.copytree(FIXTURE_REPO, repos_root / "agentharness-benchmark-fixture", dirs_exist_ok=True)
            settings = Settings(
                agentharness_repos_root=str(repos_root),
                agentharness_runs_root=str(root / "runs"),
                agentharness_database=str(root / "agentharness.sqlite3"),
                agentharness_max_agent_steps=4,
                agentharness_test_timeout_seconds=30,
            )
            db = Database(settings.database_path)
            repos = RepositoryManager(settings)
            run_manager = RunManager(settings, db, FakeLLM(), repos)
            benchmark_manager = BenchmarkManager(settings, db, run_manager, repos)
            started = benchmark_manager.start(repetitions=1)
            await asyncio.wait_for(benchmark_manager.tasks[started["id"]], timeout=60)
            result = benchmark_manager.get(started["id"])
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["completed_samples"], 4)
            self.assertEqual(result["summary"]["successful_samples"], 4)
            self.assertEqual(result["summary"]["success_rate_percent"], 100.0)
            self.assertTrue(all(sample["test_passed"] and sample["review_approved"] for sample in result["results"]))
            self.assertTrue(all(sample["success"] for sample in result["results"]))
            self.assertTrue(all(db.get_run(sample["run_id"])["status"] in {"succeeded", "success_with_warnings"} for sample in result["results"]))


if __name__ == "__main__":
    unittest.main()
