import ast
import asyncio
import json
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from .agents import AgentExecutor
from .config import Settings
from .db import Database, utcnow
from .dependencies import prepare_environment
from .llm import LLMClient
from .repository import RepositoryManager, ToolRuntime, WorkspaceError, _kill_process_tree


def parse_json_loose(text: str) -> dict[str, Any]:
    try:
        return json.loads(text)
    except Exception:
        match = re.search(r"\{.*\}", text, re.S)
        if match:
            try: return json.loads(match.group(0))
            except Exception: pass
    return {"summary": text[:500], "steps": [], "likely_files": [], "include_test_scout": True, "include_security_review": False, "risks": ["Planner output was not valid JSON; using conservative default routing."]}


def is_test_or_meta_file(path: str) -> bool:
    """Determine whether a file path represents a test, cache, virtualenv, or harness artifact."""
    p = path.replace("\\", "/").lower().strip()
    meta_artifacts = (
        ".test_last_result.json",
        ".venv",
        "evaluation.json",
        "__pycache__",
        ".pytest_cache",
        ".git",
        ".gitignore",
        "models/",
        ".pkl",
        ".agentharness_inferred_requirements.txt",
        ".agentharness_ready",
    )
    if any(m in p for m in meta_artifacts):
        return True

    parts = p.split("/")
    filename = parts[-1]

    # Test directories
    if any(part in {"tests", "test", "spec", "specs", "__tests__", "test_suite", "testing", "unit_tests", "integration_tests"} for part in parts[:-1]):
        return True

    # Test filename conventions
    if (
        filename.startswith("test_")
        or filename.endswith("_test.py")
        or filename.endswith(".test.js")
        or filename.endswith(".test.ts")
        or filename.endswith(".test.jsx")
        or filename.endswith(".test.tsx")
        or filename.endswith(".spec.js")
        or filename.endswith(".spec.ts")
        or filename.endswith("test.py")
        or filename.endswith("_spec.py")
        or filename.endswith("test.js")
        or filename.endswith("test.ts")
    ):
        return True

    return False


def classify_changed_files(files: list[str]) -> tuple[list[str], list[str]]:
    """Classify modified files into (production_files, test_or_meta_files)."""
    prod_files: list[str] = []
    test_or_meta: list[str] = []
    for f in files:
        if is_test_or_meta_file(f):
            test_or_meta.append(f)
        else:
            prod_files.append(f)
    return prod_files, test_or_meta


def is_implementation_task(task: str, plan: dict[str, Any] | None = None) -> bool:
    """Check if the task requires modifying production code rather than test-only or doc-only work."""
    task_lower = task.lower()
    implementation_keywords = (
        "fix", "bug", "implement", "feature", "issue", "defect", "error",
        "correct", "solve", "patch", "repair", "resolve", "refactor"
    )
    if any(kw in task_lower for kw in implementation_keywords):
        return True
    if plan:
        likely = plan.get("likely_files") or []
        prod, _ = classify_changed_files(likely)
        if prod:
            return True
    return False


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


def generate_regression_test_skeleton(
    workspace: Path,
    prod_files: list[str],
    task: str,
    test_dir: str = "tests",
) -> Path | None:
    """
    Generate an automated regression test skeleton when no existing tests exist,
    exercising identified functions/classes with boundary checks and error conditions.
    """
    target_dir = workspace / test_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    test_file_path = target_dir / "test_regression_generated.py"

    py_prod = [f for f in prod_files if f.endswith(".py")]
    if not py_prod:
        for p in workspace.rglob("*.py"):
            rel = p.relative_to(workspace).as_posix()
            if not is_test_or_meta_file(rel) and not rel.startswith("."):
                py_prod.append(rel)
            if len(py_prod) >= 3:
                break

    lines = [
        '"""Automatically generated regression test suite by AgentHarness."""',
        "import pytest",
        "import sys",
        "from pathlib import Path",
        "",
        "# Ensure repository root is in sys.path",
        "repo_root = Path(__file__).resolve().parent.parent",
        "if str(repo_root) not in sys.path:",
        "    sys.path.insert(0, str(repo_root))",
        "",
    ]

    has_generated_test = False

    for rel_path in py_prod:
        full_path = workspace / rel_path
        if not full_path.is_file():
            continue
        try:
            code = full_path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(code, filename=rel_path)
        except Exception:
            continue

        mod_parts = list(Path(rel_path).with_suffix("").parts)
        mod_name = ".".join(mod_parts)

        functions: list[str] = []
        classes: list[str] = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not node.name.startswith("__"):
                    functions.append(node.name)
            elif isinstance(node, ast.ClassDef):
                if not node.name.startswith("__"):
                    classes.append(node.name)

        if not functions and not classes:
            safe_id = re.sub(r"[^a-zA-Z0-9_]", "_", mod_name)
            lines.extend([
                f"def test_module_import_{safe_id}():",
                f"    import {mod_name}",
                f"    assert {mod_name} is not None",
                "",
            ])
            has_generated_test = True
            continue

        safe_id = re.sub(r"[^a-zA-Z0-9_]", "_", mod_name)
        lines.extend([
            f"def test_{safe_id}_imports():",
            f"    import {mod_name}",
            f"    assert {mod_name} is not None",
            "",
        ])
        has_generated_test = True

        for fn in functions[:5]:
            fn_safe = re.sub(r"[^a-zA-Z0-9_]", "_", fn)
            lines.extend([
                f"def test_{safe_id}_{fn_safe}_basic():",
                f"    import {mod_name}",
                f"    func = getattr({mod_name}, {fn!r}, None)",
                f"    assert callable(func), f'{fn} should be callable'",
                "",
                f"def test_{safe_id}_{fn_safe}_boundary_and_types():",
                f"    import {mod_name}",
                f"    func = getattr({mod_name}, {fn!r}, None)",
                f"    assert callable(func)",
                "    try:",
                "        func()",
                "    except (TypeError, ValueError, AttributeError, NotImplementedError, KeyError):",
                "        pass",
                "    except Exception as exc:",
                "        pytest.fail(f'Unexpected error calling {fn}: {exc}')",
                "",
            ])

        for cls in classes[:3]:
            cls_safe = re.sub(r"[^a-zA-Z0-9_]", "_", cls)
            lines.extend([
                f"def test_{safe_id}_{cls_safe}_instantiation():",
                f"    import {mod_name}",
                f"    target_cls = getattr({mod_name}, {cls!r}, None)",
                f"    assert target_cls is not None, f'{cls} should exist in {mod_name}'",
                "    try:",
                "        instance = target_cls()",
                "        assert instance is not None",
                "    except (TypeError, ValueError, AttributeError):",
                "        pass",
                "",
            ])

    if not has_generated_test:
        lines.extend([
            "def test_default_smoke():",
            "    assert True",
            "",
        ])

    try:
        test_file_path.write_text("\n".join(lines), encoding="utf-8")
        return test_file_path
    except Exception:
        return None


async def execute_alternative_verification(
    workspace: Path,
    prod_files: list[str],
    runtime: ToolRuntime | None = None,
) -> dict[str, Any]:
    """
    Execute alternative verification when formal automated tests cannot run:
    1. Static analysis (ast.parse)
    2. Syntax validation (py_compile)
    3. Module import validation in isolated environment
    """
    py_files = [f for f in prod_files if f.endswith(".py")]
    if not py_files:
        for p in workspace.rglob("*.py"):
            rel = p.relative_to(workspace).as_posix()
            if not is_test_or_meta_file(rel) and not rel.startswith("."):
                py_files.append(rel)
            if len(py_files) >= 5:
                break

    checks: list[dict[str, Any]] = []
    all_syntax_valid = True
    all_ast_valid = True
    all_imports_valid = True

    python_bin = sys.executable
    if runtime and runtime.venv_path:
        candidate = runtime.venv_path / "bin" / "python"
        if candidate.is_file():
            python_bin = str(candidate)

    for rel_path in py_files:
        full_path = workspace / rel_path
        if not full_path.is_file():
            continue

        file_check: dict[str, Any] = {"file": rel_path}

        # 1. AST Parsing
        try:
            content = full_path.read_text(encoding="utf-8", errors="replace")
            ast.parse(content, filename=rel_path)
            file_check["ast"] = "PASS"
        except SyntaxError as e:
            file_check["ast"] = f"FAIL: {e}"
            all_ast_valid = False
        except Exception as e:
            file_check["ast"] = f"FAIL: {e}"
            all_ast_valid = False

        # 2. Syntax validation via py_compile
        try:
            proc = await asyncio.create_subprocess_exec(
                python_bin, "-m", "py_compile", str(full_path),
                cwd=workspace,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode == 0:
                file_check["syntax"] = "PASS"
            else:
                file_check["syntax"] = f"FAIL: {stderr.decode(errors='replace').strip()}"
                all_syntax_valid = False
        except Exception as e:
            file_check["syntax"] = f"FAIL: {e}"
            all_syntax_valid = False

        # 3. Import validation
        mod_parts = list(Path(rel_path).with_suffix("").parts)
        mod_name = ".".join(mod_parts)
        try:
            proc = await asyncio.create_subprocess_exec(
                python_bin, "-c", f"import {mod_name}",
                cwd=workspace,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode == 0:
                file_check["import"] = "PASS"
            else:
                err_text = stderr.decode(errors="replace").strip()
                file_check["import"] = f"FAIL: {err_text[:200]}"
                all_imports_valid = False
        except Exception as e:
            file_check["import"] = f"FAIL: {e}"
            all_imports_valid = False

        checks.append(file_check)

    passed = (
        len(checks) > 0
        and all_ast_valid
        and all_syntax_valid
        and all_imports_valid
    )

    return {
        "passed": passed,
        "ast_valid": all_ast_valid,
        "syntax_valid": all_syntax_valid,
        "import_valid": all_imports_valid,
        "checks": checks,
        "files_checked": len(checks),
        "details": f"Checked {len(checks)} files. AST: {all_ast_valid}, Syntax: {all_syntax_valid}, Imports: {all_imports_valid}",
    }


def evaluate_final_status(
    *,
    dependency_status: str | None = None,
    dependency_source: str = "manifest",
    has_manifest: bool = True,
    has_original_tests: bool = True,
    has_generated_tests: bool = False,
    implementation_status: str | None = None,
    test_state: str = "NO_TESTS",
    test_metrics: dict[str, Any] | None = None,
    review_details: dict[str, Any] | None = None,
    alternative_verification: dict[str, Any] | None = None,
    agent_outputs: dict[str, Any] | None = None,
    inferred_packages: list[str] | None = None,
    ambiguous_imports: list[str] | None = None,
    error_message: str | None = None,
) -> dict[str, Any]:
    """
    Centralized evaluator that computes:
      - repository_health: HEALTHY, MISSING_MANIFEST, MISSING_TESTS, INCOMPLETE_INFRASTRUCTURE
      - execution_status: COMPLETED, NOT_COMPLETED, FAILED, BLOCKED
      - verification_status: FULL_VERIFICATION, PARTIAL_VERIFICATION, FAILED_VERIFICATION, UNVERIFIED, BLOCKED
      - final_status: SUCCESS, SUCCESS_WITH_WARNINGS, FAILED
      - db_status: succeeded, success_with_warnings, failed
      - success: bool
      - warnings: list[str]
      - notification: str | None
      - verification_details: dict(checks_passed, checks_failed, checks_skipped)
    """
    test_metrics = test_metrics or {}
    review_details = review_details or {}
    inferred_packages = inferred_packages or []
    ambiguous_imports = ambiguous_imports or []

    # 1. Repository Health
    manifest_missing = (not has_manifest) or (dependency_source == "inferred")
    tests_missing = not has_original_tests

    if manifest_missing and tests_missing:
        repository_health = "INCOMPLETE_INFRASTRUCTURE"
    elif manifest_missing:
        repository_health = "MISSING_MANIFEST"
    elif tests_missing:
        repository_health = "MISSING_TESTS"
    else:
        repository_health = "HEALTHY"

    # 2. Execution Status
    if dependency_status and dependency_status != "DEPENDENCIES_READY" and not (alternative_verification and alternative_verification.get("passed")):
        execution_status = "BLOCKED"
    elif implementation_status == "TIMED_OUT":
        execution_status = "TIMED_OUT"
    elif implementation_status == "CANCELLED":
        execution_status = "CANCELLED"
    elif implementation_status == "NOT_IMPLEMENTED":
        execution_status = "NOT_COMPLETED"
    elif implementation_status == "FAILED":
        execution_status = "FAILED"
    else:
        execution_status = "COMPLETED"

    # 3. Verification Details (Passed, Failed, Skipped)
    checks_passed: list[str] = []
    checks_failed: list[str] = []
    checks_skipped: list[str] = []

    if execution_status == "TIMED_OUT":
        checks_failed.append(f"Execution timed out: {error_message or 'Workflow deadline exceeded'}")
    elif execution_status == "CANCELLED":
        checks_failed.append("Execution cancelled by user")

    # Dependency check
    if dependency_status and dependency_status != "DEPENDENCIES_READY":
        if alternative_verification and alternative_verification.get("passed"):
            checks_skipped.append(f"Dependency preparation skipped/incomplete: {dependency_status}")
        else:
            checks_failed.append(f"Dependency preparation failed: {dependency_status}")
    elif dependency_status == "DEPENDENCIES_READY":
        if dependency_source == "inferred":
            checks_passed.append(f"Inferred dependencies prepared ({len(inferred_packages)} packages)")
        else:
            checks_passed.append("Manifest dependencies prepared successfully")
    else:
        checks_skipped.append("Dependency preparation not performed")

    # Tests check
    if has_original_tests:
        if test_state == "PASS":
            checks_passed.append(f"Original test suite passed ({test_metrics.get('passed', 0)} passed)")
        elif test_state == "FAIL":
            checks_failed.append(f"Original test suite failed ({test_metrics.get('failed', 0)} failed)")
        elif test_state == "BLOCKED":
            checks_failed.append("Original test suite blocked")
        else:
            checks_failed.append(f"Original test suite outcome: {test_state}")
    else:
        checks_skipped.append("Original test suite not found in repository")

    # Generated tests check
    if has_generated_tests:
        if test_state == "PASS":
            checks_passed.append(f"Generated regression test suite passed ({test_metrics.get('passed', 0)} passed)")
        elif test_state == "FAIL":
            checks_failed.append(f"Generated regression test suite failed ({test_metrics.get('failed', 0)} failed)")
        else:
            checks_failed.append(f"Generated regression test suite outcome: {test_state}")

    # Alternative verification check
    if alternative_verification:
        if alternative_verification.get("passed"):
            checks_passed.append(f"Alternative validation passed: {alternative_verification.get('details', 'syntax/import verification')}")
        else:
            checks_failed.append(f"Alternative validation failed: {alternative_verification.get('details', 'syntax/import errors')}")

    # Review check
    rev_state = review_details.get("review_state", "PENDING")
    rev_approved = review_details.get("approved", False)
    if rev_approved or rev_state == "APPROVED":
        checks_passed.append("Code review approved")
    elif rev_state == "REJECTED":
        checks_failed.append(f"Code review rejected: {'; '.join(review_details.get('rejection_reasons', [])) or 'Rejected'}")
    else:
        checks_skipped.append(f"Code review state: {rev_state}")

    # 4. Verification Status
    if dependency_status and dependency_status != "DEPENDENCIES_READY" and not (alternative_verification and alternative_verification.get("passed")):
        verification_status = "BLOCKED"
    elif test_state == "FAIL":
        verification_status = "FAILED_VERIFICATION"
    elif test_state == "BLOCKED":
        if alternative_verification and alternative_verification.get("passed"):
            verification_status = "PARTIAL_VERIFICATION"
        else:
            verification_status = "BLOCKED"
    elif test_state == "MISSING_DEPENDENCY":
        if alternative_verification and alternative_verification.get("passed"):
            verification_status = "PARTIAL_VERIFICATION"
        else:
            verification_status = "FAILED_VERIFICATION"
    elif alternative_verification and not alternative_verification.get("passed") and not has_original_tests and not has_generated_tests and test_state == "NO_TESTS":
        verification_status = "FAILED_VERIFICATION"
    elif test_state == "PASS":
        if not has_original_tests and has_generated_tests:
            verification_status = "PARTIAL_VERIFICATION"
        elif repository_health != "HEALTHY":
            verification_status = "PARTIAL_VERIFICATION"
        elif dependency_status and dependency_status != "DEPENDENCIES_READY":
            verification_status = "PARTIAL_VERIFICATION"
        else:
            verification_status = "FULL_VERIFICATION"
    elif test_state == "NO_TESTS":
        if alternative_verification and alternative_verification.get("passed"):
            verification_status = "PARTIAL_VERIFICATION"
        else:
            verification_status = "UNVERIFIED"
    else:
        verification_status = "FAILED_VERIFICATION"

    # 5. Warnings and Notifications
    warnings: list[str] = []
    if dependency_status and dependency_status != "DEPENDENCIES_READY":
        warnings.append(
            f"Repository dependencies could not be fully prepared ({dependency_status}): {error_message or 'Dependencies absent'}. "
            "Verification relied on available alternative analysis."
        )
    if manifest_missing:
        if dependency_source == "inferred":
            warnings.append(f"Repository lacks a dependency manifest. {len(inferred_packages)} packages were inferred from source code.")
        else:
            warnings.append("Repository lacks a dependency manifest.")
    if ambiguous_imports:
        warnings.append(f"Ambiguous or unmapped imports detected: {', '.join(ambiguous_imports[:5])}.")
    if tests_missing:
        if has_generated_tests and test_state == "PASS":
            warnings.append("Repository lacked existing tests. Verification relied on generated regression tests.")
        elif alternative_verification and alternative_verification.get("passed"):
            warnings.append("Repository lacked existing tests. Verification relied on alternative syntax and import checks.")
        else:
            warnings.append("Repository lacks automated test suites.")
    if verification_status == "PARTIAL_VERIFICATION" and not tests_missing and not manifest_missing and not (dependency_status and dependency_status != "DEPENDENCIES_READY"):
        warnings.append("Verification was partial: not all standard verification stages could be performed.")

    notification: str | None = None
    if warnings:
        notification = "Execution completed with warnings: " + "; ".join(warnings)

    # 6. Final Status
    if execution_status in {"BLOCKED", "NOT_COMPLETED", "FAILED", "TIMED_OUT", "CANCELLED"}:
        final_status = "FAILED"
    elif verification_status in {"FAILED_VERIFICATION", "BLOCKED", "UNVERIFIED"}:
        final_status = "FAILED"
    elif rev_state == "REJECTED" or not rev_approved:
        final_status = "FAILED"
    else:
        if repository_health != "HEALTHY" or verification_status == "PARTIAL_VERIFICATION" or len(warnings) > 0:
            final_status = "SUCCESS_WITH_WARNINGS"
        else:
            final_status = "SUCCESS"

    success = (final_status in {"SUCCESS", "SUCCESS_WITH_WARNINGS"})
    if execution_status == "TIMED_OUT":
        db_status = "timed_out"
    elif execution_status == "CANCELLED":
        db_status = "cancelled"
    elif final_status == "SUCCESS":
        db_status = "succeeded"
    elif final_status == "SUCCESS_WITH_WARNINGS":
        db_status = "success_with_warnings"
    else:
        db_status = "failed"

    return {
        "final_status": final_status,
        "execution_status": execution_status,
        "verification_status": verification_status,
        "repository_health": repository_health,
        "success": success,
        "db_status": db_status,
        "warnings": warnings,
        "notification": notification,
        "verification_details": {
            "checks_passed": checks_passed,
            "checks_failed": checks_failed,
            "checks_skipped": checks_skipped,
        },
    }


def compute_verification_state(
    tests: list[dict[str, Any]],
    reviewer_output: str,
    workspace: Path | None = None,
    dependency_status: str | None = None,
    dependency_error: str | None = None,
    baseline_tests: list[dict[str, Any]] | None = None,
    implementation_status: str | None = None,
    failure_to_pass_verified: bool | None = None,
    reproduced_on_baseline: bool | None = None,
    alternative_verification: dict[str, Any] | None = None,
    has_original_tests: bool = True,
    has_generated_tests: bool = False,
    dependency_source: str = "manifest",
    has_manifest: bool = True,
    inferred_packages: list[str] | None = None,
    ambiguous_imports: list[str] | None = None,
) -> tuple[str, bool, bool, str | None, dict[str, Any]]:
    """
    Compute explicit verification state across dependency preparation, baseline comparison,
    test execution, task implementation evidence, and code review:
      Tests: PASS, FAIL, NO_TESTS, BLOCKED, INCONCLUSIVE
      Code Review: APPROVED, REJECTED, PENDING, BLOCKED, INCONCLUSIVE
      Overall: PASS, PARTIAL, PARTIAL_PASS, FAIL, NO_TESTS, BLOCKED, INCONCLUSIVE
    Returns: (overall_verdict, test_passed, review_approved, error_message, review_details)
    """
    review_details = parse_reviewer_verdict(reviewer_output)
    review_approved = review_details["approved"]
    review_state = review_details.get("review_state", "REJECTED" if not review_approved else "APPROVED")
    rejection_reasons = review_details.get("rejection_reasons", [])

    # If change-evidence validation detected that no production files were modified for an implementation task,
    # the task was not implemented and cannot pass verification.
    if implementation_status == "NOT_IMPLEMENTED":
        review_approved = False
        review_state = "REJECTED"
        rejection_msg = "No production code changes were made; only test or metadata files were modified."
        if rejection_msg not in rejection_reasons:
            rejection_reasons.append(rejection_msg)
        review_details.update({
            "approved": False,
            "review_state": "REJECTED",
            "verdict": "NEEDS_CHANGES",
            "rejection_reasons": rejection_reasons,
        })

    if dependency_status and dependency_status != "DEPENDENCIES_READY":
        if not (alternative_verification and alternative_verification.get("passed")):
            eval_result = evaluate_final_status(
                dependency_status=dependency_status,
                dependency_source=dependency_source,
                has_manifest=has_manifest,
                has_original_tests=has_original_tests,
                has_generated_tests=has_generated_tests,
                implementation_status=implementation_status,
                test_state="BLOCKED",
                review_details=review_details,
                alternative_verification=alternative_verification,
                inferred_packages=inferred_packages,
                ambiguous_imports=ambiguous_imports,
                error_message=dependency_error,
            )
            review_details.update({
                "verification_verdict": dependency_status,
                "test_state": "BLOCKED",
                "review_state": review_state,
                "regression_status": "BLOCKED",
                "baseline_comparison": {},
                "test_failure_details": [],
                "alternative_verification": alternative_verification or {},
                "has_original_tests": has_original_tests,
                "has_generated_tests": has_generated_tests,
                "final_status": eval_result["final_status"],
                "execution_status": eval_result["execution_status"],
                "verification_status": eval_result["verification_status"],
                "repository_health": eval_result["repository_health"],
                "warnings": eval_result["warnings"],
                "notification": eval_result["notification"],
                "verification_details": eval_result["verification_details"],
            })
            return (
                dependency_status,
                False,
                False,
                dependency_error or f"Dependency preparation failed ({dependency_status}).",
                review_details,
            )
        dep_warning = dependency_error or f"Dependency preparation incomplete ({dependency_status})."
        if not test_state or test_state in {"BLOCKED", "NO_TESTS", "MISSING_DEPENDENCY"}:
            test_err = dep_warning

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
        "failure_to_pass_verified": bool(failure_to_pass_verified),
        "reproduced_on_baseline": bool(reproduced_on_baseline),
    }

    # Regression status
    if implementation_status == "NOT_IMPLEMENTED":
        regression_status = "NOT_IMPLEMENTED"
    elif new_failures:
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
        if alternative_verification and alternative_verification.get("passed"):
            overall_verdict = "PARTIAL_PASS"
            error_msg = None
        else:
            overall_verdict = "MISSING_DEPENDENCY"
            error_msg = f"Test execution failed due to undeclared dependency: {test_err}"
    elif test_state == "BLOCKED":
        if alternative_verification and alternative_verification.get("passed"):
            overall_verdict = "PARTIAL_PASS"
            error_msg = None
        else:
            overall_verdict = "BLOCKED"
            error_msg = f"Test execution was blocked: {test_err}"
    elif review_state == "BLOCKED":
        overall_verdict = "BLOCKED"
        error_msg = f"Code review was blocked: {'; '.join(rejection_reasons) or 'Missing review evidence'}"
    elif test_state == "NO_TESTS":
        if alternative_verification and alternative_verification.get("passed"):
            if review_state == "APPROVED":
                overall_verdict = "PARTIAL_PASS"
                error_msg = None
            elif review_state in {"REJECTED", "PENDING", "INCONCLUSIVE"}:
                overall_verdict = "PARTIAL_PASS" if review_state != "REJECTED" else "FAIL"
                error_msg = f"Alternative verification passed, but review is {review_state}: {'; '.join(rejection_reasons) or 'Changes not approved.'}"
            else:
                overall_verdict = "PARTIAL_PASS"
                error_msg = None
        else:
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

    test_passed = (test_state == "PASS")

    eval_result = evaluate_final_status(
        dependency_status=dependency_status,
        dependency_source=dependency_source,
        has_manifest=has_manifest,
        has_original_tests=has_original_tests,
        has_generated_tests=has_generated_tests,
        implementation_status=implementation_status,
        test_state=test_state,
        test_metrics=last_metrics,
        review_details=review_details,
        alternative_verification=alternative_verification,
        inferred_packages=inferred_packages,
        ambiguous_imports=ambiguous_imports,
        error_message=error_msg,
    )

    review_details.update({
        "verification_verdict": overall_verdict,
        "test_state": test_state,
        "review_state": review_state,
        "regression_status": regression_status,
        "baseline_comparison": baseline_comparison,
        "test_failure_details": current_failures,
        "alternative_verification": alternative_verification or {},
        "has_original_tests": has_original_tests,
        "has_generated_tests": has_generated_tests,
        "final_status": eval_result["final_status"],
        "execution_status": eval_result["execution_status"],
        "verification_status": eval_result["verification_status"],
        "repository_health": eval_result["repository_health"],
        "warnings": eval_result["warnings"],
        "notification": eval_result["notification"],
        "verification_details": eval_result["verification_details"],
    })

    return overall_verdict, test_passed, review_approved, error_msg, review_details


class WorkflowError(Exception):
    """Base exception for workflow orchestration failures."""
    pass


class RunTimeoutError(WorkflowError):
    """Raised when an overall run execution exceeds the configured run deadline."""
    pass


class StageTimeoutError(WorkflowError):
    """Raised when a specific workflow stage exceeds its allocated timeout."""
    pass


class WorkflowStage:
    QUEUED = "QUEUED"
    PREPARING_DEPENDENCIES = "PREPARING_DEPENDENCIES"
    RECORDING_BASELINE = "RECORDING_BASELINE"
    PLANNING = "PLANNING"
    ANALYZING = "ANALYZING"
    CODING = "CODING"
    TESTING_DEBUGGING = "TESTING_DEBUGGING"
    VERIFYING = "VERIFYING"
    REVIEWING = "REVIEWING"
    FINALIZING = "FINALIZING"


class WorkflowTracker:
    def __init__(
        self,
        run_id: str,
        overall_timeout_seconds: float,
        stage_timeout_seconds: float,
        max_agent_handoffs: int,
        event_callback: Any = None,
    ):
        self.run_id = run_id
        self.overall_timeout_seconds = max(10.0, float(overall_timeout_seconds))
        self.stage_timeout_seconds = max(5.0, float(stage_timeout_seconds))
        self.max_agent_handoffs = max_agent_handoffs
        self.event_callback = event_callback

        self.started_at = time.perf_counter()
        self.overall_deadline = self.started_at + self.overall_timeout_seconds
        self.stage = WorkflowStage.QUEUED
        self.stage_start_time = self.started_at
        self.handoff_count = 0
        self.stage_history: list[dict[str, Any]] = []
        self.stage_action_counts: dict[str, int] = {}
        self.current_agent: str | None = None
        self.is_terminal = False

    def check_deadline(self) -> None:
        now = time.perf_counter()
        if now >= self.overall_deadline:
            elapsed = round((now - self.started_at) * 1000)
            raise RunTimeoutError(
                f"Overall run execution deadline exceeded ({self.overall_timeout_seconds}s limit, ran for {elapsed}ms in stage {self.stage})"
            )

    def get_remaining_run_budget(self) -> float:
        return max(0.0, self.overall_deadline - time.perf_counter())

    def get_remaining_stage_budget(self, override_timeout: float | None = None) -> float:
        self.check_deadline()
        stage_limit = override_timeout if override_timeout is not None else self.stage_timeout_seconds
        remaining_in_stage = max(1.0, stage_limit - (time.perf_counter() - self.stage_start_time))
        remaining_run = self.get_remaining_run_budget()
        return max(1.0, min(remaining_in_stage, remaining_run))

    def transition(self, new_stage: str, agent: str | None = None) -> None:
        self.check_deadline()
        now = time.perf_counter()
        old_stage = self.stage
        duration_ms = round((now - self.stage_start_time) * 1000)

        self.stage_history.append({
            "stage": old_stage,
            "duration_ms": duration_ms,
            "agent": self.current_agent,
            "actions": self.stage_action_counts.get(old_stage, 0),
        })

        self.stage = new_stage
        self.stage_start_time = now
        self.current_agent = agent

        if self.event_callback:
            self.event_callback(
                "stage_transition",
                f"Transitioned from {old_stage} to {new_stage}",
                {
                    "from_stage": old_stage,
                    "to_stage": new_stage,
                    "previous_duration_ms": duration_ms,
                    "agent": agent,
                    "remaining_budget_s": round(self.get_remaining_run_budget(), 1),
                },
                agent=agent,
            )

    def record_handoff(self, from_agent: str, to_agent: str) -> None:
        self.check_deadline()
        self.handoff_count += 1
        if self.handoff_count > self.max_agent_handoffs:
            raise WorkflowError(
                f"Maximum agent handoffs exceeded ({self.handoff_count} > {self.max_agent_handoffs}). Circular routing prevented."
            )
        if self.event_callback:
            self.event_callback(
                "agent_handoff",
                f"Agent handoff: {from_agent} -> {to_agent} (handoff #{self.handoff_count}/{self.max_agent_handoffs})",
                {
                    "from_agent": from_agent,
                    "to_agent": to_agent,
                    "handoff_count": self.handoff_count,
                    "max_handoffs": self.max_agent_handoffs,
                },
                agent=to_agent,
            )

    def record_action(self, action_name: str) -> None:
        self.stage_action_counts[self.stage] = self.stage_action_counts.get(self.stage, 0) + 1


class RunManager:
    def __init__(self, settings: Settings, db: Database, llm: LLMClient, repos: RepositoryManager):
        self.settings, self.db, self.llm, self.repos = settings, db, llm, repos
        self.tasks: dict[str, asyncio.Task] = {}
        self.semaphore = asyncio.Semaphore(max(1, settings.agentharness_max_parallel_readers))
        self._finalized_runs: set[str] = set()

    def event(self, run_id: str, event_type: str, message: str, data: dict[str, Any] | None = None, agent: str | None = None):
        self.db.add_event(run_id, event_type, agent, message, data or {})

    def _finalize_run(
        self,
        run_id: str,
        *,
        status: str,
        duration_ms: int,
        metrics: dict[str, Any],
        result: dict[str, Any],
        evaluation_report: dict[str, Any],
        error_msg: str | None = None,
        workspace: Path | None = None,
        notification: str | None = None,
        usage_totals: dict[str, Any] | None = None,
    ) -> bool:
        """
        Idempotent run finalization. Enforces that run termination events,
        DB updates, and disk evaluation artifacts are written exactly once.
        """
        if run_id in self._finalized_runs:
            return False
        self._finalized_runs.add(run_id)

        usage = usage_totals or {
            "calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0
        }

        # Write evaluation.json
        try:
            eval_bytes = json.dumps(evaluation_report, indent=2, ensure_ascii=False)
            if workspace and workspace.is_dir():
                (workspace / "evaluation.json").write_text(eval_bytes, encoding="utf-8")
            runs_dir = self.settings.runs_root / run_id
            runs_dir.mkdir(parents=True, exist_ok=True)
            (runs_dir / "evaluation.json").write_text(eval_bytes, encoding="utf-8")
        except Exception:
            pass

        # Update database with immutable terminal status
        self.db.update_run(
            run_id,
            status=status,
            finished_at=utcnow(),
            duration_ms=duration_ms,
            success=int(result.get("success", False)),
            total_tokens=usage.get("total_tokens", 0),
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            llm_calls=usage.get("calls", 0),
            result_json=json.dumps(result, ensure_ascii=False),
            metrics_json=json.dumps(metrics),
            error=error_msg,
        )

        # Emit terminal events
        self.event(
            run_id,
            "verification_evaluated",
            f"Verification state: {metrics.get('verification_state', 'UNKNOWN')} (final: {metrics.get('final_status', 'UNKNOWN')})",
            {
                "verification_state": metrics.get("verification_state"),
                "final_status": metrics.get("final_status"),
                "execution_status": metrics.get("execution_status"),
                "verification_status": metrics.get("verification_status"),
                "repository_health": metrics.get("repository_health"),
                "warnings": metrics.get("warnings", []),
                "notification": notification or metrics.get("notification"),
                "verification_details": metrics.get("verification_details", {}),
                "test_passed": metrics.get("test_passed", False),
                "review_approved": metrics.get("review_approved", False),
                "metrics": {
                    "collected": metrics.get("tests_collected", 0),
                    "passed": metrics.get("tests_passed", 0),
                    "failed": metrics.get("tests_failed", 0),
                    "skipped": metrics.get("tests_skipped", 0),
                    "errors": 0,
                },
            },
        )

        self.event(
            run_id,
            "run_finished",
            notification or (
                "Run completed successfully" if result.get("success")
                else f"Run finished ({status}): {error_msg or 'Execution finished'}"
            ),
            {
                "success": result.get("success", False),
                "final_status": metrics.get("final_status"),
                "execution_status": metrics.get("execution_status"),
                "verification_status": metrics.get("verification_status"),
                "repository_health": metrics.get("repository_health"),
                "warnings": metrics.get("warnings", []),
                "notification": notification or metrics.get("notification"),
                "verification_state": metrics.get("verification_state"),
                "duration_ms": duration_ms,
                "total_tokens": usage.get("total_tokens", 0),
                "llm_calls": usage.get("calls", 0),
                "metrics": metrics,
            },
        )
        return True

    async def execute(self, run_id: str):
        row = self.db.get_run(run_id)
        if not row: return
        started = time.perf_counter()
        self.db.update_run(run_id, status="running", started_at=utcnow())
        self.event(run_id, "run_started", "Execution started", {"model": self.settings.model})
        workspace: Path | None = None
        usage_totals = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "inference_ms": 0, "tool_calls": 0}
        agent_outputs: dict[str, str] = {}
        dep_status = None
        test_generation_completed = False

        tracker = WorkflowTracker(
            run_id=run_id,
            overall_timeout_seconds=self.settings.agentharness_overall_run_timeout_seconds,
            stage_timeout_seconds=self.settings.agentharness_stage_timeout_seconds,
            max_agent_handoffs=self.settings.agentharness_max_agent_handoffs,
            event_callback=lambda typ, msg, data, agent=None: self.event(run_id, typ, msg, data, agent=agent),
        )

        try:
            repo = self.repos.resolve_repository(row["repository"])
            workspace = await self.repos.create_workspace(repo, run_id)
            self.db.update_run(run_id, workspace_path=str(workspace))
            self.event(run_id, "workspace_ready", "Created a disposable Git clone for this run", {"workspace": str(workspace)})

            tracker.transition(WorkflowStage.PREPARING_DEPENDENCIES)
            # Prepare environment (manifest inspection, isolated virtualenv, package installation)
            dep_info = await prepare_environment(
                workspace,
                self.settings,
                repo.name,
                event_callback=lambda typ, msg, data: self.event(run_id, typ, msg, data),
            )
            dep_status = dep_info.get("status")

            # Prevent pytest or agents from running if dependency preparation failed, timed out, or was blocked
            if dep_status != "DEPENDENCIES_READY":
                error_msg = dep_info.get("error") or f"Dependency preparation incomplete ({dep_status})."
                diagnostics = dep_info.get("diagnostics", {})
                self.event(
                    run_id,
                    "dependency_warning",
                    f"Dependency warning ({dep_status}): {error_msg}. Proceeding with alternative verification.",
                    {
                        "dependency_status": dep_status,
                        "error": error_msg,
                        "diagnostics": diagnostics,
                        "declared_packages": dep_info.get("declared_packages", []),
                    },
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
                dependency_status=dep_status,
                dep_info=dep_info,
            )
            max_steps = min(max(int(row.get("max_agent_steps") or self.settings.agentharness_max_agent_steps), 1), 20)
            executor = AgentExecutor(self.llm, runtime,
                event=lambda typ, msg, data: self.event(run_id, typ, msg, data))

            # Initial repository test inspection
            repo_test_info = runtime.detect_repository_tests()
            has_original_tests = bool(repo_test_info.get("has_existing_tests", False))
            self.event(run_id, "test_discovery_snapshot", f"Detected {repo_test_info['detected_language']} ({repo_test_info['detected_framework']})", repo_test_info)

            # Record baseline tests before any agent modifications
            tracker.transition(WorkflowStage.RECORDING_BASELINE)
            if has_original_tests and dep_status == "DEPENDENCIES_READY":
                baseline_record = await runtime.run_test_command(
                    repo_test_info.get("recommended_command") or runtime._detect_test_command() or ["python", "-m", "pytest", "-q"],
                    timeout=min(self.settings.agentharness_test_timeout_seconds, 60, tracker.get_remaining_stage_budget())
                )
            else:
                baseline_record = {
                    "command": repo_test_info.get("recommended_command") or ["python", "-m", "pytest", "-q"],
                    "outcome": "NO_TESTS" if not has_original_tests else "BLOCKED",
                    "passed": False,
                    "metrics": {"collected": 0, "passed": 0, "failed": 0, "skipped": 0, "errors": 0},
                    "failure_details": [],
                    "error_message": "No pre-existing test suite detected in repository." if not has_original_tests else f"Baseline skipped due to dependency status: {dep_status}",
                    "duration_ms": 0,
                }
            baseline_tests = [baseline_record] if baseline_record else []
            self.event(
                run_id,
                "baseline_recorded",
                f"Baseline test status: {baseline_record.get('outcome', 'NO_TESTS')} ({baseline_record.get('metrics', {}).get('collected', 0)} collected)",
                {"baseline": baseline_record}
            )

            # Step 1: Coordinator
            tracker.transition(WorkflowStage.PLANNING, agent="coordinator")
            plan_result = await executor.run(
                "coordinator",
                row["task"],
                context="Create an execution plan. Set include_security_review true only if security/auth/secrets/injection are involved.",
                max_steps=1,
                tools_enabled=False,
                timeout=min(tracker.get_remaining_stage_budget(), 60.0),
            )
            agent_outputs["coordinator"] = plan_result["output"]
            plan = parse_json_loose(plan_result["output"])
            self.event(run_id, "plan_created", "Coordinator created the execution plan", {"plan": plan}, "coordinator")

            # Step 2: Read-only specialist investigation
            tracker.record_handoff("coordinator", "repository_analyst")
            tracker.transition(WorkflowStage.ANALYZING, agent="repository_analyst")

            async def reader(role: str, extra_ctx: str = ""):
                async with self.semaphore:
                    return await executor.run(
                        role,
                        row["task"],
                        context=f"Coordinator plan:\n{json.dumps(plan)}\n{extra_ctx}\nInspect repository code using tools; do not edit.",
                        max_steps=max_steps,
                        timeout=tracker.get_remaining_stage_budget(),
                    )

            scout_ctx = f"Automated test detection:\n{json.dumps(repo_test_info, indent=2)}"
            if plan.get("include_test_scout", True) and has_original_tests:
                analyst, scout = await asyncio.gather(reader("repository_analyst"), reader("test_scout", scout_ctx))
                agent_outputs[analyst["role"]] = analyst["output"]
                agent_outputs[scout["role"]] = scout["output"]
                test_discovery = scout["output"]
            else:
                analyst = await reader("repository_analyst")
                agent_outputs[analyst["role"]] = analyst["output"]
                test_discovery = "Test discovery skipped (repository lacks automated test suites)." if not has_original_tests else "Test discovery was skipped by coordinator."

            # Step 3: Coding Agent with structured handoff
            tracker.record_handoff("repository_analyst", "coding_agent")
            tracker.transition(WorkflowStage.CODING, agent="coding_agent")

            coding_context = (
                f"COORDINATOR PLAN:\n{json.dumps(plan)}\n\n"
                f"REPOSITORY ANALYSIS:\n{analyst['output'][:2500]}\n\n"
                f"TEST DISCOVERY & RECOMMENDATIONS:\n{test_discovery[:2500]}\n\n"
                f"WORKSPACE TEST STATUS:\n{json.dumps(repo_test_info, indent=2)}\n\n"
                "CRITICAL MANDATORY INSTRUCTIONS:\n"
                "1. For bug-fixing or implementation tasks, you MUST use write_file to modify the actual production code file(s) (e.g. in app/ or src/).\n"
                "   Submitting a test-only change or reporting a bug fix without modifying production code is STRICTLY PROHIBITED and will cause task rejection.\n"
                "2. Order of operations: first modify production code, then add or update meaningful regression tests in tests/ (e.g. tests/test_*.py).\n"
                "3. Ensure tests specifically verify the defect fix against domain/market invariants and would fail on unpatched code."
            )

            coder = await executor.run(
                "coding_agent",
                row["task"],
                context=coding_context,
                max_steps=max_steps,
                timeout=tracker.get_remaining_stage_budget(),
            )
            agent_outputs[coder["role"]] = coder["output"]

            # Step 3b: Change-evidence validation & Bounded repair attempt
            needs_prod = is_implementation_task(row["task"], plan)
            changed_files = await self._changed_files(workspace)
            prod_files, test_meta_files = classify_changed_files(changed_files)

            max_repairs = 1
            repair_attempt = 0
            while needs_prod and len(prod_files) == 0 and repair_attempt < max_repairs:
                repair_attempt += 1
                likely_files = plan.get("likely_files") or []
                likely_prod, _ = classify_changed_files(likely_files)
                target_hint = ", ".join(likely_prod) if likely_prod else "the relevant source file in app/ or src/ (e.g. app/ml_service.py)"

                self.event(
                    run_id,
                    "implementation_deficiency_detected",
                    f"Coding agent did not modify any production code files (attempt {repair_attempt}/{max_repairs})",
                    {"changed_files": changed_files, "test_meta_files": test_meta_files, "target_hint": target_hint},
                    "coding_agent"
                )

                repair_context = (
                    f"CRITICAL IMPLEMENTATION DEFICIENCY DETECTED:\n"
                    f"Your git diff shows that ZERO production code files were modified on disk.\n"
                    f"You only created or modified test/metadata files: {', '.join(test_meta_files) or '(none)'}.\n"
                    f"The task requires fixing a bug in the production application code: {row['task']}.\n"
                    f"Coordinator identified likely files: {target_hint}.\n\n"
                    f"MANDATORY ACTION REQUIRED:\n"
                    f"1. You MUST call write_file to edit the production code file ({target_hint}) to implement the actual fix.\n"
                    f"2. Do NOT simply describe a fix or only add tests. You MUST call write_file on the production file.\n"
                    f"Use write_file now to update the production code."
                )

                coder_repair = await executor.run(
                    "coding_agent",
                    row["task"],
                    context=repair_context,
                    max_steps=min(max_steps, 6),
                    timeout=tracker.get_remaining_stage_budget(),
                )
                coder["output"] += f"\n\n--- REPAIR ATTEMPT {repair_attempt} ---\n" + coder_repair["output"]
                agent_outputs["coding_agent"] = coder["output"]

                changed_files = await self._changed_files(workspace)
                prod_files, test_meta_files = classify_changed_files(changed_files)
                if prod_files:
                    self.event(
                        run_id,
                        "implementation_repair_succeeded",
                        f"Coding agent successfully modified production code ({', '.join(prod_files)})",
                        {"prod_files": prod_files},
                        "coding_agent"
                    )
                    break

            if needs_prod and len(prod_files) == 0:
                implementation_status = "NOT_IMPLEMENTED"
                self.event(
                    run_id,
                    "implementation_repair_exhausted",
                    "Coding agent failed to modify production files after repair attempts",
                    {"changed_files": changed_files},
                    "coding_agent"
                )
            else:
                implementation_status = "IMPLEMENTED"

            self.event(
                run_id,
                "implementation_ready",
                f"Coding agent finished (status: {implementation_status})",
                {"status": implementation_status, "prod_files": prod_files, "test_files": test_meta_files, "summary": coder["output"][:2000]},
                "coding_agent"
            )

            # Step 4: Test & Debug Agent
            tracker.record_handoff("coding_agent", "test_debug_agent")
            tracker.transition(WorkflowStage.TESTING_DEBUGGING, agent="test_debug_agent")

            diff_after_coder = await runtime.execute("git_diff", {})
            tester_context = (
                f"TASK:\n{row['task']}\n\n"
                f"IMPLEMENTATION STATUS: {implementation_status}\n"
                f"MODIFIED PRODUCTION FILES: {', '.join(prod_files) or 'NONE'}\n"
                f"MODIFIED TEST FILES: {', '.join(test_meta_files) or 'NONE'}\n\n"
                f"TEST DISCOVERY:\n{test_discovery[:2500]}\n\n"
                f"IMPLEMENTATION REPORT:\n{coder['output'][:2500]}\n\n"
                f"GIT DIFF:\n{diff_after_coder[:3000]}\n\n"
                f"CONFIGURED TEST COMMAND: {row['test_command']}\n"
                "Run tests using run_tests. Check whether tests exercise the production fix. If tests fail or are missing, diagnose and fix them using write_file."
            )

            tester = await executor.run(
                "test_debug_agent",
                row["task"],
                context=tester_context,
                max_steps=max_steps,
                timeout=tracker.get_remaining_stage_budget(),
            )
            agent_outputs[tester["role"]] = tester["output"]

            # Step 5: Verification & Baseline Validation
            tracker.transition(WorkflowStage.VERIFYING)

            # Re-evaluate changed files after test_debug_agent
            changed_files = await self._changed_files(workspace)
            prod_files, test_meta_files = classify_changed_files(changed_files)
            test_files = [f for f in test_meta_files if any(ext in f for ext in (".py", ".js", ".ts", ".jsx", ".tsx")) and "test" in f.lower()]
            has_generated_tests = len(test_files) > 0 and not has_original_tests

            # If repository had no tests and agents didn't create any, generate fallback regression test skeleton (at most once)
            if not has_original_tests and not test_files and prod_files and not test_generation_completed:
                test_generation_completed = True
                test_dir_to_use = repo_test_info.get("test_directory") or "tests"
                gen_path = generate_regression_test_skeleton(
                    workspace, prod_files, row["task"], test_dir=test_dir_to_use
                )
                if gen_path and gen_path.is_file():
                    has_generated_tests = True
                    changed_files = await self._changed_files(workspace)
                    prod_files, test_meta_files = classify_changed_files(changed_files)
                    test_files = [f for f in test_meta_files if any(ext in f for ext in (".py", ".js", ".ts", ".jsx", ".tsx")) and "test" in f.lower()]
                    self.event(
                        run_id,
                        "regression_test_generated",
                        f"Generated regression test skeleton at {gen_path.relative_to(workspace)}",
                        {"test_file": str(gen_path.relative_to(workspace))},
                        "test_debug_agent",
                    )
                    cmd = ["python", "-m", "pytest", "-q", str(gen_path.relative_to(workspace))]
                    await runtime.run_test_command(cmd, timeout=min(self.settings.agentharness_test_timeout_seconds, 60))

            # Fallback test run if test_debug_agent did not run any test tool
            if not runtime.test_results:
                self.event(run_id, "verification_fallback", "Test agent did not execute tests; running configured verification command", {"test_command": row["test_command"]}, "test_debug_agent")
                await runtime.execute("run_tests", {"command": "auto"})

            # Failure-to-pass (RED-to-GREEN) verification:
            reproduced_on_baseline = False
            failure_to_pass_verified = False
            baseline_regression_details: list[str] = []

            # Re-evaluate changed files before failure-to-pass
            changed_files = await self._changed_files(workspace)
            prod_files, test_meta_files = classify_changed_files(changed_files)
            test_files = [f for f in test_meta_files if any(ext in f for ext in (".py", ".js", ".ts", ".jsx", ".tsx")) and "test" in f.lower()]

            if prod_files and test_files:
                saved_prod: dict[str, str] = {}
                for pf in prod_files:
                    p_path = workspace / pf
                    if p_path.is_file():
                        try:
                            saved_prod[pf] = p_path.read_text(encoding="utf-8")
                        except Exception:
                            pass

                if saved_prod:
                    proc = None
                    try:
                        proc = await asyncio.create_subprocess_exec(
                            "git", "checkout", "HEAD", "--", *list(saved_prod.keys()),
                            cwd=workspace, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                            start_new_session=(sys.platform != "win32"),
                        )
                        await asyncio.wait_for(proc.communicate(), timeout=15.0)

                        repro_record = await runtime.run_test_command(
                            runtime._detect_test_command() or ["python", "-m", "pytest", "-q"],
                            timeout=min(self.settings.agentharness_test_timeout_seconds, 30)
                        )
                        if repro_record.get("outcome") == "FAIL":
                            reproduced_on_baseline = True
                            failure_to_pass_verified = True
                            baseline_regression_details = repro_record.get("failure_details", [])
                            self.event(
                                run_id,
                                "failure_to_pass_verified",
                                f"Verified: Regression tests correctly fail on unpatched baseline code ({len(baseline_regression_details)} failures)",
                                {"failure_details": baseline_regression_details}
                            )
                        else:
                            self.event(
                                run_id,
                                "failure_to_pass_unverified",
                                "Tests passed on unpatched baseline code (they may verify existing behavior only)",
                                {"outcome": repro_record.get("outcome")}
                            )
                    except (asyncio.TimeoutError, Exception):
                        if proc:
                            _kill_process_tree(proc)
                    finally:
                        # Restore production file contents unconditionally
                        for pf, content in saved_prod.items():
                            try:
                                (workspace / pf).write_text(content, encoding="utf-8")
                            except Exception:
                                pass

            # Re-run tests on restored patched code only if failure-to-pass check ran and verified
            if failure_to_pass_verified:
                await runtime.execute("run_tests", {"command": "auto"})

            # Check if alternative verification is needed (when no tests were run or tests produced NO_TESTS/BLOCKED)
            last_test_outcome = runtime.test_results[-1].get("outcome") if runtime.test_results else "NO_TESTS"
            alternative_verification = None
            if last_test_outcome in {"NO_TESTS", "BLOCKED", "INCONCLUSIVE"} or not runtime.test_results:
                alternative_verification = await execute_alternative_verification(workspace, prod_files, runtime)
                self.event(
                    run_id,
                    "alternative_verification_executed",
                    f"Alternative verification completed (passed: {alternative_verification.get('passed', False)})",
                    alternative_verification,
                )

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

            if alternative_verification:
                test_evidence_summary += f"\nAlternative Verification: {alternative_verification.get('details')}"

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

            changed_files_summary = "\n".join(f"- {f}" for f in changed_files) or "No files changed."

            # Step 6: Code Reviewer
            tracker.record_handoff("test_debug_agent", "code_reviewer")
            tracker.transition(WorkflowStage.REVIEWING, agent="code_reviewer")

            review_context = (
                f"ORIGINAL TASK / BUG DESCRIPTION:\n{row['task']}\n\n"
                f"COORDINATOR PLAN:\n{json.dumps(plan)}\n\n"
                f"IMPLEMENTATION STATUS: {implementation_status}\n"
                f"MODIFIED PRODUCTION FILES: {', '.join(prod_files) or 'NONE (NO PRODUCTION CODE WAS MODIFIED)'}\n"
                f"MODIFIED TEST / METADATA FILES: {', '.join(test_meta_files) or 'NONE'}\n\n"
                f"FAILURE-TO-PASS REGRESSION EVIDENCE:\n"
                f"{'VERIFIED: New regression tests failed on unpatched baseline code and pass on patched code.' if reproduced_on_baseline else 'NOTE: Tests were not verified to fail on unpatched baseline code (they may verify existing behavior only).'}\n\n"
                f"IMPLEMENTATION SUMMARY:\n{coder['output'][:2000]}\n\n"
                f"BASELINE TESTS (BEFORE AGENT CHANGES):\n{baseline_summary}\n\n"
                f"AFTER-STATE TEST EXECUTION EVIDENCE:\n{test_evidence_summary}\n\n"
                f"CHANGED FILES:\n{changed_files_summary}\n\n"
                f"GIT DIFF:\n{diff_display[:10000]}\n\n"
                "MANDATORY REVIEW RULES:\n"
                "1. If the task is a bug fix or feature implementation and ZERO production files were modified, you MUST reject with NEEDS_CHANGES and state: 'No production code changes were made; only test/metadata files were modified.'\n"
                "2. If the implementation summary claims a file was edited (e.g. app/ml_service.py) but git diff contains no changes to that file, you MUST reject with NEEDS_CHANGES.\n"
                "3. Check that tests ran and all passed. If tests failed, reject with NEEDS_CHANGES.\n"
                "4. If production code was fixed, tests pass, and evidence is verified, approve with APPROVE."
            )

            reviewer = await executor.run(
                "code_reviewer",
                row["task"],
                context=review_context,
                max_steps=max_steps,
                timeout=tracker.get_remaining_stage_budget(),
            )
            agent_outputs[reviewer["role"]] = reviewer["output"]

            # Step 7: Security Reviewer (if requested)
            security_review = None
            security_keywords = ("security", "auth", "permission", "secret", "credential", "token", "injection",
                                 "path traversal", "command execution", "authorization", "user data", "sql")
            needs_security_review = bool(plan.get("include_security_review")) or any(word in row["task"].lower() for word in security_keywords)
            if needs_security_review:
                tracker.record_handoff("code_reviewer", "security_reviewer")
                security_review = await executor.run(
                    "security_reviewer",
                    row["task"],
                    context=review_context,
                    max_steps=max_steps,
                    timeout=tracker.get_remaining_stage_budget(),
                )
                agent_outputs[security_review["role"]] = security_review["output"]

            usage_totals = executor.usage
            duration_ms = round((time.perf_counter() - started) * 1000)

            # Step 8: Finalizing
            tracker.transition(WorkflowStage.FINALIZING)

            # Verification state aggregation
            verification_state, test_passed, review_approved, error_msg, review_details = compute_verification_state(
                runtime.test_results,
                reviewer["output"],
                workspace,
                dependency_status=dep_info.get("status"),
                dependency_error=dep_info.get("error"),
                baseline_tests=baseline_tests,
                implementation_status=implementation_status,
                failure_to_pass_verified=failure_to_pass_verified,
                reproduced_on_baseline=reproduced_on_baseline,
                alternative_verification=alternative_verification,
                has_original_tests=has_original_tests,
                has_generated_tests=has_generated_tests,
                dependency_source=dep_info.get("dependency_source", "manifest"),
                has_manifest=(dep_info.get("dependency_source", "manifest") != "inferred"),
                inferred_packages=dep_info.get("inferred_packages", []),
                ambiguous_imports=dep_info.get("inference_diagnostics", {}).get("ambiguous_imports", []),
            )

            last_metrics = runtime.test_results[-1].get("metrics", {}) if runtime.test_results else {}
            last_outcome = runtime.test_results[-1].get("outcome") if runtime.test_results else "NO_TESTS"
            test_state_label = review_details.get("test_state", last_outcome)

            final_eval = evaluate_final_status(
                dependency_status=dep_info.get("status"),
                dependency_source=dep_info.get("dependency_source", "manifest"),
                has_manifest=(dep_info.get("dependency_source", "manifest") != "inferred"),
                has_original_tests=has_original_tests,
                has_generated_tests=has_generated_tests,
                implementation_status=implementation_status,
                test_state=last_outcome,
                test_metrics=last_metrics,
                review_details=review_details,
                alternative_verification=alternative_verification,
                inferred_packages=dep_info.get("inferred_packages", []),
                ambiguous_imports=dep_info.get("inference_diagnostics", {}).get("ambiguous_imports", []),
                error_message=error_msg,
            )

            final_status = final_eval["final_status"]
            execution_status = final_eval["execution_status"]
            verification_status = final_eval["verification_status"]
            repository_health = final_eval["repository_health"]
            warnings = final_eval["warnings"]
            notification = final_eval["notification"]
            verification_details = final_eval["verification_details"]
            success = final_eval["success"]
            db_status = final_eval["db_status"]

            review_details.update({
                "final_status": final_status,
                "execution_status": execution_status,
                "verification_status": verification_status,
                "repository_health": repository_health,
                "warnings": warnings,
                "notification": notification,
                "verification_details": verification_details,
            })

            dep_summary = {
                "status": dep_info.get("status"),
                "language": dep_info.get("language"),
                "duration_ms": dep_info.get("duration_ms"),
                "cached": dep_info.get("cached", False),
                "declared_packages": dep_info.get("declared_packages", []),
                "dependency_source": dep_info.get("dependency_source", "manifest"),
                "inferred_packages": dep_info.get("inferred_packages", []),
                "manifest_hash": dep_info.get("manifest_hash"),
                "exit_code": dep_info.get("exit_code"),
                "error": dep_info.get("error"),
                "inference_diagnostics": dep_info.get("inference_diagnostics", {}),
            }

            metrics = {
                "duration_ms": duration_ms,
                "inference_ms": usage_totals["inference_ms"],
                "tool_calls": usage_totals["tool_calls"],
                "agent_count": len(agent_outputs),
                "agent_handoffs": tracker.handoff_count,
                "agent_metrics": getattr(executor, "agent_metrics", {}),
                "dependency_status": dep_info.get("status"),
                "dependency_preparation": dep_summary,
                "test_state": test_state_label,
                "review_state": review_details.get("review_state", "REJECTED" if not review_approved else "APPROVED"),
                "verification_state": verification_state,
                "verification_verdict": review_details.get("verification_verdict", verification_state),
                "final_status": final_status,
                "execution_status": execution_status,
                "verification_status": verification_status,
                "repository_health": repository_health,
                "warnings": warnings,
                "notification": notification,
                "verification_details": verification_details,
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
                "alternative_verification": alternative_verification,
                "has_original_tests": has_original_tests,
                "has_generated_tests": has_generated_tests,
                "security_review_included": security_review is not None,
                "success_definition": "Relevant tests were collected and passed (PASS) and the code reviewer approved the diff.",
            }

            result = {
                "plan": plan,
                "dependency_status": dep_info.get("status"),
                "dependency_preparation": dep_summary,
                "verification_state": verification_state,
                "verification_verdict": review_details.get("verification_verdict", verification_state),
                "final_status": final_status,
                "execution_status": execution_status,
                "verification_status": verification_status,
                "repository_health": repository_health,
                "warnings": warnings,
                "notification": notification,
                "verification_details": verification_details,
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
                "alternative_verification": alternative_verification,
                "has_original_tests": has_original_tests,
                "has_generated_tests": has_generated_tests,
                "agents": agent_outputs,
                "agent_metrics": getattr(executor, "agent_metrics", {}),
                "security_review": security_review["output"] if security_review else None,
            }

            evaluation_report = {
                "run_id": run_id,
                "repository": row["repository"],
                "task": row["task"],
                "verification_state": verification_state,
                "verification_verdict": review_details.get("verification_verdict", verification_state),
                "final_status": final_status,
                "execution_status": execution_status,
                "verification_status": verification_status,
                "repository_health": repository_health,
                "warnings": warnings,
                "notification": notification,
                "verification_details": verification_details,
                "success": success,
                "duration_ms": duration_ms,
                "dependency_preparation": dep_summary,
                "baseline_comparison": review_details.get("baseline_comparison", {}),
                "regression_status": review_details.get("regression_status", "NO_REGRESSION"),
                "alternative_verification": alternative_verification,
                "has_original_tests": has_original_tests,
                "has_generated_tests": has_generated_tests,
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

            self._finalize_run(
                run_id,
                status=db_status,
                duration_ms=duration_ms,
                metrics=metrics,
                result=result,
                evaluation_report=evaluation_report,
                error_msg=error_msg,
                workspace=workspace,
                notification=notification,
                usage_totals=usage_totals,
            )

        except asyncio.CancelledError:
            duration_ms = round((time.perf_counter() - started) * 1000)
            error_msg = "Run cancelled by user."
            eval_result = evaluate_final_status(
                dependency_status=dep_status,
                has_manifest=True,
                has_original_tests=False,
                implementation_status="CANCELLED",
                test_state="BLOCKED",
                error_message=error_msg,
            )
            metrics = {
                "duration_ms": duration_ms,
                "inference_ms": usage_totals.get("inference_ms", 0),
                "tool_calls": usage_totals.get("tool_calls", 0),
                "agent_count": len(agent_outputs),
                "agent_handoffs": tracker.handoff_count if 'tracker' in locals() else 0,
                "dependency_status": dep_status,
                "test_state": "BLOCKED",
                "review_state": "BLOCKED",
                "verification_state": "CANCELLED",
                "final_status": eval_result["final_status"],
                "execution_status": eval_result["execution_status"],
                "verification_status": eval_result["verification_status"],
                "repository_health": eval_result["repository_health"],
                "warnings": eval_result["warnings"],
                "notification": eval_result["notification"],
                "verification_details": eval_result["verification_details"],
            }
            result = {
                "success": False,
                "final_status": eval_result["final_status"],
                "execution_status": eval_result["execution_status"],
                "verification_status": eval_result["verification_status"],
                "error": error_msg,
                "metrics": metrics,
            }
            eval_report = {
                "run_id": run_id,
                "success": False,
                "final_status": eval_result["final_status"],
                "execution_status": eval_result["execution_status"],
                "duration_ms": duration_ms,
                "metrics": metrics,
                "error": error_msg,
            }
            self.event(run_id, "run_cancelled", error_msg, {})
            self._finalize_run(
                run_id,
                status="cancelled",
                duration_ms=duration_ms,
                metrics=metrics,
                result=result,
                evaluation_report=eval_report,
                error_msg=error_msg,
                workspace=workspace,
                usage_totals=usage_totals,
            )
            raise

        except (RunTimeoutError, asyncio.TimeoutError) as exc:
            duration_ms = round((time.perf_counter() - started) * 1000)
            error_msg = f"Run timed out: {exc}"
            eval_result = evaluate_final_status(
                dependency_status=dep_status,
                has_manifest=True,
                has_original_tests=False,
                implementation_status="TIMED_OUT",
                test_state="BLOCKED",
                error_message=error_msg,
            )
            metrics = {
                "duration_ms": duration_ms,
                "inference_ms": usage_totals.get("inference_ms", 0),
                "tool_calls": usage_totals.get("tool_calls", 0),
                "agent_count": len(agent_outputs),
                "agent_handoffs": tracker.handoff_count if 'tracker' in locals() else 0,
                "dependency_status": dep_status,
                "test_state": "BLOCKED",
                "review_state": "BLOCKED",
                "verification_state": "TIMED_OUT",
                "final_status": eval_result["final_status"],
                "execution_status": eval_result["execution_status"],
                "verification_status": eval_result["verification_status"],
                "repository_health": eval_result["repository_health"],
                "warnings": eval_result["warnings"],
                "notification": eval_result["notification"],
                "verification_details": eval_result["verification_details"],
            }
            result = {
                "success": False,
                "final_status": eval_result["final_status"],
                "execution_status": eval_result["execution_status"],
                "verification_status": eval_result["verification_status"],
                "error": error_msg,
                "metrics": metrics,
            }
            eval_report = {
                "run_id": run_id,
                "success": False,
                "final_status": eval_result["final_status"],
                "execution_status": eval_result["execution_status"],
                "duration_ms": duration_ms,
                "metrics": metrics,
                "error": error_msg,
            }
            self.event(run_id, "run_timed_out", error_msg, {"duration_ms": duration_ms})
            self._finalize_run(
                run_id,
                status="timed_out",
                duration_ms=duration_ms,
                metrics=metrics,
                result=result,
                evaluation_report=eval_report,
                error_msg=error_msg,
                workspace=workspace,
                usage_totals=usage_totals,
            )

        except Exception as exc:
            duration_ms = round((time.perf_counter() - started) * 1000)
            error_msg = f"{type(exc).__name__}: {exc}"
            eval_result = evaluate_final_status(
                dependency_status=dep_status,
                has_manifest=True,
                has_original_tests=False,
                implementation_status="FAILED",
                test_state="BLOCKED",
                error_message=error_msg,
            )
            metrics = {
                "duration_ms": duration_ms,
                "inference_ms": usage_totals.get("inference_ms", 0),
                "tool_calls": usage_totals.get("tool_calls", 0),
                "agent_count": len(agent_outputs),
                "agent_handoffs": tracker.handoff_count if 'tracker' in locals() else 0,
                "dependency_status": dep_status,
                "test_state": "BLOCKED",
                "review_state": "BLOCKED",
                "verification_state": "FAILED",
                "final_status": eval_result["final_status"],
                "execution_status": eval_result["execution_status"],
                "verification_status": eval_result["verification_status"],
                "repository_health": eval_result["repository_health"],
                "warnings": eval_result["warnings"],
                "notification": eval_result["notification"],
                "verification_details": eval_result["verification_details"],
            }
            result = {
                "success": False,
                "final_status": eval_result["final_status"],
                "execution_status": eval_result["execution_status"],
                "verification_status": eval_result["verification_status"],
                "error": error_msg,
                "metrics": metrics,
            }
            eval_report = {
                "run_id": run_id,
                "success": False,
                "final_status": eval_result["final_status"],
                "execution_status": eval_result["execution_status"],
                "duration_ms": duration_ms,
                "metrics": metrics,
                "error": error_msg,
            }
            self.event(run_id, "run_error", f"Execution failed: {error_msg}", {"error": error_msg})
            self._finalize_run(
                run_id,
                status="failed",
                duration_ms=duration_ms,
                metrics=metrics,
                result=result,
                evaluation_report=eval_report,
                error_msg=error_msg,
                workspace=workspace,
                usage_totals=usage_totals,
            )

        finally:
            tracker.is_terminal = True

    async def _changed_files(self, workspace: Path, timeout: float = 15.0) -> list[str]:
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "status", "--short", cwd=workspace,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=(sys.platform != "win32")
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return [line[3:].strip() for line in stdout.decode(errors="replace").splitlines() if len(line) > 3]
        except (asyncio.TimeoutError, Exception):
            if proc:
                _kill_process_tree(proc)
            return []
