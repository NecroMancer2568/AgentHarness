import asyncio
import json
import os
import platform
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from app.config import Settings
from app.dependencies import (
    sanitize_credentials,
    normalize_package_name,
    parse_requirements_txt,
    parse_pyproject_toml,
    parse_setup_py,
    parse_poetry_lock,
    parse_uv_lock,
    detect_python_version_requirement,
    check_python_version_satisfies,
    resolve_compatible_python,
    run_process_safely,
    build_cache_key,
    inspect_repository_dependencies,
    is_declared_dependency,
    prepare_environment,
    DEPENDENCY_STATES,
)
from app.repository import ToolRuntime, parse_test_output
from app.runner import compute_verification_state


class DependencyManagementTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="ah_deptest_"))
        self.settings = Settings()
        self.settings.agentharness_repos_root = str(self.tmp_dir / "repos")
        self.settings.agentharness_runs_root = str(self.tmp_dir / "runs")
        self.settings.agentharness_venv_cache_root = str(self.tmp_dir / "venvs")
        self.settings.agentharness_pip_cache_dir = str(self.tmp_dir / "pip_cache")
        self.settings.agentharness_dependency_timeout_seconds = 60

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_credential_sanitization(self):
        raw = "Downloading https://user:secret123@private-pypi.org/packages/demo.whl"
        sanitized = sanitize_credentials(raw)
        self.assertNotIn("secret123", sanitized)
        self.assertNotIn("user:", sanitized)
        self.assertIn("https://***:***@private-pypi.org/packages/demo.whl", sanitized)

    def test_manifest_parsers(self):
        # 1. requirements.txt
        req_content = """
        # Comments
        --index-url https://pypi.org/simple
        requests>=2.28.0
        pandas==2.1.0; python_version >= '3.10'
        numpy
        """
        req_pkgs = parse_requirements_txt(req_content)
        self.assertIn("requests", req_pkgs)
        self.assertIn("pandas", req_pkgs)
        self.assertIn("numpy", req_pkgs)

        # 2. pyproject.toml
        toml_content = """
        [project]
        name = "my-project"
        dependencies = [
            "fastapi>=0.100.0",
            "uvicorn[standard]",
            "pydantic"
        ]
        """
        toml_pkgs = parse_pyproject_toml(toml_content)
        self.assertIn("fastapi", toml_pkgs)
        self.assertIn("uvicorn", toml_pkgs)
        self.assertIn("pydantic", toml_pkgs)

        # 3. setup.py
        setup_content = """
        from setuptools import setup
        setup(
            name="demo",
            install_requires=[
                "scikit-learn>=1.0",
                "scipy"
            ]
        )
        """
        setup_pkgs = parse_setup_py(setup_content)
        self.assertIn("scikit-learn", setup_pkgs)
        self.assertIn("scipy", setup_pkgs)

    def test_poetry_and_uv_lock_parsers(self):
        # poetry.lock
        poetry_lock = """
        [[package]]
        name = "fastapi"
        version = "0.110.0"

        [[package]]
        name = "pandas"
        version = "2.2.0"

        [[package]]
        name = "torch"
        version = "2.2.1"
        """
        poetry_pkgs = parse_poetry_lock(poetry_lock)
        self.assertIn("fastapi", poetry_pkgs)
        self.assertIn("pandas", poetry_pkgs)
        self.assertIn("torch", poetry_pkgs)

        # uv.lock
        uv_lock = """
        version = 1
        revision = 1

        [[package]]
        name = "transformers"
        version = "4.38.2"

        [[package]]
        name = "scikit-learn"
        version = "1.4.1"
        """
        uv_pkgs = parse_uv_lock(uv_lock)
        self.assertIn("transformers", uv_pkgs)
        self.assertIn("scikit-learn", uv_pkgs)

    def test_is_declared_dependency(self):
        declared = ["scikit-learn", "pyyaml", "requests", "torch", "transformers"]
        self.assertTrue(is_declared_dependency("requests", declared))
        self.assertTrue(is_declared_dependency("Scikit_Learn", declared))
        self.assertTrue(is_declared_dependency("sklearn", declared))
        self.assertTrue(is_declared_dependency("yaml", declared))
        self.assertTrue(is_declared_dependency("torch", declared))
        self.assertTrue(is_declared_dependency("transformers", declared))
        self.assertFalse(is_declared_dependency("pandas", declared))
        self.assertFalse(is_declared_dependency("yfinance", declared))

    def test_inspect_repository_dependencies(self):
        workspace = self.tmp_dir / "repo"
        workspace.mkdir(parents=True)
        (workspace / "requirements.txt").write_text("pandas>=2.0\npytest\n")
        (workspace / "pyproject.toml").write_text('[project]\ndependencies = ["requests"]\n')
        (workspace / "poetry.lock").write_text('[[package]]\nname = "torch"\nversion = "2.1.0"\n')
        (workspace / "uv.lock").write_text('version = 1\n[[package]]\nname = "joblib"\nversion = "1.3.0"\n')

        info = inspect_repository_dependencies(workspace)
        self.assertEqual(info["language"], "python")
        self.assertTrue(info["has_manifest"])
        self.assertIn("pandas", info["declared_packages"])
        self.assertIn("requests", info["declared_packages"])
        self.assertIn("pytest", info["declared_packages"])
        self.assertIn("torch", info["declared_packages"])
        self.assertIn("joblib", info["declared_packages"])
        self.assertTrue(len(info["combined_hash"]) > 10)
        self.assertEqual(len(info["manifests"]), 4)

    def test_python_version_requirement_detection(self):
        ws = self.tmp_dir / "pyver_repo"
        ws.mkdir(parents=True)

        # 1. .python-version
        (ws / ".python-version").write_text("3.11.8\n")
        req1 = detect_python_version_requirement(ws)
        self.assertIsNotNone(req1)
        self.assertEqual(req1["specifier"], "3.11.8")
        self.assertEqual(req1["source"], ".python-version")
        (ws / ".python-version").unlink()

        # 2. runtime.txt
        (ws / "runtime.txt").write_text("python-3.12.2\n")
        req2 = detect_python_version_requirement(ws)
        self.assertIsNotNone(req2)
        self.assertEqual(req2["specifier"], "3.12.2")
        (ws / "runtime.txt").unlink()

        # 3. pyproject.toml requires-python
        (ws / "pyproject.toml").write_text('[project]\nrequires-python = ">=3.10,<3.13"\n')
        req3 = detect_python_version_requirement(ws)
        self.assertIsNotNone(req3)
        self.assertEqual(req3["specifier"], ">=3.10,<3.13")

    def test_check_python_version_satisfies(self):
        self.assertTrue(check_python_version_satisfies("3.11.5", ">=3.10,<3.13"))
        self.assertTrue(check_python_version_satisfies("3.12.1", ">=3.10,<3.13"))
        self.assertFalse(check_python_version_satisfies("3.13.5", ">=3.10,<3.13"))
        self.assertTrue(check_python_version_satisfies("3.13.5", "^3.10"))
        self.assertTrue(check_python_version_satisfies("3.11.0", "3.11"))
        self.assertFalse(check_python_version_satisfies("3.13.5", "3.11"))

    def test_build_cache_key_includes_platform_arch_python(self):
        key1 = build_cache_key("repo_a", "abcdef1234567890", "3.13.5", platform_name="darwin", arch_name="arm64")
        key2 = build_cache_key("repo_a", "abcdef1234567890", "3.12.2", platform_name="darwin", arch_name="arm64")
        key3 = build_cache_key("repo_a", "abcdef1234567890", "3.13.5", platform_name="linux", arch_name="x86_64")
        key4 = build_cache_key("repo_a", "9999991234567890", "3.13.5", platform_name="darwin", arch_name="arm64")

        # Must differ when python, platform, or manifest hash differs
        self.assertIn("darwin", key1)
        self.assertIn("arm64", key1)
        self.assertIn("py3.13", key1)
        self.assertNotEqual(key1, key2)
        self.assertNotEqual(key1, key3)
        self.assertNotEqual(key1, key4)

    def test_undeclared_missing_dependency_outcome(self):
        stdout = "Traceback (most recent call last):\nModuleNotFoundError: No module named 'matplotlib'"
        analysis = parse_test_output(
            command=["pytest"],
            exit_code=1,
            stdout=stdout,
            stderr="",
            declared_packages=["pandas", "pytest"],
        )
        self.assertEqual(analysis["outcome"], "MISSING_DEPENDENCY")
        self.assertIn("undeclared", analysis["error_message"])

    def test_declared_missing_dependency_outcome(self):
        stdout = "Traceback (most recent call last):\nModuleNotFoundError: No module named 'pandas'"
        analysis = parse_test_output(
            command=["pytest"],
            exit_code=1,
            stdout=stdout,
            stderr="",
            declared_packages=["pandas", "pytest"],
        )
        self.assertEqual(analysis["outcome"], "BLOCKED")
        self.assertIn("Missing dependency: pandas", analysis["error_message"])

    def test_verification_state_with_dependency_failure(self):
        # Verification state must report DEPENDENCY_INSTALL_FAILED when prep failed
        state, tp, ra, err, _ = compute_verification_state(
            tests=[],
            reviewer_output="VERDICT: APPROVE",
            dependency_status="DEPENDENCY_INSTALL_FAILED",
            dependency_error="pip install failed",
        )
        self.assertEqual(state, "DEPENDENCY_INSTALL_FAILED")
        self.assertFalse(tp)

        # Verification state must report DEPENDENCY_INSTALL_BLOCKED when prep timed out
        state, tp, ra, err, _ = compute_verification_state(
            tests=[],
            reviewer_output="VERDICT: APPROVE",
            dependency_status="DEPENDENCY_INSTALL_BLOCKED",
            dependency_error="Dependency installation timed out",
        )
        self.assertEqual(state, "DEPENDENCY_INSTALL_BLOCKED")
        self.assertFalse(tp)

    def test_tool_runtime_prevents_test_execution_when_dependency_install_blocked(self):
        workspace = self.tmp_dir / "blocked_repo"
        workspace.mkdir(parents=True)
        runtime = ToolRuntime(
            workspace,
            self.settings,
            "pytest",
            dependency_status="DEPENDENCY_INSTALL_BLOCKED",
        )
        res = asyncio.run(runtime.run_test_command(["pytest", "-q"], timeout=30))
        self.assertEqual(res["outcome"], "BLOCKED")
        self.assertFalse(res["passed"])
        self.assertEqual(res["metrics"]["collected"], 0)
        self.assertEqual(res["metrics"]["passed"], 0)
        self.assertIn("dependency installation was not completed", res["error_message"])

    def test_run_process_safely_timeout_and_termination(self):
        # Run a long sleep command and verify safe cancellation and timeout reporting
        cmd = [sys.executable, "-c", "import time; time.sleep(10)"]
        rc, stdout, stderr, timed_out = asyncio.run(
            run_process_safely(cmd, cwd=self.tmp_dir, timeout=0.5)
        )
        self.assertTrue(timed_out)
        self.assertEqual(rc, -1)

    def test_installation_timeout_produces_dependency_install_blocked_with_diagnostics(self):
        # Reproduce original failure scenario: timeout during installation
        workspace = self.tmp_dir / "timeout_scenario"
        workspace.mkdir(parents=True)
        (workspace / "requirements.txt").write_text("fastapi\npandas\ntorch\n")

        # Mock run_process_safely to simulate timeout during pip install
        call_count = 0
        original_run = run_process_safely

        async def mock_run_process(cmd, cwd, timeout, env=None):
            nonlocal call_count
            call_count += 1
            if "venv" in cmd:
                return await original_run(cmd, cwd=cwd, timeout=timeout, env=env)
            # Simulate pip install timing out after exceeding budget
            return -1, "Collecting torch\nDownloading torch-2.2.0-cp313.whl (2800 MB)", "Connection timed out", True

        with patch("app.dependencies.run_process_safely", side_effect=mock_run_process):
            self.settings.agentharness_dependency_timeout_seconds = 10
            res = asyncio.run(prepare_environment(workspace, self.settings, "timeout_scenario"))
            self.assertEqual(res["status"], "DEPENDENCY_INSTALL_BLOCKED")
            self.assertFalse(res["cached"])
            self.assertIn("timed out", res["error"])
            self.assertIn("diagnostics", res)
            self.assertEqual(res["diagnostics"]["reason"], "TIMEOUT")
            self.assertIn("actionable_suggestion", res["diagnostics"])
            # Ensure ready marker was NOT created
            ready_marker = res["venv_path"] / ".agentharness_ready"
            self.assertFalse(ready_marker.exists())

    def test_cache_invalidation_on_manifest_change(self):
        workspace = self.tmp_dir / "cache_invalidation_repo"
        workspace.mkdir(parents=True)
        req = workspace / "requirements.txt"
        req.write_text("iniconfig\n")

        # 1. First preparation
        res1 = asyncio.run(prepare_environment(workspace, self.settings, "cache_invalidation_repo"))
        self.assertEqual(res1["status"], "DEPENDENCIES_READY")
        self.assertFalse(res1["cached"])

        # 2. Modify manifest: add another dependency
        req.write_text("iniconfig\npackaging\n")
        res2 = asyncio.run(prepare_environment(workspace, self.settings, "cache_invalidation_repo"))
        self.assertEqual(res2["status"], "DEPENDENCIES_READY")
        # Must NOT be cached because manifest hash changed!
        self.assertFalse(res2["cached"])
        self.assertNotEqual(res1["manifest_hash"], res2["manifest_hash"])

    def test_cache_invalidation_on_python_or_platform_mismatch(self):
        workspace = self.tmp_dir / "platform_mismatch_repo"
        workspace.mkdir(parents=True)
        (workspace / "requirements.txt").write_text("iniconfig\n")

        res1 = asyncio.run(prepare_environment(workspace, self.settings, "platform_mismatch_repo"))
        self.assertEqual(res1["status"], "DEPENDENCIES_READY")
        venv_path = res1["venv_path"]
        ready_marker = venv_path / ".agentharness_ready"
        self.assertTrue(ready_marker.is_file())

        # Tamper marker to simulate cache from a different platform/python
        data = json.loads(ready_marker.read_text())
        data["platform"] = "other_os"
        ready_marker.write_text(json.dumps(data))

        # Second preparation must detect platform mismatch and rebuild instead of erroneously reusing
        res2 = asyncio.run(prepare_environment(workspace, self.settings, "platform_mismatch_repo"))
        self.assertEqual(res2["status"], "DEPENDENCIES_READY")
        self.assertFalse(res2["cached"])

    def test_environment_isolation_and_caching(self):
        workspace = self.tmp_dir / "sample_repo"
        workspace.mkdir(parents=True)
        (workspace / "requirements.txt").write_text("iniconfig\n")

        # 1. First preparation: provisions venv and installs iniconfig + pytest
        res1 = asyncio.run(prepare_environment(workspace, self.settings, "sample_repo"))
        self.assertEqual(res1["status"], "DEPENDENCIES_READY")
        self.assertFalse(res1["cached"])
        venv_path = res1["venv_path"]
        self.assertTrue(venv_path.is_dir())
        self.assertTrue((venv_path / ".agentharness_ready").is_file())

        # 2. Second preparation on same workspace: must be CACHED
        res2 = asyncio.run(prepare_environment(workspace, self.settings, "sample_repo"))
        self.assertEqual(res2["status"], "DEPENDENCIES_READY")
        self.assertTrue(res2["cached"])

        # 3. ToolRuntime uses the isolated venv
        runtime = ToolRuntime(workspace, self.settings, "pytest", venv_path=venv_path, declared_packages=["iniconfig"])
        env = runtime._build_env()
        self.assertEqual(env["VIRTUAL_ENV"], str(venv_path))
        self.assertTrue(env["PATH"].startswith(f"{venv_path}/bin:"))

        # Check executable resolution
        resolved = runtime._resolve_executable(["pytest"])
        self.assertEqual(resolved[0], str(venv_path / "bin" / "pytest"))

    def test_ml_dependency_configuration_and_cpu_torch_extra_index(self):
        # Test repository declaring the user's exact ML packages
        workspace = self.tmp_dir / "ml_repo"
        workspace.mkdir(parents=True)
        ml_manifest = (
            "fastapi\njoblib\npandas\npydantic\nrequests\n"
            "scikit-learn\nsqlalchemy\ntorch\ntransformers\nuvicorn\nyfinance\n"
        )
        (workspace / "requirements.txt").write_text(ml_manifest)

        info = inspect_repository_dependencies(workspace)
        self.assertIn("torch", info["declared_packages"])
        self.assertIn("transformers", info["declared_packages"])
        self.assertIn("pandas", info["declared_packages"])
        self.assertIn("scikit-learn", info["declared_packages"])

        # Simulate pip command execution to inspect the generated command arguments
        captured_cmds = []

        async def capture_proc(cmd, cwd, timeout, env=None):
            captured_cmds.append(cmd)
            # Create dummy bin/pytest and bin/python to satisfy readiness
            if "venv" in cmd:
                venv_dir = Path(cmd[3])
                (venv_dir / "bin").mkdir(parents=True, exist_ok=True)
                (venv_dir / "bin" / "python").write_text("#!/bin/sh\nexit 0\n")
                (venv_dir / "bin" / "python").chmod(0o755)
                (venv_dir / "bin" / "pytest").write_text("#!/bin/sh\nexit 0\n")
                (venv_dir / "bin" / "pytest").chmod(0o755)
                return 0, "", "", False
            return 0, "Successfully installed", "", False

        # On x86_64 CPU: adds download.pytorch.org/whl/cpu
        with patch("app.dependencies.run_process_safely", side_effect=capture_proc), \
             patch("platform.machine", return_value="x86_64"):
            res = asyncio.run(prepare_environment(workspace, self.settings, "ml_repo_x86"))
            self.assertEqual(res["status"], "DEPENDENCIES_READY")

            pip_call = next(c for c in captured_cmds if "install" in c)
            self.assertIn("--cache-dir", pip_call)
            self.assertIn("--prefer-binary", pip_call)
            self.assertIn("https://download.pytorch.org/whl/cpu", pip_call)
            self.assertIn("-r", pip_call)

        # On aarch64: whl/cpu must NOT be added (aarch64 wheels are on PyPI)
        captured_cmds.clear()
        with patch("app.dependencies.run_process_safely", side_effect=capture_proc), \
             patch("platform.machine", return_value="aarch64"):
            res_arm = asyncio.run(prepare_environment(workspace, self.settings, "ml_repo_arm"))
            self.assertEqual(res_arm["status"], "DEPENDENCIES_READY")

            pip_call_arm = next(c for c in captured_cmds if "install" in c)
            self.assertNotIn("https://download.pytorch.org/whl/cpu", pip_call_arm)


if __name__ == "__main__":
    unittest.main()
