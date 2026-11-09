import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from app.config import Settings
from app.repository import (
    ALLOWED_TEST_COMMANDS,
    ToolRuntime,
    WorkspaceError,
    is_allowed_test_command,
    parse_test_output,
    resolve_test_command,
)
from app.runner import (
    classify_changed_files,
    compute_verification_state,
    is_implementation_task,
    is_test_or_meta_file,
    parse_reviewer_verdict,
)


class VerificationPipelineTests(unittest.IsolatedAsyncioTestCase):
    def test_empty_test_collection(self):
        """Exit code 5 and no tests collected must yield NO_TESTS outcome."""
        stdout = "============================= test session starts ==============================\ncollected 0 items\n\n============================ no tests ran in 0.00s ============================="
        analysis = parse_test_output(["python", "-m", "pytest", "-q"], exit_code=5, stdout=stdout, stderr="")
        self.assertEqual(analysis["outcome"], "NO_TESTS")
        self.assertFalse(analysis["passed"])
        self.assertEqual(analysis["metrics"]["collected"], 0)
        self.assertEqual(analysis["metrics"]["passed"], 0)
        self.assertEqual(analysis["error_type"], "no_tests_collected")

    def test_successful_test_execution(self):
        """Passing test execution must yield PASS outcome with parsed metrics."""
        stdout = "============================= test session starts ==============================\ncollected 3 items\n\ntests/test_sample.py ... [100%]\n\n============================== 3 passed in 0.05s ==============================="
        analysis = parse_test_output(["python", "-m", "pytest", "-q"], exit_code=0, stdout=stdout, stderr="")
        self.assertEqual(analysis["outcome"], "PASS")
        self.assertTrue(analysis["passed"])
        self.assertEqual(analysis["metrics"]["collected"], 3)
        self.assertEqual(analysis["metrics"]["passed"], 3)
        self.assertEqual(analysis["metrics"]["failed"], 0)
        self.assertIsNone(analysis["error_type"])

    def test_failed_test_execution(self):
        """Failed test execution must yield FAIL outcome with failed metrics."""
        stdout = "============================= test session starts ==============================\ncollected 2 items\n\ntests/test_sample.py .F\n\n=================================== FAILURES ===================================\n_ test_fail _\nAssertionError\n========================= 1 failed, 1 passed in 0.12s =========================="
        analysis = parse_test_output(["python", "-m", "pytest", "-q"], exit_code=1, stdout=stdout, stderr="")
        self.assertEqual(analysis["outcome"], "FAIL")
        self.assertFalse(analysis["passed"])
        self.assertEqual(analysis["metrics"]["collected"], 2)
        self.assertEqual(analysis["metrics"]["passed"], 1)
        self.assertEqual(analysis["metrics"]["failed"], 1)
        self.assertEqual(analysis["error_type"], "test_failure")

    def test_missing_test_dependencies(self):
        """Missing dependencies must be distinguished from test failures as BLOCKED."""
        stderr = "Traceback (most recent call last):\n  File 'test.py', line 1\nModuleNotFoundError: No module named 'nonexistent_package'"
        analysis = parse_test_output(["python", "-m", "pytest", "-q"], exit_code=1, stdout="", stderr=stderr)
        self.assertEqual(analysis["outcome"], "BLOCKED")
        self.assertFalse(analysis["passed"])
        self.assertEqual(analysis["error_type"], "missing_dependency")
        self.assertIn("nonexistent_package", analysis["error_message"])

    def test_test_timeout(self):
        """Timed-out test commands must yield BLOCKED with timeout error."""
        analysis = parse_test_output(["python", "-m", "pytest", "-q"], exit_code=None, stdout="", stderr="", timed_out=True, timeout_seconds=10)
        self.assertEqual(analysis["outcome"], "BLOCKED")
        self.assertFalse(analysis["passed"])
        self.assertEqual(analysis["error_type"], "timeout")
        self.assertIn("timed out after 10s", analysis["error_message"])

    def test_incorrect_working_directory(self):
        """Attempts to access or resolve paths outside the workspace must be rejected."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings()
            runtime = ToolRuntime(root, settings, "auto")
            with self.assertRaises(WorkspaceError):
                runtime._safe_path("../../secret.txt")
            with self.assertRaises(WorkspaceError):
                runtime._safe_path("/etc/passwd")

            self.assertIsNone(resolve_test_command("pytest -q ../../secret.py", root))
            self.assertFalse(is_allowed_test_command("pytest -q ../outside.py"))

    # --- 10 Failure-Handling Regression Test Scenarios ---

    def test_scenario_1_one_failing_test_among_otherwise_passing(self):
        """Scenario 1: One failing test among otherwise passing tests (Event #940 pattern)."""
        stdout = (
            "...F\n"
            "=================================== FAILURES ===================================\n"
            "_ test_model_training_failure_empty_df _\n"
            "KeyError: 'Close'\n"
            "========================= 1 failed, 3 passed in 1.45s =========================="
        )
        test_res = parse_test_output(["python", "-m", "pytest", "-q"], exit_code=1, stdout=stdout, stderr="")
        self.assertEqual(test_res["outcome"], "FAIL")
        self.assertFalse(test_res["passed"])
        self.assertEqual(test_res["metrics"]["collected"], 4)
        self.assertEqual(test_res["metrics"]["passed"], 3)
        self.assertEqual(test_res["metrics"]["failed"], 1)
        self.assertIn("test_model_training_failure_empty_df", test_res["failure_details"])

        verdict, tp, ra, err, details = compute_verification_state([test_res], "VERDICT: APPROVE")
        self.assertEqual(verdict, "FAIL")
        self.assertFalse(tp)
        self.assertEqual(details["test_state"], "FAIL")
        self.assertIn("test_model_training_failure_empty_df", details["test_failure_details"])

    def test_scenario_2_pre_existing_test_failure_vs_new_regression(self):
        """Scenario 2: Pre-existing repository test failure vs new regression."""
        baseline_test = {
            "command": ["pytest"],
            "outcome": "FAIL",
            "passed": False,
            "metrics": {"collected": 5, "passed": 4, "failed": 1},
            "failure_details": ["tests/test_legacy.py::test_legacy_bug"],
        }
        # After agent changes: the legacy bug still fails, but no new failures and reviewer approves fix
        after_test = {
            "command": ["pytest"],
            "outcome": "FAIL",
            "passed": False,
            "metrics": {"collected": 6, "passed": 5, "failed": 1},
            "failure_details": ["tests/test_legacy.py::test_legacy_bug"],
        }
        review = "VERDICT: APPROVE\nSummary: Fix for target bug is verified and unrelated to legacy failure."
        verdict, tp, ra, err, details = compute_verification_state(
            [after_test], review, baseline_tests=[baseline_test]
        )
        self.assertEqual(verdict, "PARTIAL")
        self.assertFalse(tp)
        self.assertTrue(ra)
        self.assertEqual(details["regression_status"], "PRE_EXISTING_FAILURE")
        self.assertEqual(details["baseline_comparison"]["pre_existing_failures"], ["tests/test_legacy.py::test_legacy_bug"])
        self.assertEqual(details["baseline_comparison"]["new_failures"], [])

    def test_scenario_3_regression_introduced_by_coding_agent(self):
        """Scenario 3: Regression introduced by the Coding Agent."""
        baseline_test = {
            "command": ["pytest"],
            "outcome": "PASS",
            "passed": True,
            "metrics": {"collected": 5, "passed": 5, "failed": 0},
            "failure_details": [],
        }
        # After agent changes: a previously passing test is now failing
        after_test = {
            "command": ["pytest"],
            "outcome": "FAIL",
            "passed": False,
            "metrics": {"collected": 6, "passed": 5, "failed": 1},
            "failure_details": ["tests/test_auth.py::test_token_generation"],
        }
        review = "VERDICT: APPROVE"
        verdict, tp, ra, err, details = compute_verification_state(
            [after_test], review, baseline_tests=[baseline_test]
        )
        self.assertEqual(verdict, "FAIL")
        self.assertFalse(tp)
        self.assertEqual(details["regression_status"], "REGRESSION_DETECTED")
        self.assertIn("tests/test_auth.py::test_token_generation", details["baseline_comparison"]["new_failures"])

    def test_scenario_4_incomplete_bug_fix(self):
        """Scenario 4: Incomplete bug fix."""
        repro_test = {
            "command": ["pytest"],
            "outcome": "FAIL",
            "passed": False,
            "metrics": {"collected": 1, "passed": 0, "failed": 1},
            "failure_details": ["tests/test_fix.py::test_reproduce_bug"],
        }
        reject_review = "VERDICT: NEEDS_CHANGES\nREJECTION_REASONS:\n- Bug still occurs when input is empty"
        verdict, tp, ra, err, details = compute_verification_state([repro_test], reject_review)
        self.assertEqual(verdict, "FAIL")
        self.assertFalse(tp)
        self.assertFalse(ra)
        self.assertEqual(details["regression_status"], "INCOMPLETE_FIX")
        self.assertEqual(details["review_state"], "REJECTED")

    def test_scenario_5_reviewer_rejection(self):
        """Scenario 5: Reviewer rejection when tests pass."""
        pass_test = {
            "command": ["pytest"],
            "outcome": "PASS",
            "passed": True,
            "metrics": {"collected": 5, "passed": 5, "failed": 0},
            "failure_details": [],
        }
        reject_review = (
            "VERDICT: NEEDS_CHANGES\n"
            "REJECTION_REASONS:\n"
            "- Hardcoded return values rather than fixing algorithm\n"
            "- Removed existing assertion"
        )
        verdict, tp, ra, err, details = compute_verification_state([pass_test], reject_review)
        self.assertEqual(verdict, "PARTIAL")
        self.assertTrue(tp)
        self.assertFalse(ra)
        self.assertEqual(details["review_state"], "REJECTED")
        self.assertEqual(len(details["rejection_reasons"]), 2)
        self.assertIn("Hardcoded return values", err)

    def test_scenario_6_reviewer_invalid_structured_output(self):
        """Scenario 6: Reviewer response with invalid structured output."""
        # JSON missing the required "verdict" field
        invalid_json = json.dumps({"notes": "Looks interesting", "score": 8})
        parsed = parse_reviewer_verdict(invalid_json)
        self.assertEqual(parsed["review_state"], "INCONCLUSIVE")
        self.assertFalse(parsed["approved"])
        self.assertIn("missing 'verdict' field", parsed["rejection_reasons"][0])

        # Verification state with inconclusive review
        pass_test = {"command": ["pytest"], "outcome": "PASS", "passed": True, "metrics": {"collected": 2, "passed": 2}}
        verdict, tp, ra, err, details = compute_verification_state([pass_test], invalid_json)
        self.assertEqual(verdict, "PARTIAL")
        self.assertTrue(tp)
        self.assertFalse(ra)
        self.assertEqual(details["review_state"], "INCONCLUSIVE")

    def test_scenario_7_reviewer_approval_after_successful_correction(self):
        """Scenario 7: Reviewer approval after successful correction."""
        pass_test = {
            "command": ["pytest"],
            "outcome": "PASS",
            "passed": True,
            "metrics": {"collected": 5, "passed": 5, "failed": 0},
            "failure_details": [],
        }
        approval_json = json.dumps({
            "verdict": "APPROVE",
            "summary": "Verified defensive check against empty DataFrame with 5 passing tests.",
            "test_evidence_verified": True,
            "task_alignment": "Defensive check handles all empty DataFrame inputs.",
            "rejection_reasons": []
        })
        verdict, tp, ra, err, details = compute_verification_state([pass_test], approval_json)
        self.assertEqual(verdict, "PASS")
        self.assertTrue(tp)
        self.assertTrue(ra)
        self.assertEqual(details["review_state"], "APPROVED")
        self.assertEqual(details["test_state"], "PASS")
        self.assertIsNone(err)

    def test_scenario_8_missing_review_evidence(self):
        """Scenario 8: Missing review evidence / empty diff."""
        pass_test = {"command": ["pytest"], "outcome": "PASS", "passed": True, "metrics": {"collected": 1, "passed": 1}}
        blocked_review = "VERDICT: BLOCKED\nREJECTION_REASONS:\n- Missing review evidence: no diff provided."
        verdict, tp, ra, err, details = compute_verification_state([pass_test], blocked_review)
        self.assertEqual(verdict, "BLOCKED")
        self.assertFalse(ra)
        self.assertEqual(details["review_state"], "BLOCKED")
        self.assertIn("Missing review evidence", err)

    def test_scenario_9_blocked_test_environment(self):
        """Scenario 9: Blocked test environment due to dependency installation failure."""
        verdict, tp, ra, err, details = compute_verification_state(
            tests=[],
            reviewer_output="VERDICT: APPROVE",
            dependency_status="DEPENDENCY_INSTALL_BLOCKED",
            dependency_error="No network access to download packages",
        )
        self.assertEqual(verdict, "DEPENDENCY_INSTALL_BLOCKED")
        self.assertFalse(tp)
        self.assertFalse(ra)
        self.assertEqual(details["test_state"], "BLOCKED")
        self.assertIn("No network access", err)

    def test_scenario_10_verification_state_aggregation_matrix(self):
        """Scenario 10: Verification state aggregation matrix (5-state system)."""
        pass_test = [{"command": ["pytest"], "outcome": "PASS", "passed": True, "metrics": {"collected": 2, "passed": 2}}]
        fail_test = [{"command": ["pytest"], "outcome": "FAIL", "passed": False, "metrics": {"collected": 2, "passed": 1, "failed": 1}}]
        no_tests = [{"command": ["pytest"], "outcome": "NO_TESTS", "passed": False, "metrics": {"collected": 0, "passed": 0}}]
        blocked_test = [{"command": ["pytest"], "outcome": "BLOCKED", "passed": False, "metrics": {"collected": 0}}]

        approve = "VERDICT: APPROVE"
        reject = "VERDICT: NEEDS_CHANGES\nREJECTION_REASONS:\n- Incomplete fix"
        pending = "Agent reached its step limit before producing a final summary."
        inconclusive = json.dumps({"notes": "uncertain"})
        blocked_rev = "VERDICT: BLOCKED\nREJECTION_REASONS:\n- Diff is empty"

        # (PASS, APPROVE) -> PASS
        s, tp, ra, _, _ = compute_verification_state(pass_test, approve)
        self.assertEqual(s, "PASS")
        self.assertTrue(tp)
        self.assertTrue(ra)

        # (PASS, REJECT) -> PARTIAL
        s, tp, ra, _, _ = compute_verification_state(pass_test, reject)
        self.assertEqual(s, "PARTIAL")
        self.assertTrue(tp)
        self.assertFalse(ra)

        # (PASS, PENDING) -> PARTIAL
        s, tp, ra, _, _ = compute_verification_state(pass_test, pending)
        self.assertEqual(s, "PARTIAL")
        self.assertTrue(tp)
        self.assertFalse(ra)

        # (PASS, INCONCLUSIVE) -> PARTIAL
        s, tp, ra, _, _ = compute_verification_state(pass_test, inconclusive)
        self.assertEqual(s, "PARTIAL")
        self.assertTrue(tp)
        self.assertFalse(ra)

        # (PASS, BLOCKED) -> BLOCKED
        s, tp, ra, _, _ = compute_verification_state(pass_test, blocked_rev)
        self.assertEqual(s, "BLOCKED")
        self.assertFalse(ra)

        # (FAIL, APPROVE) -> FAIL
        s, tp, ra, _, _ = compute_verification_state(fail_test, approve)
        self.assertEqual(s, "FAIL")
        self.assertFalse(tp)

        # (FAIL, REJECT) -> FAIL
        s, tp, ra, _, _ = compute_verification_state(fail_test, reject)
        self.assertEqual(s, "FAIL")
        self.assertFalse(tp)

        # (NO_TESTS, APPROVE) -> NO_TESTS (Never treated as PASS)
        s, tp, ra, _, _ = compute_verification_state(no_tests, approve)
        self.assertEqual(s, "NO_TESTS")
        self.assertFalse(tp)

        # (BLOCKED, APPROVE) -> BLOCKED
        s, tp, ra, _, _ = compute_verification_state(blocked_test, approve)
        self.assertEqual(s, "BLOCKED")
        self.assertFalse(tp)

    async def test_live_tool_runtime_execution(self):
        """Verify ToolRuntime creates real test executions and parses results."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tests").mkdir()
            (root / "tests" / "test_ok.py").write_text("def test_ok(): assert True\n")
            settings = Settings()
            runtime = ToolRuntime(root, settings, "auto")

            # Run test
            record = await runtime.run_test_command(["python", "-m", "pytest", "-q", "tests/test_ok.py"], timeout=30)
            self.assertEqual(record["outcome"], "PASS")
            self.assertTrue(record["passed"])
            self.assertEqual(record["metrics"]["passed"], 1)

            # Test artifact preserved
            artifact = root / ".test_last_result.json"
            self.assertTrue(artifact.exists())
            data = json.loads(artifact.read_text())
            self.assertEqual(data["outcome"], "PASS")

    def test_file_classification_production_vs_test(self):
        """Production code files must be distinguished from test and metadata files."""
        files = [
            "app/ml_service.py",
            "app/config.py",
            "src/utils.js",
            "tests/test_ml_service.py",
            ".test_last_result.json",
            ".venv",
            "models/stock_predictor.pkl",
            "app/__pycache__/ml_service.cpython-313.pyc",
            "evaluation.json",
        ]
        prod_files, test_meta = classify_changed_files(files)
        self.assertEqual(prod_files, ["app/ml_service.py", "app/config.py", "src/utils.js"])
        self.assertIn("tests/test_ml_service.py", test_meta)
        self.assertIn(".venv", test_meta)
        self.assertIn("models/stock_predictor.pkl", test_meta)

    def test_implementation_task_detection(self):
        """Tasks requesting bug fixes or implementations must be flagged as requiring production changes."""
        self.assertTrue(is_implementation_task("Find and fix a reproducible bug."))
        self.assertTrue(is_implementation_task("Implement user authentication feature"))
        self.assertTrue(is_implementation_task("Resolve recursive forecasting feature drift"))
        self.assertFalse(is_implementation_task("Update README with project documentation"))

    def test_scenario_task_not_implemented_rejection(self):
        """When implementation_status is NOT_IMPLEMENTED, review is rejected even if tests pass."""
        pass_test = [{"command": ["pytest"], "outcome": "PASS", "passed": True, "metrics": {"collected": 4, "passed": 4}}]
        approve_review = "VERDICT: APPROVE\nSummary: Tests passed."
        verdict, tp, ra, err, details = compute_verification_state(
            pass_test,
            approve_review,
            implementation_status="NOT_IMPLEMENTED"
        )
        self.assertEqual(verdict, "PARTIAL")
        self.assertTrue(tp)
        self.assertFalse(ra)
        self.assertEqual(details["review_state"], "REJECTED")
        self.assertEqual(details["regression_status"], "NOT_IMPLEMENTED")
        self.assertIn("No production code changes were made", details["rejection_reasons"][0])

    def test_scenario_failure_to_pass_evidence_recorded(self):
        """Failure-to-pass status must be recorded in baseline_comparison."""
        pass_test = [{"command": ["pytest"], "outcome": "PASS", "passed": True, "metrics": {"collected": 4, "passed": 4}}]
        approve_review = "VERDICT: APPROVE"
        verdict, tp, ra, err, details = compute_verification_state(
            pass_test,
            approve_review,
            implementation_status="IMPLEMENTED",
            failure_to_pass_verified=True,
            reproduced_on_baseline=True,
        )
        self.assertEqual(verdict, "PASS")
        self.assertTrue(tp)
        self.assertTrue(ra)
        self.assertTrue(details["baseline_comparison"]["failure_to_pass_verified"])
        self.assertTrue(details["baseline_comparison"]["reproduced_on_baseline"])


if __name__ == "__main__":
    unittest.main()
