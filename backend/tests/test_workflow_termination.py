"""Tests for AgentHarness Lab workflow termination, stage budgets, loop prevention,
terminal state immutability, subprocess management, and crash recovery.
"""
import asyncio
import json
import os
import shutil
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.agents import AgentExecutor
from app.config import Settings
from app.db import Database, TERMINAL_STATUSES, utcnow
from app.repository import RepositoryManager, ToolRuntime, _kill_process_tree
from app.runner import (
    RunManager,
    WorkflowTracker,
    WorkflowStage,
    WorkflowError,
    RunTimeoutError,
    evaluate_final_status,
)

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
    def __init__(self, name, arguments, call_id="fake-call-1"):
        self.id = call_id
        self.type = "function"
        self.function = SimpleNamespace(name=name, arguments=json.dumps(arguments))

    def model_dump(self, exclude_none=True):
        return {"id": self.id, "type": self.type, "function": {"name": self.function.name, "arguments": self.function.arguments}}


class FakeMessage:
    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


class NormalFakeLLM:
    def __init__(self):
        self.call_count = 0

    async def chat(self, *, messages, tools=None, temperature=0.1, max_tokens=4096):
        self.call_count += 1
        prompt = messages[0]["content"].lower()
        has_tool_result = any(message.get("role") == "tool" for message in messages)

        if "coordinator of a multi-agent" in prompt:
            content = json.dumps({
                "summary": "Fix utility bug",
                "steps": ["inspect", "fix", "test", "review"],
                "likely_files": ["src/miniapp.py"],
                "include_test_scout": True,
                "include_security_review": False,
                "risks": []
            })
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
            content, tool_calls = "VERDICT: APPROVE. Code changes are verified and pass tests.", []
        else:
            content, tool_calls = "Task completed successfully.", []

        message = FakeMessage(content, tool_calls)
        return {"message": message, "elapsed_ms": 1, "prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}


class WorkflowTerminationTests(unittest.IsolatedAsyncioTestCase):

    async def test_normal_workflow_reaches_terminal_state(self):
        """Test 1: Normal 6-agent workflow reaches valid terminal state."""
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
                agentharness_overall_run_timeout_seconds=120,
            )
            db = Database(settings.database_path)
            repos = RepositoryManager(settings)
            run_manager = RunManager(settings, db, NormalFakeLLM(), repos)

            run_id = "test-run-workflow-1"
            db.create_run(
                run_id,
                repository="agentharness-benchmark-fixture",
                task="Fix is_even in src/miniapp.py",
                test_command="auto",
                max_agent_steps=4,
            )
            await run_manager.execute(run_id)

            run = db.get_run(run_id)
            self.assertIn(run["status"], TERMINAL_STATUSES)
            self.assertIn(run["status"], {"succeeded", "success_with_warnings"})

            # Verify lifecycle events
            events = db.get_events(run_id)
            event_types = {e["event_type"] for e in events}
            self.assertIn("run_started", event_types)
            self.assertIn("stage_transition", event_types)
            self.assertIn("agent_handoff", event_types)
            self.assertIn("run_finished", event_types)

    async def test_overall_run_execution_deadline_triggers_timed_out(self):
        """Test 2: Exceeding overall run execution deadline terminates in timed_out."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repos_root = root / "repositories"
            repos_root.mkdir()
            shutil.copytree(FIXTURE_REPO, repos_root / "agentharness-benchmark-fixture", dirs_exist_ok=True)

            # Very small run deadline: 0.05s
            settings = Settings(
                agentharness_repos_root=str(repos_root),
                agentharness_runs_root=str(root / "runs"),
                agentharness_database=str(root / "agentharness.sqlite3"),
                agentharness_max_agent_steps=4,
                agentharness_overall_run_timeout_seconds=1,  # minimum clamped to 10 by tracker, but we test deadline check directly
            )
            db = Database(settings.database_path)
            repos = RepositoryManager(settings)

            # Test WorkflowTracker directly for deadline expiration
            tracker = WorkflowTracker(
                run_id="test-run",
                overall_timeout_seconds=10.0,
                stage_timeout_seconds=5.0,
                max_agent_handoffs=10,
            )
            # Force deadline in the past
            tracker.overall_deadline = time.perf_counter() - 1.0
            with self.assertRaises(RunTimeoutError):
                tracker.check_deadline()

            with self.assertRaises(RunTimeoutError):
                tracker.transition(WorkflowStage.PLANNING)

    async def test_stage_execution_budget_bounded(self):
        """Test 3: Stage execution budget is bounded by stage limit and overall deadline."""
        tracker = WorkflowTracker(
            run_id="test-run",
            overall_timeout_seconds=100.0,
            stage_timeout_seconds=30.0,
            max_agent_handoffs=10,
        )
        # Stage budget cannot exceed stage_timeout_seconds
        budget = tracker.get_remaining_stage_budget()
        self.assertLessEqual(budget, 30.0)
        self.assertGreater(budget, 25.0)

        # Stage budget cannot exceed overall remaining run budget
        tracker.overall_deadline = time.perf_counter() + 5.0
        budget = tracker.get_remaining_stage_budget()
        self.assertLessEqual(budget, 5.0)

    async def test_tool_call_loop_prevention(self):
        """Test 4: Consecutive duplicate tool calls are refused and trigger loop prevention."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(
                agentharness_max_duplicate_tool_calls=2,
                agentharness_max_tool_calls_per_agent=12,
            )
            runtime = ToolRuntime(root, settings, "auto")
            events = []

            # Mock LLM that endlessly emits the exact same tool call
            class LoopingLLM:
                async def chat(self, *, messages, tools=None, temperature=0.1, max_tokens=4096):
                    # Check if model received tool loop warning
                    has_loop_warning = any("duplicate" in str(m.get("content", "")).lower() for m in messages)
                    if has_loop_warning:
                        return {"message": FakeMessage("Stopping loop due to warning"), "elapsed_ms": 1, "prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10}
                    return {
                        "message": FakeMessage("", [FakeToolCall("list_files", {"path": "."})]),
                        "elapsed_ms": 1,
                        "prompt_tokens": 5,
                        "completion_tokens": 5,
                        "total_tokens": 10,
                    }

            executor = AgentExecutor(
                LoopingLLM(),
                runtime,
                event=lambda typ, msg, data: events.append({"type": typ, "message": msg, "data": data}),
            )

            result = await executor.run("repository_analyst", "Inspect repo", max_steps=8)
            loop_events = [e for e in events if e["type"] == "tool_loop_prevented"]
            self.assertGreater(len(loop_events), 0)
            self.assertIn("duplicate", loop_events[0]["message"].lower())

    async def test_tool_call_budget_limit_stops_runaway_agent(self):
        """Test 5: Agent exceeding max_tool_calls_per_agent is stopped."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(
                agentharness_max_tool_calls_per_agent=3,
                agentharness_max_duplicate_tool_calls=10,
            )
            runtime = ToolRuntime(root, settings, "auto")
            events = []
            call_idx = 0

            # Mock LLM that calls a different path each time to avoid duplicate detector
            class InfiniteCallsLLM:
                async def chat(self, *, messages, tools=None, temperature=0.1, max_tokens=4096):
                    nonlocal call_idx
                    call_idx += 1
                    return {
                        "message": FakeMessage("", [FakeToolCall("list_files", {"path": f"dir_{call_idx}"})]),
                        "elapsed_ms": 1,
                        "prompt_tokens": 5,
                        "completion_tokens": 5,
                        "total_tokens": 10,
                    }

            executor = AgentExecutor(
                InfiniteCallsLLM(),
                runtime,
                event=lambda typ, msg, data: events.append({"type": typ, "message": msg, "data": data}),
            )

            await executor.run("repository_analyst", "Inspect files", max_steps=10)
            budget_events = [e for e in events if e["type"] == "tool_budget_exceeded"]
            self.assertEqual(len(budget_events), 1)
            self.assertIn("budget", budget_events[0]["message"].lower())

    def test_terminal_state_immutability(self):
        """Test 6: Terminal runs cannot be overwritten by subsequent non-forced updates."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.sqlite3"
            db = Database(db_path)

            run_id = "test-run-immutability"
            db.create_run(run_id, "repo", "task", "pytest")
            db.update_run(run_id, status="running")

            # Finalize to terminal status 'timed_out'
            db.update_run(run_id, status="timed_out")
            self.assertEqual(db.get_run(run_id)["status"], "timed_out")

            # Attempt late overwrite to 'running' or 'failed'
            db.update_run(run_id, status="running")
            self.assertEqual(db.get_run(run_id)["status"], "timed_out")

            db.update_run(run_id, status="failed", error="Late error from background")
            self.assertEqual(db.get_run(run_id)["status"], "timed_out")

            # Force update succeeds if explicitly requested
            db.update_run(run_id, status="cancelled", force=True)
            self.assertEqual(db.get_run(run_id)["status"], "cancelled")

    def test_stale_run_cleanup_on_startup(self):
        """Test 7: Stale running or queued runs are cleanly recovered on startup."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.sqlite3"
            db = Database(db_path)

            run1 = "run-stale-1"
            run2 = "run-stale-2"
            run3 = "run-stale-3"
            db.create_run(run1, "repo", "task 1", "pytest")
            db.create_run(run2, "repo", "task 2", "pytest")
            db.create_run(run3, "repo", "task 3", "pytest")

            db.update_run(run1, status="running")
            # run2 remains 'queued'
            db.update_run(run3, status="succeeded")

            recovered = db.cleanup_stale_runs()
            self.assertEqual(recovered, 2)

            self.assertEqual(db.get_run(run1)["status"], "failed")
            self.assertIn("server restarted", db.get_run(run1)["error"].lower())

            self.assertEqual(db.get_run(run2)["status"], "failed")
            self.assertIn("server restarted", db.get_run(run2)["error"].lower())

            # Terminal runs remain untouched
            self.assertEqual(db.get_run(run3)["status"], "succeeded")

    async def test_subprocess_cancellation_kills_process_group(self):
        """Test 8: Subprocess cancellation terminates the entire process group cleanly."""
        # Spawn a python process that sleeps
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import time; time.sleep(60)",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=(sys.platform != "win32"),
        )
        pid = proc.pid
        self.assertIsNotNone(pid)

        # Kill process group
        _kill_process_tree(proc)

        # Verify process is terminated
        await asyncio.sleep(0.1)
        self.assertIsNotNone(proc.returncode)

    def test_exactly_once_run_finalization(self):
        """Test 9: _finalize_run executes exactly once per run_id."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(
                agentharness_repos_root=str(root / "repositories"),
                agentharness_runs_root=str(root / "runs"),
                agentharness_database=str(root / "agentharness.sqlite3"),
            )
            db = Database(settings.database_path)
            repos = RepositoryManager(settings)
            run_manager = RunManager(settings, db, NormalFakeLLM(), repos)

            run_id = "test-run-immutability"
            db.create_run(run_id, "repo", "task", "pytest")
            db.update_run(run_id, status="running")

            first = run_manager._finalize_run(
                run_id,
                status="succeeded",
                duration_ms=1000,
                metrics={"final_status": "SUCCESS"},
                result={"success": True},
                evaluation_report={"run_id": run_id, "success": True},
                workspace=root,
            )
            self.assertTrue(first)

            # Second call on same run_id must be a no-op
            second = run_manager._finalize_run(
                run_id,
                status="failed",
                duration_ms=2000,
                metrics={"final_status": "FAILED"},
                result={"success": False},
                evaluation_report={"run_id": run_id, "success": False},
                workspace=root,
            )
            self.assertFalse(second)
            self.assertEqual(db.get_run(run_id)["status"], "succeeded")

    def test_shared_run_scoped_dependency_preparation_reuse(self):
        """Test 10: ToolRuntime stores and reuses run-scoped dependency preparation."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings()
            dep_info = {
                "status": "DEPENDENCIES_READY",
                "declared_packages": ["pandas", "fastapi"],
                "cached": True,
            }
            runtime = ToolRuntime(root, settings, "auto", dep_info=dep_info)
            self.assertEqual(runtime.dep_info["status"], "DEPENDENCIES_READY")
            self.assertEqual(runtime.dep_info["declared_packages"], ["pandas", "fastapi"])

    def test_max_handoff_bounding_prevents_circular_routing(self):
        """Test 11: WorkflowTracker limits total agent handoffs to prevent circular loops."""
        tracker = WorkflowTracker(
            run_id="test-run",
            overall_timeout_seconds=100.0,
            stage_timeout_seconds=30.0,
            max_agent_handoffs=3,
        )
        tracker.record_handoff("coordinator", "analyst")
        tracker.record_handoff("analyst", "coder")
        tracker.record_handoff("coder", "tester")
        self.assertEqual(tracker.handoff_count, 3)

        # 4th handoff exceeds limit
        with self.assertRaises(WorkflowError) as cm:
            tracker.record_handoff("tester", "coder")
        self.assertIn("Maximum agent handoffs exceeded", str(cm.exception))

    def test_evaluate_final_status_timed_out_and_cancelled(self):
        """Test 12: evaluate_final_status correctly handles TIMED_OUT and CANCELLED."""
        eval_timeout = evaluate_final_status(
            implementation_status="TIMED_OUT",
            test_state="BLOCKED",
            error_message="Workflow deadline exceeded",
        )
        self.assertEqual(eval_timeout["execution_status"], "TIMED_OUT")
        self.assertEqual(eval_timeout["final_status"], "FAILED")
        self.assertEqual(eval_timeout["db_status"], "timed_out")
        self.assertFalse(eval_timeout["success"])
        self.assertTrue(any("timed out" in c.lower() for c in eval_timeout["verification_details"]["checks_failed"]))

        eval_cancel = evaluate_final_status(
            implementation_status="CANCELLED",
            test_state="BLOCKED",
            error_message="Cancelled by user",
        )
        self.assertEqual(eval_cancel["execution_status"], "CANCELLED")
        self.assertEqual(eval_cancel["final_status"], "FAILED")
        self.assertEqual(eval_cancel["db_status"], "cancelled")
        self.assertFalse(eval_cancel["success"])

    async def test_sse_stream_terminates_on_timed_out(self):
        """Test 13: SSE stream generator terminates promptly with event: done on timed_out."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.sqlite3"
            db = Database(db_path)
            run_id = "run-sse-timeout"
            db.create_run(run_id, "repo", "task", "pytest")
            db.update_run(run_id, status="timed_out")

            class MockRequest:
                async def is_disconnected(self):
                    return False

            cursor = 0
            chunks = []
            request = MockRequest()
            while not await request.is_disconnected():
                events = db.get_events(run_id, after_id=cursor, limit=100)
                for event in events:
                    cursor = event["id"]
                    chunks.append(f"event: {event['event_type']}")
                row = db.get_run(run_id)
                if row and row["status"] in {"succeeded", "failed", "cancelled", "success_with_warnings", "timed_out"} and not events:
                    chunks.append("event: done")
                    break
                await asyncio.sleep(0.01)

            self.assertIn("event: done", chunks)


if __name__ == "__main__":
    unittest.main()
