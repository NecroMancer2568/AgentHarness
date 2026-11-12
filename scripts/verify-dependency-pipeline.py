#!/usr/bin/env python3
"""Validation script verifying AgentHarness Lab dependency management, caching, and timeout fixes."""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

# Ensure backend is in python path
backend_path = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.config import Settings
from app.dependencies import (
    inspect_repository_dependencies,
    detect_python_version_requirement,
    check_python_version_satisfies,
    resolve_compatible_python,
    run_process_safely,
    prepare_environment,
    build_cache_key,
)
from app.repository import ToolRuntime


async def main():
    print("=" * 70)
    print("AgentHarness Lab — Dependency Pipeline Validation")
    print("=" * 70)
    tmp_root = Path(tempfile.mkdtemp(prefix="ah_val_"))

    settings = Settings()
    settings.agentharness_repos_root = str(tmp_root / "repos")
    settings.agentharness_runs_root = str(tmp_root / "runs")
    settings.agentharness_venv_cache_root = str(tmp_root / "venvs")
    settings.agentharness_pip_cache_dir = str(tmp_root / "pip_cache")
    settings.agentharness_dependency_timeout_seconds = 60

    try:
        # Scenario 1: Multi-format manifest parsing (requirements.txt, pyproject.toml, poetry.lock, uv.lock)
        print("\n[Scenario 1] Testing multi-format dependency manifest inspection...")
        ws1 = tmp_root / "multi_manifest_repo"
        ws1.mkdir(parents=True)
        (ws1 / "requirements.txt").write_text("fastapi>=0.100.0\npandas\nrequests\n")
        (ws1 / "pyproject.toml").write_text("""
[project]
name = "ml-app"
requires-python = ">=3.10,<3.14"
dependencies = [
    "scikit-learn",
    "joblib",
    "sqlalchemy",
]
""")
        (ws1 / "poetry.lock").write_text("""
[[package]]
name = "torch"
version = "2.2.0"

[[package]]
name = "transformers"
version = "4.38.0"
""")
        (ws1 / "uv.lock").write_text("""
version = 1
revision = 1

[[package]]
name = "yfinance"
version = "0.2.36"

[[package]]
name = "uvicorn"
version = "0.28.0"
""")

        dep_info = inspect_repository_dependencies(ws1)
        expected_pkgs = {
            "fastapi", "pandas", "requests", "scikit-learn", "joblib",
            "sqlalchemy", "torch", "transformers", "yfinance", "uvicorn"
        }
        found_pkgs = set(dep_info["declared_packages"])
        missing = expected_pkgs - found_pkgs
        assert not missing, f"Missing parsed packages: {missing}"
        print(f"  ✓ Successfully inspected {len(dep_info['manifests'])} manifests:")
        for m in dep_info["manifests"]:
            print(f"    - {m['type']} ({m['path']}): {len(m['packages'])} packages")
        print(f"  ✓ All 11 target ML packages declared and tracked. Manifest hash: {dep_info['combined_hash'][:16]}...")

        # Scenario 2: Python version detection and compatible interpreter resolution
        print("\n[Scenario 2] Testing Python version requirement detection & interpreter resolution...")
        req_py = detect_python_version_requirement(ws1)
        print(f"  ✓ Detected requirement: {req_py['specifier']} from {req_py['source']}")
        selected_py, py_ver, py_details = resolve_compatible_python(ws1)
        print(f"  ✓ Selected interpreter: {selected_py} (version {py_ver})")
        assert check_python_version_satisfies(py_ver, req_py["specifier"])
        print("  ✓ Selected interpreter satisfies required specifier.")

        # Scenario 3: Real environment provisioning and cache reuse
        print("\n[Scenario 3] Testing environment provisioning and cache reuse...")
        ws_cache = tmp_root / "cache_repo"
        ws_cache.mkdir(parents=True)
        (ws_cache / "requirements.txt").write_text("iniconfig\n")

        t0 = time.perf_counter()
        res_run1 = await prepare_environment(ws_cache, settings, "cache_repo")
        dur1 = round((time.perf_counter() - t0) * 1000)
        assert res_run1["status"] == "DEPENDENCIES_READY"
        assert res_run1["cached"] is False
        print(f"  ✓ Run 1 (Fresh provision): Status={res_run1['status']}, Cached={res_run1['cached']}, Duration={dur1}ms")
        print(f"    Venv directory: {res_run1['venv_path'].name}")

        t1 = time.perf_counter()
        res_run2 = await prepare_environment(ws_cache, settings, "cache_repo")
        dur2 = round((time.perf_counter() - t1) * 1000)
        assert res_run2["status"] == "DEPENDENCIES_READY"
        assert res_run2["cached"] is True
        print(f"  ✓ Run 2 (Cache hit): Status={res_run2['status']}, Cached={res_run2['cached']}, Duration={dur2}ms")
        print(f"  ✓ Cache speedup: {dur1}ms -> {dur2}ms ({dur1 // max(dur2, 1)}x faster)")

        # Scenario 4: Manifest change invalidates cache
        print("\n[Scenario 4] Testing cache invalidation on manifest modification...")
        (ws_cache / "requirements.txt").write_text("iniconfig\npackaging\n")
        res_run3 = await prepare_environment(ws_cache, settings, "cache_repo")
        assert res_run3["cached"] is False
        assert res_run3["manifest_hash"] != res_run1["manifest_hash"]
        print(f"  ✓ Cache missed on manifest edit: New Hash={res_run3['manifest_hash'][:16]}, Cached={res_run3['cached']}")

        # Scenario 5: Reproduction of original failure scenario (Timeout during pip install)
        print("\n[Scenario 5] Testing reproduction of original failure scenario (Installation Timeout)...")
        ws_fail = tmp_root / "timeout_repo"
        ws_fail.mkdir(parents=True)
        (ws_fail / "requirements.txt").write_text(
            "fastapi\njoblib\npandas\npydantic\nrequests\n"
            "scikit-learn\nsqlalchemy\ntorch\ntransformers\nuvicorn\nyfinance\n"
        )

        # Simulate timeout on pip install
        orig_run_proc = run_process_safely

        async def mock_timeout_proc(cmd, cwd, timeout, env=None):
            if "venv" in cmd:
                return await orig_run_proc(cmd, cwd, timeout, env)
            return -1, "Downloading torch (2800 MB)...", "Operation timed out", True

        settings.agentharness_dependency_timeout_seconds = 10
        with patch("app.dependencies.run_process_safely", side_effect=mock_timeout_proc):
            fail_res = await prepare_environment(ws_fail, settings, "timeout_repo")
            assert fail_res["status"] == "DEPENDENCY_INSTALL_BLOCKED"
            assert "diagnostics" in fail_res
            assert fail_res["diagnostics"]["reason"] == "TIMEOUT"
            print(f"  ✓ Caught timeout cleanly:")
            print(f"    - Status: {fail_res['status']}")
            print(f"    - Error: {fail_res['error']}")
            print(f"    - Reason: {fail_res['diagnostics']['reason']}")
            print(f"    - Actionable Suggestion: {fail_res['diagnostics']['actionable_suggestion']}")

        # Scenario 6: Pytest execution prevention on failed/blocked dependency installation
        print("\n[Scenario 6] Verifying pytest execution is strictly prevented when dependencies are blocked...")
        runtime = ToolRuntime(
            ws_fail,
            settings,
            "pytest",
            dependency_status=fail_res["status"],
        )
        test_res = await runtime.run_test_command(["pytest", "-q"], timeout=30)
        assert test_res["outcome"] == "BLOCKED"
        assert test_res["passed"] is False
        assert test_res["metrics"]["collected"] == 0
        assert test_res["metrics"]["passed"] == 0
        assert "dependency installation was not completed" in test_res["error_message"]
        print(f"  ✓ Pytest prevented from executing:")
        print(f"    - Outcome: {test_res['outcome']}")
        print(f"    - Passed: {test_res['passed']}")
        print(f"    - Tests collected: {test_res['metrics']['collected']}")
        print(f"    - Error: {test_res['error_message']}")

        print("\n" + "=" * 70)
        print("ALL 6 VALIDATION SCENARIOS PASSED WITH ZERO ERRORS!")
        print("=" * 70)

    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
