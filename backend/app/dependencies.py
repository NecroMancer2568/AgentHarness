"""Repository-aware dependency manifest inspection, virtual environment isolation, and caching."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .config import Settings

# Standard mapping of Python import module names to PyPI distribution package names
IMPORT_TO_DISTRIBUTION = {
    "sklearn": "scikit-learn",
    "yaml": "pyyaml",
    "bs4": "beautifulsoup4",
    "pil": "pillow",
    "cv2": "opencv-python",
    "dateutil": "python-dateutil",
    "dotenv": "python-dotenv",
    "jose": "python-jose",
    "multipart": "python-multipart",
    "serial": "pyserial",
    "magic": "python-magic",
}

DEPENDENCY_STATES = {
    "DEPENDENCIES_READY",
    "DEPENDENCY_INSTALL_FAILED",
    "MISSING_DEPENDENCY",
    "DEPENDENCY_INSTALL_BLOCKED",
    "TESTS_PASSED",
    "TESTS_FAILED",
}


def sanitize_credentials(text: str) -> str:
    """Mask credentials in package manager logs and URLs."""
    if not text:
        return ""
    # Mask http(s)://user:password@host -> http(s)://***:***@host
    sanitized = re.sub(r'(https?://)[^:\s/]+:[^@\s/]+@', r'\1***:***@', text)
    sanitized = re.sub(r'([a-zA-Z0-9_\-\+\.]+://)[^:\s/]+:[^@\s/]+@', r'\1***:***@', sanitized)
    return sanitized


def normalize_package_name(name: str) -> str:
    """Normalize a package name according to PEP 503 (lowercase, dashes instead of underscores/dots)."""
    clean = re.sub(r"[-_.]+", "-", name.strip()).lower()
    return clean


def parse_requirements_txt(content: str) -> list[str]:
    """Extract package names from a requirements.txt file."""
    packages = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Ignore pip options
        if line.startswith(("-i", "--index-url", "--extra-index-url", "-f", "--find-links", "-r", "-e")):
            continue
        # Extract package name before any version specifier, environment marker, or extras
        match = re.match(r"^([a-zA-Z0-9_\-\.]+)", line)
        if match:
            packages.append(normalize_package_name(match.group(1)))
    return packages


def parse_pyproject_toml(content: str) -> list[str]:
    """Extract declared dependencies from a pyproject.toml file."""
    packages: list[str] = []
    # 1. Try standard tomllib
    try:
        import tomllib
        data = tomllib.loads(content)
        project = data.get("project") or {}
        for dep in project.get("dependencies", []):
            m = re.match(r"^([a-zA-Z0-9_\-\.]+)", str(dep).strip())
            if m:
                packages.append(normalize_package_name(m.group(1)))
        for group in (project.get("optional-dependencies") or {}).values():
            for dep in group:
                m = re.match(r"^([a-zA-Z0-9_\-\.]+)", str(dep).strip())
                if m:
                    packages.append(normalize_package_name(m.group(1)))
        poetry_deps = ((data.get("tool") or {}).get("poetry") or {}).get("dependencies") or {}
        for name in poetry_deps.keys():
            if name.lower() not in {"python"}:
                packages.append(normalize_package_name(name))
        if packages:
            return sorted(list(dict.fromkeys(packages)))
    except Exception:
        pass

    # 2. Regex fallback for partial or malformed TOML
    matches = re.findall(r'["\']([a-zA-Z0-9_\-\.]+)(?:\[[^\]]*\])?(?:[><=~!\s][^"\']*)?["\']', content)
    for m in matches:
        if m.lower() not in {"project", "tool", "dependencies", "version", "name", "description", "readme"}:
            packages.append(normalize_package_name(m))
    return sorted(list(dict.fromkeys(packages)))


def parse_setup_py(content: str) -> list[str]:
    """Extract packages listed in install_requires in setup.py."""
    packages = []
    matches = re.findall(r'["\']([a-zA-Z0-9_\-\.]+)(?:[><=~!\s][^"\']*)?["\']', content)
    for m in matches:
        if m.lower() not in {"setup.py", "utf-8", "text/markdown", "setuptools"}:
            packages.append(normalize_package_name(m))
    return packages


def inspect_repository_dependencies(workspace: Path) -> dict[str, Any]:
    """Examine repository dependency manifests across multiple languages and compute a content hash."""
    manifests: list[dict[str, Any]] = []
    declared_packages: set[str] = set()
    language = "unknown"

    # 1. Python manifests
    req_files = list(workspace.glob("requirements*.txt")) + list(workspace.glob("requirements/*.txt"))
    for rf in sorted(req_files):
        if rf.is_file():
            text = rf.read_text(encoding="utf-8", errors="replace")
            pkgs = parse_requirements_txt(text)
            declared_packages.update(pkgs)
            h = hashlib.sha256(text.encode("utf-8")).hexdigest()
            manifests.append({"type": "requirements.txt", "path": str(rf.relative_to(workspace)), "hash": h, "packages": pkgs})
            language = "python"

    pyproject = workspace / "pyproject.toml"
    if pyproject.is_file():
        text = pyproject.read_text(encoding="utf-8", errors="replace")
        pkgs = parse_pyproject_toml(text)
        declared_packages.update(pkgs)
        h = hashlib.sha256(text.encode("utf-8")).hexdigest()
        manifests.append({"type": "pyproject.toml", "path": "pyproject.toml", "hash": h, "packages": pkgs})
        language = "python"

    setup_py = workspace / "setup.py"
    if setup_py.is_file():
        text = setup_py.read_text(encoding="utf-8", errors="replace")
        pkgs = parse_setup_py(text)
        declared_packages.update(pkgs)
        h = hashlib.sha256(text.encode("utf-8")).hexdigest()
        manifests.append({"type": "setup.py", "path": "setup.py", "hash": h, "packages": pkgs})
        language = "python"

    pipfile = workspace / "Pipfile"
    if pipfile.is_file():
        text = pipfile.read_text(encoding="utf-8", errors="replace")
        pkgs = parse_requirements_txt(text)
        declared_packages.update(pkgs)
        h = hashlib.sha256(text.encode("utf-8")).hexdigest()
        manifests.append({"type": "Pipfile", "path": "Pipfile", "hash": h, "packages": pkgs})
        language = "python"

    # 2. JavaScript / TypeScript
    pkg_json = workspace / "package.json"
    if pkg_json.is_file():
        try:
            data = json.loads(pkg_json.read_text(encoding="utf-8", errors="replace"))
            deps = list((data.get("dependencies") or {}).keys()) + list((data.get("devDependencies") or {}).keys())
            declared_packages.update(normalize_package_name(d) for d in deps)
            h = hashlib.sha256(pkg_json.read_bytes()).hexdigest()
            manifests.append({"type": "package.json", "path": "package.json", "hash": h, "packages": deps})
            if language == "unknown":
                language = "javascript/typescript"
        except Exception:
            pass

    # 3. Rust
    cargo_toml = workspace / "Cargo.toml"
    if cargo_toml.is_file():
        h = hashlib.sha256(cargo_toml.read_bytes()).hexdigest()
        manifests.append({"type": "Cargo.toml", "path": "Cargo.toml", "hash": h, "packages": []})
        if language == "unknown":
            language = "rust"

    # 4. Go
    go_mod = workspace / "go.mod"
    if go_mod.is_file():
        h = hashlib.sha256(go_mod.read_bytes()).hexdigest()
        manifests.append({"type": "go.mod", "path": "go.mod", "hash": h, "packages": []})
        if language == "unknown":
            language = "go"

    # 5. Java
    pom_xml = workspace / "pom.xml"
    if pom_xml.is_file():
        h = hashlib.sha256(pom_xml.read_bytes()).hexdigest()
        manifests.append({"type": "pom.xml", "path": "pom.xml", "hash": h, "packages": []})
        if language == "unknown":
            language = "java"

    # 6. C/C++
    cmake = workspace / "CMakeLists.txt"
    if cmake.is_file():
        h = hashlib.sha256(cmake.read_bytes()).hexdigest()
        manifests.append({"type": "CMakeLists.txt", "path": "CMakeLists.txt", "hash": h, "packages": []})
        if language == "unknown":
            language = "c_cpp"

    # Combined hash across all manifests
    combined_hasher = hashlib.sha256()
    for m in sorted(manifests, key=lambda x: x["path"]):
        combined_hasher.update(m["path"].encode("utf-8"))
        combined_hasher.update(m["hash"].encode("utf-8"))
    combined_hash = combined_hasher.hexdigest()

    return {
        "language": language,
        "manifests": manifests,
        "declared_packages": sorted(declared_packages),
        "combined_hash": combined_hash,
        "has_manifest": len(manifests) > 0,
    }


def is_declared_dependency(module_name: str, declared_packages: list[str]) -> bool:
    """Check if a missing import corresponds to any declared package or known distribution mapping."""
    norm = normalize_package_name(module_name)
    declared_set = {normalize_package_name(p) for p in declared_packages}
    if norm in declared_set:
        return True
    dist_name = IMPORT_TO_DISTRIBUTION.get(norm)
    if dist_name and normalize_package_name(dist_name) in declared_set:
        return True
    return False


async def prepare_environment(
    workspace: Path,
    settings: Settings,
    repo_name: str,
    event_callback=None,
) -> dict[str, Any]:
    """
    Inspect dependency manifests, provision a dedicated isolated virtual environment,
    install declared dependencies, and cache the environment safely using manifest hashes.
    """
    workspace = workspace.resolve()
    dep_info = inspect_repository_dependencies(workspace)
    language = dep_info["language"]
    manifests = dep_info["manifests"]
    declared_packages = dep_info["declared_packages"]
    manifest_hash = dep_info["combined_hash"] or "no-manifest"
    started = time.perf_counter()

    def emit(typ: str, msg: str, data: dict[str, Any] | None = None):
        if event_callback:
            event_callback(typ, msg, data or {})

    # Non-Python repositories
    if language != "python" and not list(workspace.glob("*.py")) and not (workspace / "requirements.txt").exists():
        if language == "javascript/typescript":
            pkg = workspace / "package.json"
            if pkg.is_file() and not (workspace / "node_modules").exists() and shutil.which("npm"):
                emit("dependency_preparation_started", "Installing JavaScript dependencies via npm", {"language": language})
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "npm", "install", "--no-audit", "--prefer-offline",
                        cwd=workspace, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                    )
                    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=settings.agentharness_dependency_timeout_seconds)
                    dur = round((time.perf_counter() - started) * 1000)
                    if proc.returncode == 0:
                        emit("dependencies_ready", "Node dependencies installed", {"duration_ms": dur})
                        return {"status": "DEPENDENCIES_READY", "language": language, "duration_ms": dur, "cached": False, "declared_packages": declared_packages, "exit_code": 0, "output": stdout.decode(errors="replace")}
                    else:
                        emit("dependency_install_failed", "npm install exited with non-zero code", {"exit_code": proc.returncode})
                        return {"status": "DEPENDENCY_INSTALL_FAILED", "language": language, "duration_ms": dur, "cached": False, "declared_packages": declared_packages, "exit_code": proc.returncode, "error": stderr.decode(errors="replace")}
                except asyncio.TimeoutError:
                    return {"status": "DEPENDENCY_INSTALL_BLOCKED", "language": language, "duration_ms": settings.agentharness_dependency_timeout_seconds * 1000, "error": "npm install timed out"}
        return {
            "status": "DEPENDENCIES_READY",
            "language": language,
            "duration_ms": round((time.perf_counter() - started) * 1000),
            "cached": True,
            "declared_packages": declared_packages,
            "exit_code": 0,
            "output": f"Repository framework ({language}) detected; standard runtime environment active.",
        }

    # Python virtual environment management
    settings.venvs_root.mkdir(parents=True, exist_ok=True)
    clean_repo_name = re.sub(r"[^a-zA-Z0-9_\-]+", "_", repo_name)
    cache_key = f"{clean_repo_name}-{manifest_hash[:16]}"
    venv_dir = (settings.venvs_root / cache_key).resolve()
    ready_marker = venv_dir / ".agentharness_ready"
    python_bin = venv_dir / "bin" / "python"
    pytest_bin = venv_dir / "bin" / "pytest"

    # Link run workspace .venv to the dedicated venv
    workspace_venv_link = workspace / ".venv"

    # Check cache validity
    if ready_marker.is_file() and python_bin.is_file() and pytest_bin.is_file():
        try:
            marker_data = json.loads(ready_marker.read_text(encoding="utf-8"))
            if marker_data.get("hash") == manifest_hash:
                dur = round((time.perf_counter() - started) * 1000)
                if not workspace_venv_link.exists():
                    try: workspace_venv_link.symlink_to(venv_dir, target_is_directory=True)
                    except OSError: pass
                emit("dependencies_ready", "Using cached Python virtual environment", {"venv": str(venv_dir), "duration_ms": dur})
                return {
                    "status": "DEPENDENCIES_READY",
                    "language": "python",
                    "cached": True,
                    "duration_ms": dur,
                    "manifest_hash": manifest_hash,
                    "declared_packages": declared_packages,
                    "venv_path": venv_dir,
                    "python_bin": python_bin,
                    "pytest_bin": pytest_bin,
                    "exit_code": 0,
                    "output": "Reused cached environment matching dependency manifest hash.",
                }
        except Exception:
            pass

    emit("dependency_preparation_started", f"Provisioning isolated virtual environment for {repo_name}", {"cache_dir": str(venv_dir)})

    # Create fresh virtual environment in cache directory
    if venv_dir.exists():
        shutil.rmtree(venv_dir, ignore_errors=True)
    venv_dir.mkdir(parents=True, exist_ok=True)

    # 1. Create venv
    try:
        proc_venv = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "venv", str(venv_dir),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _, stderr_v = await asyncio.wait_for(proc_venv.communicate(), timeout=60)
        if proc_venv.returncode != 0:
            emit("dependency_install_failed", "Failed to create Python virtual environment", {"error": stderr_v.decode()})
            return {
                "status": "DEPENDENCY_INSTALL_FAILED",
                "language": "python",
                "duration_ms": round((time.perf_counter() - started) * 1000),
                "error": f"venv creation failed: {stderr_v.decode(errors='replace')}",
                "declared_packages": declared_packages,
            }
    except Exception as exc:
        return {
            "status": "DEPENDENCY_INSTALL_BLOCKED",
            "language": "python",
            "duration_ms": round((time.perf_counter() - started) * 1000),
            "error": f"venv creation blocked: {exc}",
            "declared_packages": declared_packages,
        }

    pip_bin = venv_dir / "bin" / "pip"
    combined_log: list[str] = []

    # 2. Ensure pytest is installed in the isolated virtual environment
    try:
        cmd_pytest = [str(pip_bin), "install", "--prefer-binary", "pytest"]
        proc_pt = await asyncio.create_subprocess_exec(*cmd_pytest, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        so, se = await asyncio.wait_for(proc_pt.communicate(), timeout=settings.agentharness_dependency_timeout_seconds)
        combined_log.append(sanitize_credentials(so.decode(errors="replace")))
        if se:
            combined_log.append(sanitize_credentials(se.decode(errors="replace")))
    except asyncio.TimeoutError:
        return {
            "status": "DEPENDENCY_INSTALL_BLOCKED",
            "language": "python",
            "duration_ms": round((time.perf_counter() - started) * 1000),
            "error": "pytest installation timed out",
            "declared_packages": declared_packages,
        }

    # 3. Install declared dependencies
    install_commands: list[list[str]] = []
    # If requirements.txt present
    for m in manifests:
        if m["type"] == "requirements.txt":
            req_path = (workspace / m["path"]).resolve()
            if req_path.is_file():
                install_commands.append([str(pip_bin), "install", "--prefer-binary", "-r", str(req_path)])
        elif m["type"] in {"pyproject.toml", "setup.py"}:
            install_commands.append([str(pip_bin), "install", "--prefer-binary", str(workspace)])

    for cmd in install_commands:
        cmd_str = " ".join(cmd)
        emit("dependency_install_step", f"Running: {sanitize_credentials(cmd_str)}", {"command": cmd})
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=workspace, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            so, se = await asyncio.wait_for(proc.communicate(), timeout=settings.agentharness_dependency_timeout_seconds)
            so_clean = sanitize_credentials(so.decode(errors="replace"))
            se_clean = sanitize_credentials(se.decode(errors="replace"))
            combined_log.append(so_clean)
            if se_clean:
                combined_log.append(se_clean)

            if proc.returncode != 0:
                full_log = "\n".join(combined_log)
                dur = round((time.perf_counter() - started) * 1000)
                # Check if failure is due to blocked network / authentication / unavailable package
                is_blocked = (
                    "Could not find a version that satisfies the requirement" in full_log
                    or "401" in full_log
                    or "403" in full_log
                    or "ProxyError" in full_log
                    or "Connection" in full_log
                )
                state = "DEPENDENCY_INSTALL_BLOCKED" if is_blocked else "DEPENDENCY_INSTALL_FAILED"
                emit("dependency_install_failed", f"Dependency installation failed with code {proc.returncode}", {"state": state, "exit_code": proc.returncode})
                return {
                    "status": state,
                    "language": "python",
                    "duration_ms": dur,
                    "cached": False,
                    "manifest_hash": manifest_hash,
                    "declared_packages": declared_packages,
                    "venv_path": venv_dir,
                    "exit_code": proc.returncode,
                    "error": se_clean[:1000] or so_clean[:1000],
                    "output": full_log[:5000],
                }
        except asyncio.TimeoutError:
            dur = round((time.perf_counter() - started) * 1000)
            emit("dependency_install_failed", f"Dependency installation timed out after {settings.agentharness_dependency_timeout_seconds}s", {})
            return {
                "status": "DEPENDENCY_INSTALL_BLOCKED",
                "language": "python",
                "duration_ms": dur,
                "cached": False,
                "manifest_hash": manifest_hash,
                "declared_packages": declared_packages,
                "error": f"Dependency installation timed out after {settings.agentharness_dependency_timeout_seconds}s",
                "output": "\n".join(combined_log)[:5000],
            }

    # Mark as ready
    dur = round((time.perf_counter() - started) * 1000)
    ready_marker.write_text(json.dumps({
        "hash": manifest_hash,
        "repo": repo_name,
        "created_at": time.time(),
        "declared_packages": declared_packages,
    }, indent=2), encoding="utf-8")

    if not workspace_venv_link.exists():
        try: workspace_venv_link.symlink_to(venv_dir, target_is_directory=True)
        except OSError: pass

    emit("dependencies_ready", f"Dependencies installed successfully into isolated venv ({dur} ms)", {"duration_ms": dur, "declared_packages": len(declared_packages)})

    return {
        "status": "DEPENDENCIES_READY",
        "language": "python",
        "cached": False,
        "duration_ms": dur,
        "manifest_hash": manifest_hash,
        "declared_packages": declared_packages,
        "venv_path": venv_dir,
        "python_bin": python_bin,
        "pytest_bin": pytest_bin,
        "exit_code": 0,
        "output": "\n".join(combined_log)[:5000],
    }
