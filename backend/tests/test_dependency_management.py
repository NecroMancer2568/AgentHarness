import asyncio
import json
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

    def test_is_declared_dependency(self):
        declared = ["scikit-learn", "pyyaml", "requests"]
        # Exact and normalized matches
        self.assertTrue(is_declared_dependency("requests", declared))
        self.assertTrue(is_declared_dependency("Scikit_Learn", declared))
        # Module-to-package mappings
        self.assertTrue(is_declared_dependency("sklearn", declared))
        self.assertTrue(is_declared_dependency("yaml", declared))
        # Undeclared packages
        self.assertFalse(is_declared_dependency("pandas", declared))
        self.assertFalse(is_declared_dependency("torch", declared))

    def test_inspect_repository_dependencies(self):
        workspace = self.tmp_dir / "repo"
        workspace.mkdir(parents=True)
        (workspace / "requirements.txt").write_text("pandas>=2.0\npytest\n")
        (workspace / "pyproject.toml").write_text('[project]\ndependencies = ["requests"]\n')

        info = inspect_repository_dependencies(workspace)
        self.assertEqual(info["language"], "python")
        self.assertTrue(info["has_manifest"])
        self.assertIn("pandas", info["declared_packages"])
        self.assertIn("requests", info["declared_packages"])
        self.assertIn("pytest", info["declared_packages"])
        self.assertTrue(len(info["combined_hash"]) > 10)

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

        # Verification state must report MISSING_DEPENDENCY when tests failed due to undeclared dependency
        missing_dep_test = [{
            "command": ["pytest"],
            "outcome": "MISSING_DEPENDENCY",
            "passed": False,
            "error_message": "Missing dependency: unknown_lib (undeclared dependency)",
        }]
        state, tp, ra, err, _ = compute_verification_state(
            tests=missing_dep_test,
            reviewer_output="VERDICT: APPROVE",
            dependency_status="DEPENDENCIES_READY",
        )
        self.assertEqual(state, "MISSING_DEPENDENCY")
        self.assertFalse(tp)

    def test_environment_isolation_and_caching(self):
        # Create a workspace with a light package
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


if __name__ == "__main__":
    unittest.main()

