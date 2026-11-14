import asyncio
import io
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Any
from .config import Settings
from .dependencies import is_declared_dependency

ALLOWED_TEST_COMMANDS = {
    "auto": None,
    "pytest": ["python", "-m", "pytest", "-q"],
    "python -m pytest": ["python", "-m", "pytest", "-q"],
    "python -m pytest -q": ["python", "-m", "pytest", "-q"],
    "pytest -q tests/test_is_even.py": ["python", "-m", "pytest", "-q", "tests/test_is_even.py"],
    "pytest -q tests/test_normalize_email.py": ["python", "-m", "pytest", "-q", "tests/test_normalize_email.py"],
    "pytest -q tests/test_safe_divide.py": ["python", "-m", "pytest", "-q", "tests/test_safe_divide.py"],
    "pytest -q tests/test_unique_preserving_order.py": ["python", "-m", "pytest", "-q", "tests/test_unique_preserving_order.py"],
    "npm test": ["npm", "test"],
    "npm run test": ["npm", "run", "test"],
    "cargo test": ["cargo", "test"],
    "go test ./...": ["go", "test", "./..."],
    "mvn test": ["mvn", "-q", "test"],
    "gradle test": ["gradle", "test"],
    "ctest": ["ctest", "--output-on-failure"],
    "make test": ["make", "test"],
}

SAFE_TEST_BINARY_PREFIXES = {
    "pytest", "python", "npm", "cargo", "go", "mvn", "gradle", "ctest", "make"
}

def is_allowed_test_command(command: str) -> bool:
    if command in ALLOWED_TEST_COMMANDS:
        return True
    # Reject shell metacharacters and control characters
    if any(char in command for char in ";&|`$<>\n\r\x00"):
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    if not tokens:
        return False
    
    first = tokens[0]
    if first not in SAFE_TEST_BINARY_PREFIXES:
        return False

    # Check for path traversal anywhere in arguments
    for token in tokens:
        if ".." in token:
            return False
        if token.startswith("/") and not token.startswith("/"):
            return False

    # Python/pytest scoped commands
    if (tokens[:2] == ["pytest", "-q"] or tokens[:2] == ["pytest", "-v"] or tokens[:1] == ["pytest"] or
        tokens[:4] == ["python", "-m", "pytest", "-q"] or tokens[:4] == ["python", "-m", "pytest", "-v"] or
        tokens[:3] == ["python", "-m", "pytest"] or tokens[:3] == ["python", "-m", "unittest"]):
        # Check target path if present
        for arg in tokens[1:]:
            if not arg.startswith("-"):
                path = Path(arg)
                if path.is_absolute() or ".." in path.parts:
                    return False
        return True

    # npm test commands
    if tokens[:2] == ["npm", "test"] or tokens[:3] == ["npm", "run", "test"]:
        return True

    # cargo, go, ctest, make test
    if tokens[:2] == ["cargo", "test"] or tokens[:2] == ["go", "test"] or tokens[:1] == ["ctest"] or tokens[:2] == ["make", "test"]:
        return True

    return False


def resolve_test_command(command: str, workspace: Path) -> list[str] | None:
    if command in ALLOWED_TEST_COMMANDS:
        configured = ALLOWED_TEST_COMMANDS[command]
        if configured is not None:
            return list(configured)
        return None
    if not is_allowed_test_command(command):
        return None
    tokens = shlex.split(command)
    if not tokens:
        return None

    # For scoped pytest commands specifying a file target (e.g. pytest -q tests/test_foo.py)
    start_idx = 3 if tokens[:3] == ["python", "-m", "pytest"] else 1
    path_args = [t for t in tokens[start_idx:] if not t.startswith("-") and not t.startswith("@")]
    if path_args:
        target = path_args[-1]
        target_path = (workspace / target).resolve()
        try:
            target_path.relative_to(workspace.resolve())
        except ValueError:
            return None
        if not target_path.exists():
            return None

    if tokens[0] == "pytest":
        flags = [t for t in tokens[1:] if t.startswith("-")]
        if "-q" not in flags and "-v" not in flags:
            flags.insert(0, "-q")
        args = [t for t in tokens[1:] if not t.startswith("-")]
        return ["python", "-m", "pytest"] + flags + args
    if tokens[:3] == ["python", "-m", "pytest"]:
        flags = [t for t in tokens[3:] if t.startswith("-")]
        if "-q" not in flags and "-v" not in flags:
            flags.insert(0, "-q")
        args = [t for t in tokens[3:] if not t.startswith("-")]
        return ["python", "-m", "pytest"] + flags + args
    if tokens[:3] == ["python", "-m", "unittest"]:
        return ["python", "-m", "unittest"] + tokens[3:]

    return list(tokens)


def parse_test_output(
    command: list[str],
    exit_code: int | None,
    stdout: str,
    stderr: str,
    timed_out: bool = False,
    timeout_seconds: int = 180,
    declared_packages: list[str] | None = None,
) -> dict[str, Any]:
    """Parse test execution outputs into explicit verification outcomes and test metrics."""
    combined = f"{stdout}\n{stderr}".strip()
    cmd_str = " ".join(command)

    failed_tests = re.findall(r"FAILED\s+([^\s]+)", combined)
    if not failed_tests:
        failed_tests = re.findall(r"_{1,}\s*([a-zA-Z0-9_]+)\s*_{1,}", combined)
    if not failed_tests:
        failed_tests = re.findall(r"FAIL:\s*([a-zA-Z0-9_]+)", combined)
    failure_details = sorted(list(set(failed_tests)))

    if timed_out:
        return {
            "outcome": "BLOCKED",
            "passed": False,
            "error_type": "timeout",
            "error_message": f"Test execution timed out after {timeout_seconds}s.",
            "metrics": {"collected": 0, "passed": 0, "failed": 0, "skipped": 0, "errors": 0},
            "failure_details": failure_details,
        }

    # Missing tool / runtime
    if exit_code is None or "Command unavailable" in combined:
        return {
            "outcome": "BLOCKED",
            "passed": False,
            "error_type": "missing_runtime",
            "error_message": combined[:400] or "Command runtime unavailable.",
            "metrics": {"collected": 0, "passed": 0, "failed": 0, "skipped": 0, "errors": 0},
            "failure_details": failure_details,
        }

    # Missing dependencies or import failures
    missing_dep = re.search(r"ModuleNotFoundError:\s*No module named\s*['\"]([^'\"]+)['\"]", combined)
    if missing_dep and exit_code != 0:
        dep_name = missing_dep.group(1)
        if declared_packages is None:
            outcome = "BLOCKED"
            err_msg = f"Missing dependency: {dep_name}"
        else:
            declared = is_declared_dependency(dep_name, declared_packages)
            outcome = "BLOCKED" if declared else "MISSING_DEPENDENCY"
            err_msg = f"Missing dependency: {dep_name}" + ("" if declared else " (undeclared dependency)")
        return {
            "outcome": outcome,
            "passed": False,
            "error_type": "missing_dependency",
            "error_message": err_msg,
            "metrics": {"collected": 0, "passed": 0, "failed": 0, "skipped": 0, "errors": 1},
            "failure_details": failure_details or [dep_name],
        }

    import_err = re.search(r"ImportError:\s*(.*)", combined)
    if import_err and exit_code != 0:
        outcome = "BLOCKED" if declared_packages is None else "MISSING_DEPENDENCY"
        return {
            "outcome": outcome,
            "passed": False,
            "error_type": "import_error",
            "error_message": f"Import error: {import_err.group(1).strip()}",
            "metrics": {"collected": 0, "passed": 0, "failed": 0, "skipped": 0, "errors": 1},
            "failure_details": failure_details or [import_err.group(1).strip()[:100]],
        }

    syntax_err = re.search(r"SyntaxError:\s*(.*)", combined)
    if syntax_err and exit_code != 0:
        return {
            "outcome": "BLOCKED",
            "passed": False,
            "error_type": "syntax_error",
            "error_message": f"Syntax error: {syntax_err.group(1).strip()}",
            "metrics": {"collected": 0, "passed": 0, "failed": 0, "skipped": 0, "errors": 1},
            "failure_details": failure_details,
        }

    # Pytest specific handling
    if "pytest" in cmd_str:
        if exit_code == 5 or "no tests ran" in combined.lower():
            return {
                "outcome": "NO_TESTS",
                "passed": False,
                "error_type": "no_tests_collected",
                "error_message": "No tests were collected (pytest exit code 5: NO_TESTS_COLLECTED).",
                "metrics": {"collected": 0, "passed": 0, "failed": 0, "skipped": 0, "errors": 0},
                "failure_details": [],
            }

        p_match = re.search(r"(\d+)\s+passed", combined)
        f_match = re.search(r"(\d+)\s+failed", combined)
        s_match = re.search(r"(\d+)\s+skipped", combined)
        e_match = re.search(r"(\d+)\s+error(?:s)?", combined)
        c_match = re.search(r"collected\s+(\d+)\s+item", combined)

        passed_cnt = int(p_match.group(1)) if p_match else 0
        failed_cnt = int(f_match.group(1)) if f_match else 0
        skipped_cnt = int(s_match.group(1)) if s_match else 0
        errors_cnt = int(e_match.group(1)) if e_match else 0
        collected_cnt = int(c_match.group(1)) if c_match else (passed_cnt + failed_cnt + skipped_cnt + errors_cnt)

        if collected_cnt == 0 and exit_code != 0:
            return {
                "outcome": "NO_TESTS",
                "passed": False,
                "error_type": "no_tests_collected",
                "error_message": "Pytest collected 0 tests.",
                "metrics": {"collected": 0, "passed": 0, "failed": 0, "skipped": 0, "errors": errors_cnt},
                "failure_details": failure_details,
            }

        if exit_code == 0 and passed_cnt > 0 and failed_cnt == 0 and errors_cnt == 0:
            return {
                "outcome": "PASS",
                "passed": True,
                "error_type": None,
                "error_message": None,
                "metrics": {"collected": collected_cnt, "passed": passed_cnt, "failed": failed_cnt, "skipped": skipped_cnt, "errors": errors_cnt},
                "failure_details": [],
            }

        if failed_cnt > 0 or errors_cnt > 0 or exit_code != 0:
            return {
                "outcome": "FAIL",
                "passed": False,
                "error_type": "test_failure",
                "error_message": f"Pytest failed: {failed_cnt} failed, {errors_cnt} errors out of {collected_cnt} collected.",
                "metrics": {"collected": collected_cnt, "passed": passed_cnt, "failed": failed_cnt, "skipped": skipped_cnt, "errors": errors_cnt},
                "failure_details": failure_details,
            }

    # Python unittest handling
    if "unittest" in cmd_str:
        ran_match = re.search(r"Ran\s+(\d+)\s+test", combined)
        if ran_match:
            collected_cnt = int(ran_match.group(1))
            if "OK" in combined and exit_code == 0:
                return {
                    "outcome": "PASS",
                    "passed": True,
                    "error_type": None,
                    "error_message": None,
                    "metrics": {"collected": collected_cnt, "passed": collected_cnt, "failed": 0, "skipped": 0, "errors": 0},
                    "failure_details": [],
                }
            f_u = int(re.search(r"failures=(\d+)", combined).group(1)) if re.search(r"failures=(\d+)", combined) else 0
            e_u = int(re.search(r"errors=(\d+)", combined).group(1)) if re.search(r"errors=(\d+)", combined) else 0
            return {
                "outcome": "FAIL",
                "passed": False,
                "error_type": "test_failure",
                "error_message": f"Unittest failed: {f_u} failures, {e_u} errors out of {collected_cnt} ran.",
                "metrics": {"collected": collected_cnt, "passed": max(0, collected_cnt - f_u - e_u), "failed": f_u, "skipped": 0, "errors": e_u},
                "failure_details": failure_details,
            }

    # Cargo test handling
    if "cargo" in cmd_str:
        c_m = re.search(r"test result:\s*([a-zA-Z]+)\.\s*(\d+)\s+passed;\s*(\d+)\s+failed;\s*(\d+)\s+ignored", combined)
        if c_m:
            res_word = c_m.group(1).lower()
            p_c = int(c_m.group(2))
            f_c = int(c_m.group(3))
            s_c = int(c_m.group(4))
            tot = p_c + f_c + s_c
            is_p = (res_word == "ok" and exit_code == 0 and p_c > 0 and f_c == 0)
            return {
                "outcome": "PASS" if is_p else ("NO_TESTS" if tot == 0 else "FAIL"),
                "passed": is_p,
                "error_type": None if is_p else ("no_tests_collected" if tot == 0 else "test_failure"),
                "error_message": None if is_p else ("No cargo tests found." if tot == 0 else f"Cargo test failed: {f_c} failed."),
                "metrics": {"collected": tot, "passed": p_c, "failed": f_c, "skipped": s_c, "errors": 0},
                "failure_details": failure_details if not is_p else [],
            }

    # npm test handling
    if "npm" in cmd_str:
        j_pass = re.search(r"Tests:\s*(\d+)\s+passed", combined)
        j_fail = re.search(r"Tests:\s*(\d+)\s+failed", combined)
        j_total = re.search(r"Tests:\s*.*?(\d+)\s+total", combined)
        if j_pass or j_fail or j_total:
            p_n = int(j_pass.group(1)) if j_pass else 0
            f_n = int(j_fail.group(1)) if j_fail else 0
            t_n = int(j_total.group(1)) if j_total else (p_n + f_n)
            is_p = (exit_code == 0 and p_n > 0 and f_n == 0)
            return {
                "outcome": "PASS" if is_p else ("NO_TESTS" if t_n == 0 else "FAIL"),
                "passed": is_p,
                "error_type": None if is_p else ("no_tests_collected" if t_n == 0 else "test_failure"),
                "error_message": None if is_p else (f"npm test failed: {f_n} failed." if f_n > 0 else "No npm tests found."),
                "metrics": {"collected": t_n, "passed": p_n, "failed": f_n, "skipped": max(0, t_n - p_n - f_n), "errors": 0},
                "failure_details": failure_details if not is_p else [],
            }

    # Default fallback
    if exit_code == 0:
        return {
            "outcome": "PASS",
            "passed": True,
            "error_type": None,
            "error_message": None,
            "metrics": {"collected": 1, "passed": 1, "failed": 0, "skipped": 0, "errors": 0},
            "failure_details": [],
        }
    return {
        "outcome": "FAIL",
        "passed": False,
        "error_type": "command_failed",
        "error_message": f"Test command exited with code {exit_code}.",
        "metrics": {"collected": 0, "passed": 0, "failed": 1, "skipped": 0, "errors": 0},
        "failure_details": failure_details or [f"Exit code {exit_code}"],
    }


class WorkspaceError(RuntimeError): pass

class RepositoryManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        settings.repos_root.mkdir(parents=True, exist_ok=True)
        settings.runs_root.mkdir(parents=True, exist_ok=True)

    def _sanitize_repo_name(self, name: str) -> str:
        name = name.strip()
        if name.endswith(".git"):
            name = name[:-4]
        name = re.sub(r"[^a-zA-Z0-9._-]", "-", name)
        name = name.strip(".-")
        if not name:
            raise WorkspaceError("Invalid repository name. Use letters, numbers, hyphens, or underscores.")
        return name

    def _ensure_git_repository(self, target: Path, commit_message: str = "Initial repository baseline") -> None:
        target = target.resolve()
        git_dir = target / ".git"
        if git_dir.exists() and git_dir.is_dir():
            res = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=target, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if res.returncode == 0:
                return
            # Has .git but no commits
            try:
                subprocess.run(["git", "config", "user.name", "AgentHarness"], cwd=target, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run(["git", "config", "user.email", "agentharness@local.invalid"], cwd=target, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run(["git", "add", "-A"], cwd=target, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run(["git", "commit", "-q", "-m", commit_message], cwd=target, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except subprocess.CalledProcessError as exc:
                raise WorkspaceError(f"Failed to commit initial Git baseline: {exc}")
            return

        try:
            subprocess.run(["git", "init", "-q"], cwd=target, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.name", "AgentHarness"], cwd=target, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.email", "agentharness@local.invalid"], cwd=target, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["git", "add", "-A"], cwd=target, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["git", "commit", "-q", "-m", commit_message], cwd=target, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except subprocess.CalledProcessError as exc:
            raise WorkspaceError(f"Failed to initialize Git repository in {target.name}: {exc}")

    def list_repositories(self) -> list[dict[str, str]]:
        result = []
        for path in sorted(self.settings.repos_root.iterdir()):
            if path.is_dir() and not path.name.startswith("."):
                if not (path / ".git").exists():
                    try:
                        self._ensure_git_repository(path)
                    except Exception:
                        pass
                if (path / ".git").exists():
                    result.append({"name": path.name, "path": str(path.resolve())})
        return result

    def register_path(self, raw_path: str) -> dict[str, str]:
        path = Path(raw_path).expanduser().resolve()
        root = self.settings.repos_root.resolve()
        try:
            path.relative_to(root)
        except ValueError:
            raise WorkspaceError(f"Repository must be inside the configured repositories root: {root}")
        if not path.is_dir():
            raise WorkspaceError("Path must be an existing directory inside the repositories root.")
        self._ensure_git_repository(path, f"Registered {path.name}")
        return {"name": path.name, "path": str(path)}

    def resolve_repository(self, name_or_path: str) -> Path:
        root = self.settings.repos_root.resolve()
        candidate = Path(name_or_path).expanduser()
        path = (root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
        try:
            path.relative_to(root)
        except ValueError:
            raise WorkspaceError("Repository path is outside the configured repositories root.")
        if not path.is_dir():
            raise WorkspaceError(f"Repository directory not found inside {root}: {name_or_path}")
        self._ensure_git_repository(path, f"Initialized {path.name}")
        return path

    def _normalize_git_url(self, raw_url: str, branch: str | None = None) -> tuple[str, str | None, str]:
        """
        Normalize various GitHub/Git URL formats into (clone_url, branch, default_repo_name).
        Handles:
          - git clone https://github.com/owner/repo
          - https://github.com/owner/repo.git
          - https://github.com/owner/repo
          - https://github.com/owner/repo/tree/branch-name
          - https://github.com/owner/repo/blob/branch-name/path/file.py
          - github.com/owner/repo
          - owner/repo
          - git@github.com:owner/repo.git
          - file:///path/to/repo
        """
        url = raw_url.strip()
        if url.startswith("git clone "):
            url = url[len("git clone "):].strip()
        url = url.strip("'\"")

        if url.startswith("github.com/") or url.startswith("gitlab.com/"):
            url = f"https://{url}"
        elif "/" in url and not (url.startswith("http://") or url.startswith("https://") or url.startswith("git@") or url.startswith("ssh://") or url.startswith("file://")):
            parts = url.strip("/").split("/")
            if len(parts) == 2 and not any(c in url for c in " :\\"):
                url = f"https://github.com/{url}"

        detected_branch = branch
        gh_tree_match = re.match(r"^(https?://github\.com/[^/]+/[^/]+)/(?:tree|blob)/([^/]+)(?:/.*)?$", url)
        if gh_tree_match:
            url = gh_tree_match.group(1)
            if not detected_branch:
                detected_branch = gh_tree_match.group(2)

        if not (url.startswith("https://") or url.startswith("http://") or url.startswith("git@") or url.startswith("ssh://") or url.startswith("file://")):
            raise WorkspaceError("Invalid Git repository URL. Provide a valid GitHub link (e.g. https://github.com/owner/repo or owner/repo).")

        cleaned = url.rstrip("/")
        if cleaned.endswith(".git"):
            cleaned = cleaned[:-4]
        path_part = cleaned.split("/")[-1]
        if ":" in path_part:
            path_part = path_part.split(":")[-1]

        default_name = self._sanitize_repo_name(path_part)
        return url, detected_branch, default_name

    async def clone_repository(self, url: str, custom_name: str | None = None, branch: str | None = None) -> dict[str, str]:
        clone_url, target_branch, default_name = self._normalize_git_url(url, branch)
        repo_name = self._sanitize_repo_name(custom_name) if custom_name and custom_name.strip() else default_name

        target = (self.settings.repos_root / repo_name).resolve()
        root = self.settings.repos_root.resolve()
        try:
            target.relative_to(root)
        except ValueError:
            raise WorkspaceError("Target repository path is outside repositories root.")

        if target.exists():
            if (target / ".git").exists():
                raise WorkspaceError(f"A repository named '{repo_name}' already exists in workspace/repositories. Delete it or specify a custom name.")
            else:
                shutil.rmtree(target, ignore_errors=True)

        cmd = ["git", "clone", "--depth", "1", "--quiet"]
        if target_branch and target_branch.strip():
            cmd.extend(["--branch", target_branch.strip()])
        cmd.extend([clone_url, str(target)])

        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
        except asyncio.TimeoutError:
            proc.kill()
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            raise WorkspaceError("Git clone timed out after 3 minutes. Please check your internet connection or repository URL.")

        if proc.returncode != 0:
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            err_msg = stderr.decode(errors="replace").strip()
            if "could not read Username" in err_msg or "Authentication failed" in err_msg:
                raise WorkspaceError("Repository is private or requires authentication. Only public repositories or configured SSH keys are supported.")
            if "Remote branch" in err_msg and "not found" in err_msg:
                raise WorkspaceError(f"Branch '{target_branch}' not found in repository.")
            raise WorkspaceError(f"Failed to clone repository: {err_msg or 'Git clone failed.'}")

        self._ensure_git_repository(target, f"Initial clone of {clone_url}")
        return {"name": repo_name, "path": str(target)}

    async def import_zip(self, zip_bytes: bytes, filename: str, custom_name: str | None = None) -> dict[str, str]:
        if custom_name and custom_name.strip():
            repo_name = self._sanitize_repo_name(custom_name)
        else:
            base = Path(filename).stem
            if base.endswith(".tar"):
                base = Path(base).stem
            repo_name = self._sanitize_repo_name(base)

        target = (self.settings.repos_root / repo_name).resolve()
        root = self.settings.repos_root.resolve()
        try:
            target.relative_to(root)
        except ValueError:
            raise WorkspaceError("Target repository path is invalid.")

        if target.exists():
            raise WorkspaceError(f"A repository named '{repo_name}' already exists in workspace/repositories.")

        try:
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                bad_file = zf.testzip()
                if bad_file:
                    raise WorkspaceError(f"Corrupted zip archive: error in {bad_file}")

                members = zf.infolist()
                if not members:
                    raise WorkspaceError("The uploaded zip archive is empty.")

                top_parts = set()
                for m in members:
                    p = Path(m.filename).parts
                    if p:
                        top_parts.add(p[0])
                unwrap = (len(top_parts) == 1 and all(len(Path(m.filename).parts) > 1 or m.is_dir() for m in members))
                strip_prefix = list(top_parts)[0] if unwrap else None

                target.mkdir(parents=True, exist_ok=True)
                for m in members:
                    rel_name = m.filename
                    if strip_prefix and rel_name.startswith(strip_prefix):
                        rel_name = rel_name[len(strip_prefix):].lstrip("/\\")
                    if not rel_name:
                        continue
                    dest = (target / rel_name).resolve()
                    try:
                        dest.relative_to(target)
                    except ValueError:
                        raise WorkspaceError(f"Security error: Zip archive entry escapes destination: {m.filename}")

                    if m.is_dir():
                        dest.mkdir(parents=True, exist_ok=True)
                    else:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(m) as src, open(dest, "wb") as dst:
                            shutil.copyfileobj(src, dst)
        except Exception as exc:
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            if isinstance(exc, WorkspaceError):
                raise
            raise WorkspaceError(f"Failed to extract zip archive: {exc}")

        self._ensure_git_repository(target, f"Imported from {filename}")
        return {"name": repo_name, "path": str(target)}

    async def import_local_folder(self, source_path_str: str, custom_name: str | None = None) -> dict[str, str]:
        source = Path(source_path_str).expanduser().resolve()
        if not source.exists() or not source.is_dir():
            raise WorkspaceError(f"Local folder not found or is not a directory: {source_path_str}")

        if custom_name and custom_name.strip():
            repo_name = self._sanitize_repo_name(custom_name)
        else:
            repo_name = self._sanitize_repo_name(source.name)

        target = (self.settings.repos_root / repo_name).resolve()
        root = self.settings.repos_root.resolve()
        try:
            target.relative_to(root)
        except ValueError:
            raise WorkspaceError("Target repository path is invalid.")

        if source == target:
            self._ensure_git_repository(target, f"Initialized existing folder {repo_name}")
            return {"name": repo_name, "path": str(target)}

        if target.exists():
            raise WorkspaceError(f"A repository named '{repo_name}' already exists in workspace/repositories.")

        ignore_fn = shutil.ignore_patterns(".venv", "node_modules", "__pycache__", ".pytest_cache", ".DS_Store", "*.pyc")
        try:
            await asyncio.to_thread(shutil.copytree, source, target, ignore=ignore_fn)
        except Exception as exc:
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            raise WorkspaceError(f"Failed to copy local folder: {exc}")

        self._ensure_git_repository(target, f"Imported from {source}")
        return {"name": repo_name, "path": str(target)}

    def delete_repository(self, name: str) -> None:
        repo_name = self._sanitize_repo_name(name)
        target = (self.settings.repos_root / repo_name).resolve()
        root = self.settings.repos_root.resolve()
        try:
            target.relative_to(root)
        except ValueError:
            raise WorkspaceError("Invalid repository path.")

        if target == root:
            raise WorkspaceError("Cannot delete the root repositories directory.")

        if not target.exists():
            raise WorkspaceError(f"Repository '{repo_name}' not found.")

        shutil.rmtree(target, ignore_errors=False)

    async def create_workspace(self, repo: Path, run_id: str) -> Path:
        target = self.settings.runs_root / run_id / "repo"
        target.parent.mkdir(parents=True, exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            "git", "clone", "--local", "--no-hardlinks", "--quiet", str(repo), str(target),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise WorkspaceError(f"Could not create isolated repository copy: {stderr.decode(errors='replace')[-2000:]}")
        return target.resolve()

    async def cleanup_workspace(self, run_id: str):
        target = self.settings.runs_root / run_id
        if target.exists():
            await asyncio.to_thread(shutil.rmtree, target, True)


def _kill_process_tree(proc: asyncio.subprocess.Process | None):
    """Safely terminate a subprocess and all its child processes in its process group."""
    if not proc:
        return
    is_posix = sys.platform != "win32"
    try:
        if is_posix and hasattr(proc, "pid") and proc.pid:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        else:
            proc.terminate()
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.kill()
        if is_posix and hasattr(proc, "pid") and proc.pid:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


class ToolRuntime:
    def __init__(
        self,
        workspace: Path,
        settings: Settings,
        test_command: str,
        venv_path: Path | None = None,
        declared_packages: list[str] | None = None,
        event_callback=None,
        dependency_status: str = "DEPENDENCIES_READY",
        dep_info: dict[str, Any] | None = None,
    ):
        self.workspace = workspace.resolve()
        self.settings = settings
        self.test_command = test_command
        self.venv_path = venv_path.resolve() if venv_path else None
        self.declared_packages = declared_packages or []
        self.event_callback = event_callback
        self.dependency_status = dependency_status
        self.dep_info = dep_info or {}
        self.tool_count = 0
        self.test_results: list[dict[str, Any]] = []

    def _safe_path(self, raw: str) -> Path:
        raw = raw or "."
        path = (self.workspace / raw).resolve()
        try: path.relative_to(self.workspace)
        except ValueError: raise WorkspaceError("Path is outside the isolated repository workspace.")
        return path

    def _clip(self, text: str) -> str:
        limit = self.settings.agentharness_max_tool_output_chars
        return text if len(text) <= limit else text[:limit] + f"\n...[truncated at {limit} chars]"

    def tool_definitions(self, allowed: set[str]) -> list[dict[str, Any]]:
        specs = {
          "list_files": ("List repository files (ignores .git, node_modules, .venv, and build outputs).", {"path": {"type":"string"}}),
          "read_file": ("Read a UTF-8 text file in the repository.", {"path": {"type":"string"}}),
          "search_text": ("Search case-insensitively for text in tracked-like source files.", {"query": {"type":"string"}, "path": {"type":"string"}}),
          "write_file": ("Create or replace a text file in the isolated repository. Supply the complete intended file content.", {"path": {"type":"string"}, "content": {"type":"string"}}),
          "run_tests": ("Run the configured, allowlisted test command in the isolated repository.", {"command": {"type":"string"}}),
          "git_diff": ("Show the current git diff for the isolated repository.", {}),
          "discover_tests": ("Inspect the repository for test frameworks, test files, and recommended test commands.", {}),
        }
        result = []
        for name in sorted(allowed):
            desc, props = specs[name]
            required = list(props.keys())
            result.append({"type":"function","function":{"name":name,"description":desc,
              "parameters":{"type":"object","properties":props,"required":required,"additionalProperties":False}}})
        return result

    def detect_repository_tests(self) -> dict[str, Any]:
        """
        Examine repository workspace to discover testing framework, test files,
        test directories (including nonstandard ones), configurations, and conventions.
        """
        workspace = self.workspace
        test_files: list[str] = []
        test_dirs: set[str] = set()
        ignored = {".git", "node_modules", ".venv", "venv", "dist", "build", "__pycache__", ".next", ".agentharness"}

        # 1. Check configuration files for testpaths or custom test patterns
        configured_testpaths: list[str] = []
        config_file_found: str | None = None
        custom_python_files: list[str] = []

        # pytest.ini
        pytest_ini = workspace / "pytest.ini"
        if pytest_ini.is_file():
            config_file_found = "pytest.ini"
            try:
                txt = pytest_ini.read_text(encoding="utf-8", errors="replace")
                for line in txt.splitlines():
                    line = line.strip()
                    if line.startswith("testpaths"):
                        parts = line.split("=", 1)[1].strip().split()
                        configured_testpaths.extend(parts)
                    elif line.startswith("python_files"):
                        parts = line.split("=", 1)[1].strip().split()
                        custom_python_files.extend(parts)
            except Exception:
                pass

        # pyproject.toml
        pyproject = workspace / "pyproject.toml"
        if pyproject.is_file():
            try:
                txt = pyproject.read_text(encoding="utf-8", errors="replace")
                if "[tool.pytest" in txt:
                    if not config_file_found:
                        config_file_found = "pyproject.toml"
                    m_tp = re.search(r"testpaths\s*=\s*\[(.*?)\]", txt, re.S)
                    if m_tp:
                        paths = re.findall(r'["\']([^"\']+)["\']', m_tp.group(1))
                        configured_testpaths.extend(paths)
                    m_pf = re.search(r"python_files\s*=\s*\[(.*?)\]", txt, re.S)
                    if m_pf:
                        pfiles = re.findall(r'["\']([^"\']+)["\']', m_pf.group(1))
                        custom_python_files.extend(pfiles)
            except Exception:
                pass

        # setup.cfg
        setup_cfg = workspace / "setup.cfg"
        if setup_cfg.is_file():
            try:
                txt = setup_cfg.read_text(encoding="utf-8", errors="replace")
                if "[tool:pytest]" in txt or "[pytest]" in txt:
                    if not config_file_found:
                        config_file_found = "setup.cfg"
                    for line in txt.splitlines():
                        line = line.strip()
                        if line.startswith("testpaths"):
                            parts = line.split("=", 1)[1].strip().split()
                            configured_testpaths.extend(parts)
            except Exception:
                pass

        # tox.ini
        tox_ini = workspace / "tox.ini"
        if tox_ini.is_file():
            try:
                txt = tox_ini.read_text(encoding="utf-8", errors="replace")
                if "[pytest]" in txt and not config_file_found:
                    config_file_found = "tox.ini"
            except Exception:
                pass

        # Common test directory names
        known_test_dir_names = {
            "tests", "test", "testing", "spec", "specs", "test_suite", "test_suites",
            "unit_tests", "integration_tests", "functional_tests", "tests_unit", "tests_integration"
        }

        # 2. Walk workspace and identify test files and directories
        for p in workspace.rglob("*"):
            if any(part in ignored for part in p.parts):
                continue
            if p.is_file():
                lower = p.name.lower()
                rel = str(p.relative_to(workspace))
                rel_parts = p.relative_to(workspace).parts

                in_test_dir = any(part.lower() in known_test_dir_names for part in rel_parts[:-1])

                is_test = False
                # Python test patterns
                if p.suffix == ".py":
                    is_test = (
                        lower.startswith("test_")
                        or lower.endswith("_test.py")
                        or lower.endswith("_tests.py")
                        or lower.endswith("_spec.py")
                        or lower.startswith("test")
                        or (in_test_dir and (lower.startswith("t_") or lower.endswith("test.py") or "test" in lower))
                    )
                # JS / TS test patterns
                elif p.suffix in {".js", ".ts", ".jsx", ".tsx"}:
                    is_test = (
                        lower.endswith(".test.js") or lower.endswith(".test.ts")
                        or lower.endswith(".test.jsx") or lower.endswith(".test.tsx")
                        or lower.endswith(".spec.js") or lower.endswith(".spec.ts")
                        or lower.endswith(".spec.jsx") or lower.endswith(".spec.tsx")
                        or in_test_dir
                    )
                # Go test pattern
                elif p.suffix == ".go":
                    is_test = lower.endswith("_test.go")
                # Rust test pattern
                elif p.suffix == ".rs":
                    is_test = in_test_dir or lower.endswith("_test.rs")
                # Java test pattern
                elif p.suffix == ".java":
                    is_test = (
                        lower.endswith("test.java")
                        or lower.endswith("tests.java")
                        or lower.endswith("testcase.java")
                        or lower.startswith("test")
                        or in_test_dir
                    )
                # C / C++ test pattern
                elif p.suffix in {".c", ".cpp", ".cc"}:
                    is_test = in_test_dir or "test" in lower

                if is_test:
                    test_files.append(rel)
                    if len(rel_parts) > 1:
                        test_dirs.add(str(Path(*rel_parts[:-1])))

        has_python = bool(
            any(f.endswith(".py") for f in test_files)
            or list(workspace.glob("*.py"))
            or list(workspace.glob("*/*.py"))
            or (workspace / "requirements.txt").exists()
            or (workspace / "pyproject.toml").exists()
            or list(workspace.glob("requirements*.txt"))
            or list(workspace.glob("*/requirements*.txt"))
            or (self.dep_info.get("language") == "python")
            or next(workspace.rglob("*.py"), None) is not None
        )
        has_js = (workspace / "package.json").exists() or (self.dep_info.get("language") == "javascript/typescript") or any(f.endswith((".js", ".ts", ".jsx", ".tsx")) for f in test_files)
        has_rust = (workspace / "Cargo.toml").exists() or (self.dep_info.get("language") == "rust") or any(f.endswith(".rs") for f in test_files)
        has_go = (workspace / "go.mod").exists() or (self.dep_info.get("language") == "go") or any(f.endswith(".go") for f in test_files)
        has_java = (workspace / "pom.xml").exists() or (workspace / "build.gradle").exists() or (workspace / "build.gradle.kts").exists() or (self.dep_info.get("language") == "java") or any(f.endswith(".java") for f in test_files)
        has_c = (workspace / "CMakeLists.txt").exists() or (workspace / "Makefile").exists() or (self.dep_info.get("language") == "c_cpp") or any(f.endswith((".c", ".cpp", ".cc")) for f in test_files)

        is_nonstandard = False
        if test_dirs:
            if not any(d == "tests" or d.startswith("tests/") for d in test_dirs):
                is_nonstandard = True

        if has_python:
            lang = "python"
            framework = "pytest"

            if configured_testpaths:
                rec_cmd = f"python -m pytest -q {' '.join(configured_testpaths)}"
            elif is_nonstandard and test_dirs:
                primary_test_dir = sorted(list(test_dirs))[0]
                rec_cmd = f"python -m pytest -q {primary_test_dir}"
            elif test_files:
                rec_cmd = "python -m pytest -q"
            else:
                rec_cmd = "python -m pytest -q"

        elif has_js:
            lang = "javascript/typescript"
            framework = "npm test"
            rec_cmd = "npm test"
        elif has_rust:
            lang = "rust"
            framework = "cargo test"
            rec_cmd = "cargo test"
        elif has_go:
            lang = "go"
            framework = "go test"
            rec_cmd = "go test ./..."
        elif has_java:
            lang = "java"
            framework = "maven" if (workspace / "pom.xml").exists() else "gradle"
            rec_cmd = "mvn test" if (workspace / "pom.xml").exists() else "gradle test"
        elif has_c:
            lang = "c/cpp"
            framework = "ctest" if (workspace / "CMakeLists.txt").exists() else "make test"
            rec_cmd = "ctest" if (workspace / "CMakeLists.txt").exists() else "make test"
        else:
            lang = "unknown"
            framework = "unknown"
            rec_cmd = "auto"

        has_tests = len(test_files) > 0
        missing_notes = None
        if not has_tests:
            if lang == "python":
                target_dir = sorted(list(test_dirs))[0] if test_dirs else "tests"
                missing_notes = f"No test files were found. Coding Agent must create {target_dir}/test_<feature>.py with reproducible test assertions."
            elif lang == "javascript/typescript":
                missing_notes = "No test files were found. Coding Agent should create a meaningful test file."
            else:
                missing_notes = f"No test files found for {lang} repository."

        return {
            "detected_language": lang,
            "detected_framework": framework,
            "has_existing_tests": has_tests,
            "test_count": len(test_files),
            "existing_test_files": sorted(test_files[:50]),
            "test_directories": sorted(list(test_dirs)),
            "test_directory": sorted(list(test_dirs))[0] if test_dirs else "tests",
            "nonstandard_directory": is_nonstandard,
            "test_config_file": config_file_found,
            "configured_testpaths": configured_testpaths,
            "recommended_command": rec_cmd,
            "missing_coverage_notes": missing_notes,
        }

    async def execute(self, name: str, args: dict[str, Any]) -> str:
        self.tool_count += 1
        if name == "discover_tests":
            info = self.detect_repository_tests()
            return json.dumps(info, indent=2)
        if name == "list_files":
            base = self._safe_path(args.get("path", "."))
            if not base.exists() or not base.is_dir(): return "Directory not found."
            ignored = {".git", "node_modules", ".venv", "venv", "dist", "build", "__pycache__", ".next"}
            files = []
            for p in base.rglob("*"):
                if any(part in ignored for part in p.parts): continue
                if p.is_file(): files.append(str(p.relative_to(self.workspace)))
                if len(files) >= 300: break
            return self._clip("\n".join(files) or "No files found.")
        if name == "read_file":
            path = self._safe_path(args["path"])
            if not path.is_file(): return "File not found."
            if path.stat().st_size > 500_000: return "File exceeds 500 KB; inspect a smaller file."
            try: return self._clip(path.read_text(encoding="utf-8"))
            except UnicodeDecodeError: return "File is not UTF-8 text."
        if name == "search_text":
            query = str(args.get("query", ""))[:300].lower()
            base = self._safe_path(args.get("path", "."))
            ignored = {".git", "node_modules", ".venv", "venv", "dist", "build", "__pycache__", ".next"}
            hits = []
            if not query: return "Search query is empty."
            for p in base.rglob("*") if base.is_dir() else [base]:
                if not p.is_file() or any(part in ignored for part in p.parts): continue
                try:
                    if p.stat().st_size > 250_000: continue
                    for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                        if query in line.lower(): hits.append(f"{p.relative_to(self.workspace)}:{i}: {line[:300]}")
                        if len(hits) >= 100: break
                except (UnicodeDecodeError, OSError): continue
                if len(hits) >= 100: break
            return self._clip("\n".join(hits) or "No matches found.")
        if name == "write_file":
            path = self._safe_path(args["path"])
            if path.name in {".env", ".gitconfig"} or ".git" in path.parts:
                return "Refused to write a protected path."
            content = str(args.get("content", ""))
            if len(content) > 250_000: return "Refused: file content exceeds 250 KB."
            path.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(path.write_text, content, encoding="utf-8")
            return f"Wrote {path.relative_to(self.workspace)} ({len(content)} characters)."
        if name == "git_diff":
            # Include untracked files in the diff without staging their content. This workspace is disposable.
            await self._run_command(["git", "add", "-N", "--", "."], timeout=20)
            return await self._run_command(["git", "diff", "--no-ext-diff", "--", "."], timeout=20)
        if name == "run_tests":
            requested = str(args.get("command", "auto"))
            if not is_allowed_test_command(requested):
                return f"Refused. Allowed test commands: {', '.join(ALLOWED_TEST_COMMANDS)} or a scoped test command like 'pytest -q tests/test_example.py'."
            if self.test_command != "auto" and requested not in {"auto", self.test_command}:
                return f"Refused: the user selected '{self.test_command}'. Run that configured command instead."
            if requested == "auto" and self.test_command != "auto":
                requested = self.test_command
            command = self._detect_test_command() if requested == "auto" else resolve_test_command(requested, self.workspace)
            if not command:
                test_info = self.detect_repository_tests()
                lang = test_info.get("detected_language", "unknown")
                return f"No recognized test runner found for {lang} repository. If you added a test file, ensure it is in tests/ (e.g., tests/test_*.py) or choose an allowlisted test command."
            record = await self.run_test_command(command, timeout=self.settings.agentharness_test_timeout_seconds)
            return record["output"]
        return f"Unknown tool: {name}"

    def _detect_test_command(self) -> list[str] | None:
        test_info = self.detect_repository_tests()
        rec_cmd = test_info.get("recommended_command")
        if rec_cmd and rec_cmd != "auto":
            return resolve_test_command(rec_cmd, self.workspace) or rec_cmd.split()

        pkg = self.workspace / "package.json"
        if pkg.exists():
            try:
                data = json.loads(pkg.read_text())
                if "test" in data.get("scripts", {}): return ["npm", "test", "--", "--runInBand"]
            except Exception: pass
        if (self.workspace / "Cargo.toml").exists(): return ["cargo", "test"]
        if (self.workspace / "go.mod").exists(): return ["go", "test", "./..."]
        if (self.workspace / "pom.xml").exists(): return ["mvn", "-q", "test"]
        if (self.workspace / "build.gradle").exists() or (self.workspace / "build.gradle.kts").exists(): return ["gradle", "test"]
        if (self.workspace / "CMakeLists.txt").exists(): return ["ctest", "--output-on-failure"]
        if (self.workspace / "Makefile").exists(): return ["make", "test"]

        # If python repository without tests, use python -m pytest -q so newly added tests or exit code 5 are executed
        if (
            list(self.workspace.glob("*.py"))
            or list(self.workspace.glob("*/*.py"))
            or (self.workspace / "requirements.txt").exists()
            or next(self.workspace.rglob("*.py"), None) is not None
            or self.dep_info.get("language") == "python"
        ):
            return ["python", "-m", "pytest", "-q"]

        return None

    def _build_env(self) -> dict[str, str]:
        safe_env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "WINDIR", "LANG", "LC_ALL") if key in os.environ}
        safe_env.update({"CI": "1", "NO_COLOR": "1"})
        cur_pythonpath = os.environ.get("PYTHONPATH", "")
        safe_env["PYTHONPATH"] = f"{self.workspace}:{cur_pythonpath}".rstrip(":")
        if self.venv_path and self.venv_path.is_dir():
            safe_env["VIRTUAL_ENV"] = str(self.venv_path)
            safe_env["PATH"] = f"{self.venv_path / 'bin'}:{safe_env.get('PATH', '')}"
        elif "VIRTUAL_ENV" in os.environ:
            safe_env["VIRTUAL_ENV"] = os.environ["VIRTUAL_ENV"]
        return safe_env

    def _resolve_executable(self, command: list[str] | str) -> list[str]:
        if isinstance(command, str):
            exec_cmd = shlex.split(command)
        else:
            exec_cmd = list(command)
        if not exec_cmd:
            return exec_cmd
        if self.venv_path and self.venv_path.is_dir():
            py_in_venv = self.venv_path / "bin" / "python"
            pytest_in_venv = self.venv_path / "bin" / "pytest"
            if exec_cmd[0] in {"python", "python3"} and py_in_venv.is_file():
                exec_cmd[0] = str(py_in_venv)
            elif exec_cmd[0] == "pytest" and pytest_in_venv.is_file():
                exec_cmd[0] = str(pytest_in_venv)
        elif exec_cmd[0] == "python" and not shutil.which("python"):
            exec_cmd[0] = sys.executable or shutil.which("python3") or "python3"
        return exec_cmd

    async def run_test_command(self, command: list[str] | str, timeout: int) -> dict[str, Any]:
        """Execute test command, capturing stdout/stderr separately with metrics and outcome analysis."""
        if isinstance(command, str):
            command = resolve_test_command(command, self.workspace) or shlex.split(command)

        dep_status = getattr(self, "dependency_status", "DEPENDENCIES_READY")
        if dep_status != "DEPENDENCIES_READY":
            err = f"Test execution prevented: dependency installation was not completed ({dep_status})."
            self._emit("test_completed", err, {"outcome": "BLOCKED", "passed": False})
            res = {
                "command": command,
                "exit_code": -1,
                "stdout": "",
                "stderr": err,
                "duration_ms": 0,
                "timed_out": False,
                "metrics": {"collected": 0, "passed": 0, "failed": 0, "skipped": 0, "errors": 0},
                "outcome": "BLOCKED",
                "failure_details": [],
                "error_message": err,
                "passed": False,
            }
            self.test_results.append(res)
            return res

        self._emit("tool_started", "Running command: " + " ".join(command), {"command": command})
        started = time.perf_counter()
        proc = None
        try:
            safe_env = self._build_env()
            exec_cmd = self._resolve_executable(command)

            proc = await asyncio.create_subprocess_exec(
                *exec_cmd, cwd=self.workspace,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=safe_env,
                start_new_session=(sys.platform != "win32"),
            )
            timed_out = False
            try:
                stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
                stdout = stdout_b.decode(errors="replace")
                stderr = stderr_b.decode(errors="replace")
                exit_code = proc.returncode
            except asyncio.TimeoutError:
                _kill_process_tree(proc)
                stdout_b, stderr_b = await proc.communicate()
                stdout = stdout_b.decode(errors="replace")
                stderr = stderr_b.decode(errors="replace")
                exit_code = None
                timed_out = True
            except asyncio.CancelledError:
                _kill_process_tree(proc)
                await proc.communicate()
                raise

            duration_ms = round((time.perf_counter() - started) * 1000)
            analysis = parse_test_output(
                command, exit_code, stdout, stderr,
                timed_out=timed_out, timeout_seconds=timeout,
                declared_packages=self.declared_packages
            )

            # Combined formatted view for tool returns
            m = analysis["metrics"]
            out_lines = [
                f"exit code: {exit_code if exit_code is not None else 'TIMED_OUT'} ({analysis['outcome']})",
                f"metrics: {m['collected']} collected, {m['passed']} passed, {m['failed']} failed, {m['skipped']} skipped [{duration_ms} ms]",
            ]
            if stdout.strip():
                out_lines.append(f"--- stdout ---\n{stdout.strip()}")
            if stderr.strip():
                out_lines.append(f"--- stderr ---\n{stderr.strip()}")
            if analysis.get("error_message"):
                out_lines.append(f"--- outcome ---\n{analysis['error_message']}")

            formatted_output = self._clip("\n\n".join(out_lines))

            record = {
                "command": command,
                "exit_code": exit_code,
                "duration_ms": duration_ms,
                "stdout": stdout,
                "stderr": stderr,
                "output": formatted_output,
                "passed": analysis["passed"],
                "outcome": analysis["outcome"],
                "metrics": analysis["metrics"],
                "error_type": analysis.get("error_type"),
                "error_message": analysis.get("error_message"),
                "timed_out": timed_out,
            }
            self.test_results.append(record)

            # Preserve test artifact inside the run repo
            try:
                (self.workspace / ".test_last_result.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
            except Exception:
                pass

            return record
        except FileNotFoundError as exc:
            duration_ms = round((time.perf_counter() - started) * 1000)
            err_msg = f"Command unavailable ({exc.filename}). Install the repository's test runtime."
            record = {
                "command": command,
                "exit_code": None,
                "duration_ms": duration_ms,
                "stdout": "",
                "stderr": err_msg,
                "output": err_msg,
                "passed": False,
                "outcome": "BLOCKED",
                "metrics": {"collected": 0, "passed": 0, "failed": 0, "skipped": 0, "errors": 1},
                "error_type": "missing_runtime",
                "error_message": err_msg,
                "timed_out": False,
            }
            self.test_results.append(record)
            return record

    async def _run_command(self, command: list[str], timeout: int) -> str:
        self._emit("tool_started", "Running command: " + " ".join(command), {"command": command})
        proc = None
        try:
            safe_env = self._build_env()
            exec_cmd = self._resolve_executable(command)
            proc = await asyncio.create_subprocess_exec(
                *exec_cmd, cwd=self.workspace,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=safe_env,
                start_new_session=(sys.platform != "win32"),
            )
            try: stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                _kill_process_tree(proc); stdout, _ = await proc.communicate()
                output = stdout.decode(errors="replace") + f"\nCommand timed out after {timeout}s."
                return self._clip(output)
            except asyncio.CancelledError:
                _kill_process_tree(proc)
                await proc.communicate()
                raise
            output = stdout.decode(errors="replace")
            return self._clip(f"exit code: {proc.returncode}\n{output}")
        except FileNotFoundError as exc:
            return f"Command unavailable ({exc.filename}). Install the repository's test runtime."

    def _emit(self, event_type, message, data):
        if self.event_callback: self.event_callback(event_type, message, data)


def detect_repository_tests(workspace: Path) -> dict[str, Any]:
    """Examine repository workspace to discover testing framework, files, dirs, and commands."""
    runtime = ToolRuntime(workspace, Settings(), "auto")
    return runtime.detect_repository_tests()
