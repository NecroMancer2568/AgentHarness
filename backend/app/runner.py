import asyncio
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from .agents import AgentExecutor
from .config import Settings
from .db import Database, utcnow
from .dependencies import prepare_environment
from .llm import LLMClient
from .repository import RepositoryManager, ToolRuntime, WorkspaceError


def parse_json_loose(text: str) -> dict[str, Any]:
    try:
        return json.loads(text)
    except Exception:
        match = re.search(r"\{.*\}", text, re.S)
        if match:
            try: return json.loads(match.group(0))
            except Exception: pass
    return {"summary": text[:500], "steps": [], "likely_files": [], "include_test_scout": True, "include_security_review": False, "risks": ["Planner output was not valid JSON; using conservative default routing."]}


def parse_reviewer_verdict(text: str) -> dict[str, Any]:
    """Parse structured verdict and rejection reasons from Code Reviewer output."""
    if not text or not text.strip():
        return {
            "verdict": "INCONCLUSIVE",
            "review_state": "INCONCLUSIVE",
            "approved": False,
            "summary": "No reviewer output received.",
            "task_alignment": "",
            "test_evidence_verified": False,
            "rejection_reasons": ["Code review did not return output."],
        }

    # Check for structured JSON first
    parsed_json = None
    try:
        parsed_json = json.loads(text)
    except Exception:
        match = re.search(r"\{.*\}", text, re.S)
        if match:
            try:
                parsed_json = json.loads(match.group(0))
            except Exception:
                pass

    if isinstance(parsed_json, dict):
        if "verdict" in parsed_json:
            raw_verdict = str(parsed_json.get("verdict", "")).strip().upper()
            if "APPROVE" in raw_verdict:
                verdict = "APPROVE"
                review_state = "APPROVED"
            elif "PENDING" in raw_verdict:
                verdict = "PENDING"
                review_state = "PENDING"
            elif "BLOCKED" in raw_verdict:
                verdict = "BLOCKED"
                review_state = "BLOCKED"
            elif "INCONCLUSIVE" in raw_verdict:
                verdict = "INCONCLUSIVE"
                review_state = "INCONCLUSIVE"
            else:
                verdict = "NEEDS_CHANGES"
                review_state = "REJECTED"

            rejection_reasons = parsed_json.get("rejection_reasons") or []
            if isinstance(rejection_reasons, str):
                rejection_reasons = [rejection_reasons]
            return {
                "verdict": verdict,
                "review_state": review_state,
                "approved": review_state == "APPROVED",
                "summary": str(parsed_json.get("summary") or text[:500]),
                "task_alignment": str(parsed_json.get("task_alignment") or ""),
                "test_evidence_verified": bool(parsed_json.get("test_evidence_verified", False)),
                "rejection_reasons": list(rejection_reasons),
            }
        else:
            return {
                "verdict": "INCONCLUSIVE",
                "review_state": "INCONCLUSIVE",
                "approved": False,
                "summary": str(parsed_json.get("summary") or text[:500]),
                "task_alignment": "",
                "test_evidence_verified": False,
                "rejection_reasons": ["Invalid structured output: missing 'verdict' field in review JSON."],
            }

    # Regex search for explicit VERDICT:
    verdict_match = re.search(r"VERDICT:\s*(APPROVE|APPROVED|NEEDS_CHANGES|REJECT|REJECTED|PENDING|BLOCKED|INCONCLUSIVE)", text, re.IGNORECASE)
    rejection_reasons = []
    reasons_block = re.search(r"REJECTION_REASONS:?\s*(.*?)(?:\n\n|\Z)", text, re.IGNORECASE | re.DOTALL)
    if reasons_block:
        for line in reasons_block.group(1).splitlines():
            line = line.strip().lstrip("-* ").strip()
            if line:
                rejection_reasons.append(line)

    if verdict_match:
        matched = verdict_match.group(1).upper()
        if matched in {"APPROVE", "APPROVED"}:
            verdict = "APPROVE"
            review_state = "APPROVED"
        elif matched == "PENDING":
            verdict = "PENDING"
            review_state = "PENDING"
        elif matched == "BLOCKED":
            verdict = "BLOCKED"
            review_state = "BLOCKED"
        elif matched == "INCONCLUSIVE":
            verdict = "INCONCLUSIVE"
            review_state = "INCONCLUSIVE"
        else:
            verdict = "NEEDS_CHANGES"
            review_state = "REJECTED"

        return {
            "verdict": verdict,
            "review_state": review_state,
            "approved": review_state == "APPROVED",
            "summary": text[:500],
            "task_alignment": "",
            "test_evidence_verified": review_state == "APPROVED",
            "rejection_reasons": rejection_reasons,
        }

    if "Agent reached its step limit before producing a final summary." in text:
        return {
            "verdict": "PENDING",
            "review_state": "PENDING",
            "approved": False,
            "summary": "Reviewer reached step limit without final verdict.",
            "task_alignment": "",
            "test_evidence_verified": False,
            "rejection_reasons": ["Reviewer reached step limit before completing verdict."],
        }

    if any(phrase in text.lower() for phrase in ["missing review evidence", "no diff provided", "empty diff"]):
        return {
            "verdict": "BLOCKED",
            "review_state": "BLOCKED",
            "approved": False,
            "summary": text[:500],
            "task_alignment": "",
            "test_evidence_verified": False,
            "rejection_reasons": ["Code review blocked due to missing review evidence or empty diff."],
        }

    # Fallback inspection of text sentiment
    if "APPROVE" in text.upper() and "NEEDS_CHANGES" not in text.upper() and not rejection_reasons:
        return {
            "verdict": "APPROVE",
            "review_state": "APPROVED",
            "approved": True,
            "summary": text[:500],
            "task_alignment": "",
            "test_evidence_verified": True,
            "rejection_reasons": [],
        }

    return {
        "verdict": "NEEDS_CHANGES",
        "review_state": "REJECTED",
        "approved": False,
        "summary": text[:500],
        "task_alignment": "",
        "test_evidence_verified": False,
        "rejection_reasons": rejection_reasons or ["Reviewer did not explicitly approve the patch."],
    }


def compute_verification_state(
    tests: list[dict[str, Any]],
    reviewer_output: str,
    workspace: Path | None = None,
    dependency_status: str | None = None,
    dependency_error: str | None = None,
    baseline_tests: list[dict[str, Any]] | None = None,
) -> tuple[str, bool, bool, str | None, dict[str, Any]]:
    """
    Compute explicit verification state across dependency preparation, baseline comparison,
    test execution, and code review:
      Tests: PASS, FAIL, NO_TESTS, BLOCKED, INCONCLUSIVE
      Code Review: APPROVED, REJECTED, PENDING, BLOCKED, INCONCLUSIVE
      Overall: PASS, PARTIAL, FAIL, BLOCKED, INCONCLUSIVE
    Returns: (overall_verdict, test_passed, review_approved, error_message, review_details)
    """
    review_details = parse_reviewer_verdict(reviewer_output)
    review_approved = review_details["approved"]
    review_state = review_details.get("review_state", "REJECTED" if not review_approved else "APPROVED")
    rejection_reasons = review_details.get("rejection_reasons", [])

    if dependency_status in {"DEPENDENCY_INSTALL_FAILED", "DEPENDENCY_INSTALL_BLOCKED"}:
        review_details.update({
            "verification_verdict": dependency_status,
            "test_state": "BLOCKED",
            "review_state": review_state,
            "regression_status": "BLOCKED",
            "baseline_comparison": {},
            "test_failure_details": [],
        })
        return (
            dependency_status,
            False,
            False,
            dependency_error or f"Dependency preparation failed ({dependency_status}).",
            review_details,
        )

    if not tests:
        test_state = "NO_TESTS"
        test_err = "No tests were executed."
        current_failures = []
        last_metrics = {"collected": 0, "passed": 0, "failed": 0, "skipped": 0, "errors": 0}
    else:
        last_test = tests[-1]
        outcome = last_test.get("outcome", "FAIL")
        test_err = last_test.get("error_message") or last_test.get("output", "")[:500]
        current_failures = last_test.get("failure_details", [])
        last_metrics = last_test.get("metrics", {})
        if outcome == "PASS":
            test_state = "PASS"
        elif outcome == "FAIL":
            test_state = "FAIL"
        elif outcome == "NO_TESTS":
            test_state = "NO_TESTS"
        elif outcome == "MISSING_DEPENDENCY":
            test_state = "MISSING_DEPENDENCY"
        elif outcome == "BLOCKED":
            test_state = "BLOCKED"
        else:
            test_state = "INCONCLUSIVE"

    test_passed = (test_state == "PASS")

    # Baseline comparison
    if baseline_tests:
        last_base = baseline_tests[-1]
        base_failures = set(last_base.get("failure_details", []))
        base_collected = last_base.get("metrics", {}).get("collected", 0)
        base_passed = last_base.get("metrics", {}).get("passed", 0)
        base_failed = last_base.get("metrics", {}).get("failed", 0)
        base_state = last_base.get("outcome", "NO_TESTS")
        pre_existing = [f for f in current_failures if f in base_failures]
        new_failures = [f for f in current_failures if f not in base_failures] if base_collected > 0 else []
        resolved = [f for f in base_failures if f not in current_failures]
    else:
        base_collected = 0
        base_passed = 0
        base_failed = 0
        base_state = "NO_TESTS"
        pre_existing = []
        new_failures = []
        resolved = []

    baseline_comparison = {
        "baseline_collected": base_collected,
        "baseline_passed": base_passed,
        "baseline_failed": base_failed,
        "baseline_state": base_state,
        "pre_existing_failures": pre_existing,
        "new_failures": new_failures,
        "resolved_failures": resolved,
    }

    # Regression status
    if new_failures:
        regression_status = "REGRESSION_DETECTED"
    elif pre_existing and not test_passed:
        if review_state == "APPROVED" and not new_failures:
            regression_status = "PRE_EXISTING_FAILURE"
        else:
            regression_status = "INCOMPLETE_FIX"
    elif not test_passed:
        regression_status = "INCOMPLETE_FIX"
    else:
        regression_status = "NO_REGRESSION"

    # Overall verification verdict calculation
    if test_state == "MISSING_DEPENDENCY":
        overall_verdict = "MISSING_DEPENDENCY"
        error_msg = f"Test execution failed due to undeclared dependency: {test_err}"
    elif test_state == "BLOCKED":
        overall_verdict = "BLOCKED"
        error_msg = f"Test execution was blocked: {test_err}"
    elif review_state == "BLOCKED":
        overall_verdict = "BLOCKED"
        error_msg = f"Code review was blocked: {'; '.join(rejection_reasons) or 'Missing review evidence'}"
    elif test_state == "NO_TESTS":
        overall_verdict = "NO_TESTS"
        error_msg = test_err or "No tests were collected to verify the change."
    elif test_state == "PASS" and review_state == "APPROVED":
        overall_verdict = "PASS"
        error_msg = None
    elif test_state == "PASS" and review_state in {"REJECTED", "PENDING", "INCONCLUSIVE"}:
        overall_verdict = "PARTIAL"
        error_msg = f"Tests passed ({last_metrics.get('passed', 0)} passed), but review is {review_state}: {'; '.join(rejection_reasons) or 'Changes not approved.'}"
    elif test_state == "FAIL" and pre_existing and not new_failures and review_state == "APPROVED":
        overall_verdict = "PARTIAL"
        error_msg = f"Bug fix approved, but repository contains pre-existing unrelated test failures: {', '.join(pre_existing)}"
    elif test_state == "FAIL":
        overall_verdict = "FAIL"
        error_msg = f"Tests failed: {test_err}"
    elif review_state == "REJECTED":
        overall_verdict = "FAIL"
        error_msg = f"Code reviewer rejected changes: {'; '.join(rejection_reasons) or 'Rejected'}"
    elif review_state in {"PENDING", "INCONCLUSIVE"}:
        overall_verdict = "INCONCLUSIVE"
        error_msg = f"Review could not be completed ({review_state})."
    else:
        overall_verdict = "FAIL"
        error_msg = "Test verification failed."

    review_details.update({
        "verification_verdict": overall_verdict,
        "test_state": test_state,
        "review_state": review_state,
        "regression_status": regression_status,
        "baseline_comparison": baseline_comparison,
        "test_failure_details": current_failures,
    })

    return overall_verdict, test_passed, review_approved, error_msg, review_details


class RunManager:
    def __init__(self, settings: Settings, db: Database, llm: LLMClient, repos: RepositoryManager):
        self.settings, self.db, self.llm, self.repos = settings, db, llm, repos
        self.tasks: dict[str, asyncio.Task] = {}
        self.semaphore = asyncio.Semaphore(max(1, settings.agentharness_max_parallel_readers))

    def event(self, run_id: str, event_type: str, message: str, data: dict[str, Any] | None = None, agent: str | None = None):
        self.db.add_event(run_id, event_type, agent, message, data or {})

    async def execute(self, run_id: str):
        row = self.db.get_run(run_id)
        if not row: return
        started = time.perf_counter()
        self.db.update_run(run_id, status="running", started_at=utcnow())
        self.event(run_id, "run_started", "Execution started", {"model": self.settings.ollama_model})
        workspace: Path | None = None
        usage_totals = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "inference_ms": 0, "tool_calls": 0}
        agent_outputs: dict[str, str] = {}
        try:
            repo = self.repos.resolve_repository(row["repository"])
            workspace = await self.repos.create_workspace(repo, run_id)
            self.db.update_run(run_id, workspace_path=str(workspace))
            self.event(run_id, "workspace_ready", "Created a disposable Git clone for this run", {"workspace": str(workspace)})

            # Prepare environment (manifest inspection, isolated virtualenv, package installation)
            dep_info = await prepare_environment(
                workspace,
                self.settings,
                repo.name,
                event_callback=lambda typ, msg, data: self.event(run_id, typ, msg, data),
            )
            venv_path = dep_info.get("venv_path")
            declared_packages = dep_info.get("declared_packages", [])

            runtime = ToolRuntime(
                workspace,
                self.settings,
                row["test_command"],
                venv_path=venv_path,
                declared_packages=declared_packages,
                event_callback=lambda typ, msg, data: self.event(run_id, typ, msg, data),
            )
            max_steps = min(max(int(row.get("max_agent_steps") or self.settings.agentharness_max_agent_steps), 1), 20)
            executor = AgentExecutor(self.llm, runtime,
                event=lambda typ, msg, data: self.event(run_id, typ, msg, data))

            # Record baseline tests before any agent modifications
            baseline_record = await runtime.run_test_command(
                runtime._detect_test_command() or ["python", "-m", "pytest", "-q"],
                timeout=min(self.settings.agentharness_test_timeout_seconds, 60)
            )
            baseline_tests = [baseline_record] if baseline_record else []
            self.event(
                run_id,
                "baseline_recorded",
                f"Baseline test status: {baseline_record.get('outcome', 'NO_TESTS')} ({baseline_record.get('metrics', {}).get('collected', 0)} collected)",
                {"baseline": baseline_record}
            )

            # Step 1: Coordinator
            plan_result = await executor.run(
                "coordinator",
                row["task"],
                context="Create an execution plan. Set include_security_review true only if security/auth/secrets/injection are involved.",
                max_steps=1,
                tools_enabled=False
            )
            agent_outputs["coordinator"] = plan_result["output"]
            plan = parse_json_loose(plan_result["output"])
            self.event(run_id, "plan_created", "Coordinator created the execution plan", {"plan": plan}, "coordinator")

            # Initial repository test inspection
            repo_test_info = runtime.detect_repository_tests()
            self.event(run_id, "test_discovery_snapshot", f"Detected {repo_test_info['detected_language']} ({repo_test_info['detected_framework']})", repo_test_info)

            # Step 2: Read-only specialist investigation
            async def reader(role: str, extra_ctx: str = ""):
                async with self.semaphore:
                    return await executor.run(
                        role,
                        row["task"],
                        context=f"Coordinator plan:\n{json.dumps(plan)}\n{extra_ctx}\nInspect repository code using tools; do not edit.",
                        max_steps=max_steps
                    )

            scout_ctx = f"Automated test detection:\n{json.dumps(repo_test_info, indent=2)}"
            if plan.get("include_test_scout", True):
                analyst, scout = await asyncio.gather(reader("repository_analyst"), reader("test_scout", scout_ctx))
                agent_outputs[analyst["role"]] = analyst["output"]
                agent_outputs[scout["role"]] = scout["output"]
                test_discovery = scout["output"]
            else:
                analyst = await reader("repository_analyst")
                agent_outputs[analyst["role"]] = analyst["output"]
                test_discovery = "Test discovery was skipped by coordinator."

            # Step 3: Coding Agent with structured handoff
            coding_context = (
                f"COORDINATOR PLAN:\n{json.dumps(plan)}\n\n"
                f"REPOSITORY ANALYSIS:\n{analyst['output'][:2500]}\n\n"
                f"TEST DISCOVERY & RECOMMENDATIONS:\n{test_discovery[:2500]}\n\n"
                f"WORKSPACE TEST STATUS:\n{json.dumps(repo_test_info, indent=2)}\n"
                "CRITICAL: If the repository has NO tests or tests for this bug are missing, you MUST create a meaningful regression test file."
            )

            coder = await executor.run("coding_agent", row["task"], context=coding_context, max_steps=max_steps)
            agent_outputs[coder["role"]] = coder["output"]
            self.event(run_id, "implementation_ready", "Coding agent finished its implementation pass", {"summary": coder["output"][:2000]}, "coding_agent")

            # Step 4: Test & Debug Agent
            diff_after_coder = await runtime.execute("git_diff", {})
            tester_context = (
                f"TASK:\n{row['task']}\n\n"
                f"TEST DISCOVERY:\n{test_discovery[:2500]}\n\n"
                f"IMPLEMENTATION REPORT:\n{coder['output'][:2500]}\n\n"
                f"GIT DIFF:\n{diff_after_coder[:3000]}\n\n"
                f"CONFIGURED TEST COMMAND: {row['test_command']}\n"
                "Run tests using run_tests. If pytest reports exit code 5 (no tests collected) or missing tests, locate or fix the test files."
            )

            tester = await executor.run("test_debug_agent", row["task"], context=tester_context, max_steps=max_steps)
            agent_outputs[tester["role"]] = tester["output"]

            # Fallback test run if test_debug_agent did not run any test tool
            if not runtime.test_results:
                self.event(run_id, "verification_fallback", "Test agent did not execute tests; running configured verification command", {"test_command": row["test_command"]}, "test_debug_agent")
                await runtime.execute("run_tests", {"command": "auto"})

            # Structured test summary for Code Reviewer
            if runtime.test_results:
                last_test = runtime.test_results[-1]
                m = last_test.get("metrics", {})
                test_evidence_summary = (
                    f"Command: {' '.join(last_test.get('command', []))}\n"
                    f"Outcome: {last_test.get('outcome')}\n"
                    f"Exit Code: {last_test.get('exit_code')}\n"
                    f"Tests Collected: {m.get('collected', 0)}, Passed: {m.get('passed', 0)}, Failed: {m.get('failed', 0)}, Skipped: {m.get('skipped', 0)}\n"
                    f"Stdout snippet:\n{last_test.get('stdout', '')[:800]}\n"
                    f"Stderr snippet:\n{last_test.get('stderr', '')[:800]}\n"
                )
            else:
                test_evidence_summary = "No test command could be executed."

            final_diff = await runtime.execute("git_diff", {})
            diff_display = final_diff if final_diff.strip() else "No working tree diff (no files were modified)."

            # Detailed summaries for Reviewer
            base_m = baseline_record.get("metrics", {}) if baseline_tests else {}
            baseline_summary = (
                f"Command: {' '.join(baseline_record.get('command', []))}\n"
                f"Outcome: {baseline_record.get('outcome', 'NO_TESTS')}\n"
                f"Collected: {base_m.get('collected', 0)}, Passed: {base_m.get('passed', 0)}, Failed: {base_m.get('failed', 0)}\n"
                f"Failures: {', '.join(baseline_record.get('failure_details', [])) or 'None'}"
            ) if baseline_tests else "No baseline test executed."

            changed_files = await self._changed_files(workspace)
            changed_files_summary = "\n".join(f"- {f}" for f in changed_files) or "No files changed."

            # Step 5: Code Reviewer
            review_context = (
                f"ORIGINAL TASK / BUG DESCRIPTION:\n{row['task']}\n\n"
                f"COORDINATOR PLAN:\n{json.dumps(plan)}\n\n"
                f"IMPLEMENTATION SUMMARY:\n{coder['output'][:2000]}\n\n"
                f"BASELINE TESTS (BEFORE AGENT CHANGES):\n{baseline_summary}\n\n"
                f"AFTER-STATE TEST EXECUTION EVIDENCE:\n{test_evidence_summary}\n\n"
                f"CHANGED FILES:\n{changed_files_summary}\n\n"
                f"GIT DIFF:\n{diff_display[:10000]}\n\n"
                "Check bug fix against the original task problem statement.\n"
                "Verify that tests ran, test evidence is genuine, and all tests passed.\n"
                "If tests failed or no changes exist, reject with NEEDS_CHANGES and state rejection reasons in the structured output.\n"
                "If changes are correct and all tests pass with verified evidence, approve with APPROVE."
            )

            reviewer = await executor.run("code_reviewer", row["task"], context=review_context, max_steps=max_steps)
            agent_outputs[reviewer["role"]] = reviewer["output"]

            # Step 6: Security Reviewer (if requested)
            security_review = None
            security_keywords = ("security", "auth", "permission", "secret", "credential", "token", "injection",
                                 "path traversal", "command execution", "authorization", "user data", "sql")
            needs_security_review = bool(plan.get("include_security_review")) or any(word in row["task"].lower() for word in security_keywords)
            if needs_security_review:
                security_review = await executor.run("security_reviewer", row["task"], context=review_context, max_steps=max_steps)
                agent_outputs[security_review["role"]] = security_review["output"]

            usage_totals = executor.usage
            duration_ms = round((time.perf_counter() - started) * 1000)

            # Verification state aggregation
            verification_state, test_passed, review_approved, error_msg, review_details = compute_verification_state(
                runtime.test_results,
                reviewer["output"],
                workspace,
                dependency_status=dep_info.get("status"),
                dependency_error=dep_info.get("error"),
                baseline_tests=baseline_tests,
            )
            success = (verification_state == "PASS")

            last_metrics = runtime.test_results[-1].get("metrics", {}) if runtime.test_results else {}
            last_outcome = runtime.test_results[-1].get("outcome") if runtime.test_results else "NO_TESTS"
            test_state_label = review_details.get("test_state", last_outcome)

            dep_summary = {
                "status": dep_info.get("status"),
                "language": dep_info.get("language"),
                "duration_ms": dep_info.get("duration_ms"),
                "cached": dep_info.get("cached", False),
                "declared_packages": dep_info.get("declared_packages", []),
                "manifest_hash": dep_info.get("manifest_hash"),
                "exit_code": dep_info.get("exit_code"),
                "error": dep_info.get("error"),
            }

            metrics = {
                "duration_ms": duration_ms,
                "inference_ms": usage_totals["inference_ms"],
                "tool_calls": usage_totals["tool_calls"],
                "agent_count": len(agent_outputs),
                "agent_handoffs": max(0, len(agent_outputs) - 1),
                "agent_metrics": getattr(executor, "agent_metrics", {}),
                "dependency_status": dep_info.get("status"),
                "dependency_preparation": dep_summary,
                "test_state": test_state_label,
                "review_state": review_details.get("review_state", "REJECTED" if not review_approved else "APPROVED"),
                "verification_state": verification_state,
                "verification_verdict": review_details.get("verification_verdict", verification_state),
                "regression_status": review_details.get("regression_status", "NO_REGRESSION"),
                "baseline_comparison": review_details.get("baseline_comparison", {}),
                "test_failure_details": review_details.get("test_failure_details", []),
                "test_passed": test_passed,
                "review_approved": review_approved,
                "review_verdict": review_details.get("verdict"),
                "review_reasons": review_details.get("rejection_reasons", []),
                "tests_collected": last_metrics.get("collected", 0),
                "tests_passed": last_metrics.get("passed", 0),
                "tests_failed": last_metrics.get("failed", 0),
                "tests_skipped": last_metrics.get("skipped", 0),
                "changed_files": len(changed_files),
                "tests": runtime.test_results,
                "security_review_included": security_review is not None,
                "success_definition": "Relevant tests were collected and passed (PASS) and the code reviewer approved the diff.",
            }

            result = {
                "plan": plan,
                "dependency_status": dep_info.get("status"),
                "dependency_preparation": dep_summary,
                "verification_state": verification_state,
                "verification_verdict": review_details.get("verification_verdict", verification_state),
                "test_state": test_state_label,
                "review_state": review_details.get("review_state"),
                "regression_status": review_details.get("regression_status"),
                "baseline_comparison": review_details.get("baseline_comparison"),
                "test_failure_details": review_details.get("test_failure_details", []),
                "review_verdict": review_details.get("verdict"),
                "review_approved": review_approved,
                "test_passed": test_passed,
                "success": success,
                "test_results": runtime.test_results,
                "test_metrics": last_metrics,
                "changed_files": changed_files,
                "diff": final_diff[:30000],
                "agents": agent_outputs,
                "agent_metrics": getattr(executor, "agent_metrics", {}),
                "security_review": security_review["output"] if security_review else None,
            }

            # Write evaluation.json report recording dependency preparation separately from test results
            evaluation_report = {
                "run_id": run_id,
                "repository": row["repository"],
                "task": row["task"],
                "verification_state": verification_state,
                "verification_verdict": review_details.get("verification_verdict", verification_state),
                "success": success,
                "duration_ms": duration_ms,
                "dependency_preparation": dep_summary,
                "baseline_comparison": review_details.get("baseline_comparison", {}),
                "regression_status": review_details.get("regression_status", "NO_REGRESSION"),
                "test_results": {
                    "outcome": last_outcome,
                    "test_state": test_state_label,
                    "passed": test_passed,
                    "metrics": last_metrics,
                    "failure_details": review_details.get("test_failure_details", []),
                    "runs": runtime.test_results,
                },
                "code_review": {
                    "verdict": review_details.get("verdict"),
                    "review_state": review_details.get("review_state"),
                    "approved": review_approved,
                    "summary": review_details.get("summary", ""),
                    "task_alignment": review_details.get("task_alignment", ""),
                    "rejection_reasons": review_details.get("rejection_reasons", []),
                },
                "metrics": metrics,
            }
            try:
                (workspace / "evaluation.json").write_text(json.dumps(evaluation_report, indent=2, ensure_ascii=False), encoding="utf-8")
                (self.settings.runs_root / run_id / "evaluation.json").write_text(json.dumps(evaluation_report, indent=2, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass

            self.db.update_run(
                run_id,
                status="succeeded" if success else "failed",
                finished_at=utcnow(),
                duration_ms=duration_ms,
                success=int(success),
                total_tokens=usage_totals["total_tokens"],
                prompt_tokens=usage_totals["prompt_tokens"],
                completion_tokens=usage_totals["completion_tokens"],
                llm_calls=usage_totals["calls"],
                result_json=json.dumps(result, ensure_ascii=False),
                metrics_json=json.dumps(metrics),
                error=error_msg,
            )

            self.event(
                run_id,
                "verification_evaluated",
                f"Verification state: {verification_state}",
                {"verification_state": verification_state, "test_passed": test_passed, "review_approved": review_approved, "metrics": last_metrics}
            )

            self.event(
                run_id,
                "run_finished",
                "Run completed successfully" if success else f"Run finished ({verification_state}): {error_msg or 'Verification criteria not met'}",
                {"success": success, "verification_state": verification_state, "duration_ms": duration_ms, "total_tokens": usage_totals["total_tokens"], "llm_calls": usage_totals["calls"], "metrics": metrics}
            )

        except asyncio.CancelledError:
            self.db.update_run(run_id, status="cancelled", finished_at=utcnow(), error="Run cancelled by user.")
            self.event(run_id, "run_cancelled", "Run cancelled by user", {})
            raise
        except Exception as exc:
            duration_ms = round((time.perf_counter() - started) * 1000)
            usage_totals = locals().get("executor").usage if "executor" in locals() else usage_totals
            self.db.update_run(
                run_id,
                status="failed",
                finished_at=utcnow(),
                duration_ms=duration_ms,
                total_tokens=usage_totals.get("total_tokens", 0),
                prompt_tokens=usage_totals.get("prompt_tokens", 0),
                completion_tokens=usage_totals.get("completion_tokens", 0),
                llm_calls=usage_totals.get("calls", 0),
                error=f"{type(exc).__name__}: {exc}",
            )
            self.event(run_id, "run_error", "Execution failed", {"error": f"{type(exc).__name__}: {exc}"})
        finally:
            pass

    async def _changed_files(self, workspace: Path) -> list[str]:
        proc = await asyncio.create_subprocess_exec("git", "status", "--short", cwd=workspace,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, _ = await proc.communicate()
        return [line[3:].strip() for line in stdout.decode(errors="replace").splitlines() if len(line) > 3]
