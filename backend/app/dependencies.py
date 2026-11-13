"""Repository-aware dependency manifest inspection, virtual environment isolation, and caching."""
from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .config import Settings

try:
    from packaging.specifiers import SpecifierSet
    from packaging.version import Version
except ImportError:
    SpecifierSet = None
    Version = None

# Standard mapping of Python import module names to PyPI distribution package names
IMPORT_TO_DISTRIBUTION = {
    "sklearn": "scikit-learn",
    "yaml": "pyyaml",
    "bs4": "beautifulsoup4",
    "pil": "pillow",
    "PIL": "pillow",
    "cv2": "opencv-python",
    "dateutil": "python-dateutil",
    "dotenv": "python-dotenv",
    "jose": "python-jose",
    "multipart": "python-multipart",
    "serial": "pyserial",
    "magic": "python-magic",
    "torch": "torch",
    "transformers": "transformers",
    "jwt": "pyjwt",
    "git": "gitpython",
    "bio": "biopython",
    "psycopg2": "psycopg2-binary",
    "socks": "pysocks",
    "docx": "python-docx",
    "pptx": "python-pptx",
    "fitz": "pymupdf",
    "slugify": "python-slugify",
    "levenshtein": "python-levenshtein",
    "dns": "dnspython",
    "attr": "attrs",
    "pkg_resources": "setuptools",
    "googleapiclient": "google-api-python-client",
    "playwright": "playwright",
}

# Reverse mapping: distribution name to primary import module name
DISTRIBUTION_TO_IMPORT = {
    "scikit-learn": "sklearn",
    "pyyaml": "yaml",
    "beautifulsoup4": "bs4",
    "pillow": "PIL",
    "opencv-python": "cv2",
    "python-dateutil": "dateutil",
    "python-dotenv": "dotenv",
    "python-jose": "jose",
    "python-multipart": "multipart",
    "pyserial": "serial",
    "python-magic": "magic",
    "pyjwt": "jwt",
    "gitpython": "git",
    "biopython": "Bio",
    "psycopg2-binary": "psycopg2",
    "pysocks": "socks",
    "python-docx": "docx",
    "python-pptx": "pptx",
    "pymupdf": "fitz",
    "python-slugify": "slugify",
    "python-levenshtein": "Levenshtein",
    "dnspython": "dns",
    "attrs": "attr",
    "setuptools": "pkg_resources",
    "google-api-python-client": "googleapiclient",
    "playwright": "playwright",
}

def get_standard_library_modules() -> set[str]:
    """Return Python standard library module names across Python versions."""
    stdlib = set(getattr(sys, "stdlib_module_names", set()))
    common_stdlib = {
        "abc", "aifc", "antigravity", "argparse", "array", "ast", "asynchat", "asyncio",
        "asyncore", "atexit", "audioop", "base64", "bdb", "binascii", "binhex", "bisect",
        "builtins", "bz2", "cProfile", "calendar", "cgi", "cgitb", "chunk", "cmath", "cmd",
        "code", "codecs", "codeop", "collections", "colorsys", "compileall", "concurrent",
        "configparser", "contextlib", "contextvars", "copy", "copyreg", "crypt", "csv",
        "ctypes", "curses", "dataclasses", "datetime", "dbm", "decimal", "difflib", "dis",
        "distutils", "doctest", "email", "encodings", "ensurepip", "enum", "errno", "faulthandler",
        "fcntl", "filecmp", "fileinput", "fnmatch", "fractions", "ftplib", "functools",
        "gc", "getopt", "getpass", "gettext", "glob", "graphlib", "grp", "gzip", "hashlib",
        "heapq", "hmac", "html", "http", "idlelib", "imaplib", "imghdr", "imp", "importlib",
        "inspect", "io", "ipaddress", "itertools", "json", "keyword", "lib2to3", "linecache",
        "locale", "logging", "lzma", "mailbox", "mailcap", "marshal", "math", "mimetypes",
        "mmap", "modulefinder", "msilib", "msvcrt", "multiprocessing", "netrc", "nis",
        "nntplib", "numbers", "operator", "optparse", "os", "ossaudiodev", "pathlib", "pdb",
        "pickle", "pickletools", "pipes", "pkgutil", "platform", "plistlib", "poplib", "posix",
        "posixpath", "pprint", "profile", "pstats", "pty", "pwd", "py_compile", "pyclbr",
        "pydoc", "queue", "quopri", "random", "re", "readline", "reprlib", "resource",
        "rlcompleter", "runpy", "sched", "secrets", "select", "selectors", "shelve", "shlex",
        "shutil", "signal", "site", "smtpd", "smtplib", "sndhdr", "socket", "socketserver",
        "spwd", "sqlite3", "sre_compile", "sre_constants", "sre_parse", "ssl", "stat",
        "statistics", "string", "stringprep", "struct", "subprocess", "sunau", "symbol",
        "symtable", "sys", "sysconfig", "syslog", "tabnanny", "tarfile", "telnetlib", "tempfile",
        "termios", "test", "textwrap", "this", "threading", "time", "timeit", "tkinter",
        "token", "tokenize", "tomllib", "trace", "traceback", "tracemalloc", "tty", "turtle",
        "turtledemo", "types", "typing", "unicodedata", "unittest", "urllib", "uu", "uuid",
        "venv", "warnings", "wave", "weakref", "webbrowser", "winreg", "winsound", "wsgiref",
        "xdrlib", "xml", "xmlrpc", "zipapp", "zipfile", "zipimport", "zlib", "zoneinfo",
        "__future__",
    }
    return stdlib.union(common_stdlib)

def get_workspace_local_modules(workspace: Path) -> set[str]:
    """Identify modules and packages located directly within the workspace."""
    local_mods: set[str] = set()
    ignored = {".git", ".venv", "venv", "venvs", "pip_cache", "site-packages", "__pycache__", "build", "dist", "node_modules"}

    # Top-level python files (e.g. main.py -> main)
    for p in workspace.iterdir():
        if p.name in ignored or p.name.startswith("."):
            continue
        if p.is_file() and p.suffix == ".py":
            local_mods.add(normalize_package_name(p.stem))
        elif p.is_dir():
            if (p / "__init__.py").exists() or any(p.glob("*.py")):
                local_mods.add(normalize_package_name(p.name))

    # Also inspect src/, app/, lib/ roots if they exist
    for root_dir in ("src", "app", "lib"):
        sub = workspace / root_dir
        if sub.is_dir():
            for p in sub.iterdir():
                if p.name in ignored or p.name.startswith("."):
                    continue
                if p.is_file() and p.suffix == ".py":
                    local_mods.add(normalize_package_name(p.stem))
                elif p.is_dir() and ((sub / p.name / "__init__.py").exists() or any(p.glob("*.py"))):
                    local_mods.add(normalize_package_name(p.name))

    return local_mods

KNOWN_THIRD_PARTY_PACKAGES = {
    "requests", "urllib3", "httpx", "aiohttp", "flask", "django", "fastapi", "uvicorn", "starlette",
    "pydantic", "pydantic-core", "pydantic-settings", "sqlalchemy", "alembic", "celery", "redis",
    "numpy", "pandas", "scipy", "scikit-learn", "sklearn", "matplotlib", "seaborn", "plotly",
    "torch", "torchvision", "torchaudio", "transformers", "accelerate", "datasets", "huggingface-hub",
    "pytest", "pytest-cov", "pytest-asyncio", "pytest-mock", "mock", "unittest-mock",
    "click", "typer", "rich", "tqdm", "colorama", "pyyaml", "yaml", "tomli", "tomllib", "toml",
    "pillow", "pil", "opencv-python", "cv2", "beautifulsoup4", "bs4", "lxml", "html5lib",
    "boto3", "botocore", "google-api-python-client", "google-cloud-storage", "azure-storage-blob",
    "joblib", "yfinance", "networkx", "sympy", "statsmodels", "cryptography", "pyjwt", "jwt",
    "psycopg2", "psycopg2-binary", "pymongo", "motor", "elastic-search", "elasticsearch",
    "weaviate-client", "chromadb", "qdrant-client", "openai", "anthropic", "langchain",
    "tenacity", "backoff", "attrs", "attr", "marshmallow", "cerberus", "jsonschema",
    "paramiko", "fabric", "ansible", "docker", "kubernetes",
}

def scan_docker_and_readme_hints(workspace: Path) -> dict[str, Any]:
    """Scan Dockerfiles, README, setup scripts for hints of declared dependencies."""
    hints: set[str] = set()
    sources: list[str] = []

    pip_install_re = re.compile(r"(?:pip|pip3)\s+install\s+([^\n\r;&`]+)")
    poetry_add_re = re.compile(r"poetry\s+add\s+([^\n\r;&`]+)")

    files_to_check: list[Path] = []
    files_to_check.extend(workspace.glob("Dockerfile*"))
    files_to_check.extend(workspace.glob("*.dockerfile"))
    files_to_check.extend(workspace.glob("README*"))
    files_to_check.extend(workspace.glob("*.sh"))
    files_to_check.extend(workspace.glob("scripts/*.sh"))

    for f in files_to_check:
        if not f.is_file() or any(p.startswith(".") for p in f.parts):
            continue
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
            found_any = False
            for m in pip_install_re.finditer(content):
                cmd_args = m.group(1).split()
                for arg in cmd_args:
                    arg = arg.strip(" \t\n\r`\"'").strip()
                    if arg.startswith("-") or not arg or "/" in arg or "\\" in arg:
                        continue
                    pkg = re.split(r"[=<>~!\s]", arg)[0].strip(" \t\n\r`\"'").strip()
                    if pkg and re.match(r"^[a-zA-Z0-9_\-]+$", pkg):
                        if pkg.lower() not in {"to", "start", "run", "the", "a", "an", "for", "in", "and", "or"}:
                            hints.add(normalize_package_name(pkg))
                            found_any = True
            for m in poetry_add_re.finditer(content):
                cmd_args = m.group(1).split()
                for arg in cmd_args:
                    arg = arg.strip(" \t\n\r`\"'").strip()
                    if arg.startswith("-") or not arg:
                        continue
                    pkg = re.split(r"[=<>~!\s]", arg)[0].strip(" \t\n\r`\"'").strip()
                    if pkg and re.match(r"^[a-zA-Z0-9_\-]+$", pkg):
                        if pkg.lower() not in {"to", "start", "run", "the", "a", "an", "for", "in", "and", "or"}:
                            hints.add(normalize_package_name(pkg))
                            found_any = True
            if found_any:
                sources.append(str(f.relative_to(workspace)))
        except Exception:
            continue

    return {"hints": hints, "sources": sources}

def parse_conda_environment_yml(text: str) -> list[str]:
    """Parse Conda environment.yml / environment.yaml dependencies."""
    packages: list[str] = []
    in_dependencies = False
    in_pip = False

    for line in text.splitlines():
        trimmed = line.strip()
        if not trimmed or trimmed.startswith("#"):
            continue

        if trimmed == "dependencies:":
            in_dependencies = True
            in_pip = False
            continue

        if in_dependencies:
            indent = len(line) - len(line.lstrip())
            if indent == 0 and not trimmed.startswith("-"):
                in_dependencies = False
                in_pip = False
                continue

            if "- pip:" in trimmed or trimmed == "pip:":
                in_pip = True
                continue

            if in_pip:
                if trimmed.startswith("-"):
                    raw = trimmed.lstrip("-").strip()
                    pkg = re.split(r"[=<>~!\s]", raw)[0].strip()
                    if pkg:
                        packages.append(normalize_package_name(pkg))
                elif not trimmed.startswith("-") and indent <= 2:
                    in_pip = False

            elif trimmed.startswith("-"):
                raw = trimmed.lstrip("-").strip()
                if raw == "pip:":
                    in_pip = True
                    continue
                pkg = re.split(r"[=<>~!\s]", raw)[0].strip()
                if pkg and pkg.lower() not in {"python", "pip"}:
                    packages.append(normalize_package_name(pkg))

    return packages

def infer_external_dependencies(workspace: Path) -> dict[str, Any]:
    """
    Intelligently infer third-party dependencies by AST scanning of all Python files,
    excluding standard library and local workspace modules, mapping import names to PyPI
    package distribution names, and incorporating hints from Dockerfiles/READMEs.
    """
    stdlib_modules = get_standard_library_modules()
    local_modules = get_workspace_local_modules(workspace)
    hints_data = scan_docker_and_readme_hints(workspace)
    doc_hints = hints_data["hints"]

    prod_imports: set[str] = set()
    test_imports: set[str] = set()
    ambiguous_imports: set[str] = set()

    ignored_dirs = {".git", ".venv", "venv", "venvs", "pip_cache", "site-packages", "__pycache__", "build", "dist", "node_modules"}

    for py_file in workspace.rglob("*.py"):
        try:
            rel_parts = py_file.relative_to(workspace).parts
        except ValueError:
            rel_parts = py_file.parts
        if any(part.startswith(".") or part in ignored_dirs for part in rel_parts):
            continue

        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, Exception):
            continue

        is_test = "test" in py_file.name.lower() or any("test" in p.lower() for p in rel_parts[:-1])
        target_set = test_imports if is_test else prod_imports

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root_mod = alias.name.split(".")[0].strip()
                    if root_mod:
                        target_set.add(root_mod)
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    root_mod = node.module.split(".")[0].strip()
                    if root_mod:
                        target_set.add(root_mod)

    def process_imports(mod_set: set[str]) -> tuple[set[str], set[str], set[str], set[str]]:
        ext_pkgs: set[str] = set()
        loc_pkgs: set[str] = set()
        std_pkgs: set[str] = set()
        amb_pkgs: set[str] = set()

        for mod in mod_set:
            norm = normalize_package_name(mod)
            if not re.match(r"^[a-zA-Z0-9_\-]+$", norm):
                amb_pkgs.add(mod)
                continue

            if norm in local_modules or mod in local_modules:
                loc_pkgs.add(mod)
            elif norm in stdlib_modules or mod in stdlib_modules:
                std_pkgs.add(mod)
            else:
                dist = IMPORT_TO_DISTRIBUTION.get(norm, IMPORT_TO_DISTRIBUTION.get(mod, norm))
                norm_dist = normalize_package_name(dist)
                ext_pkgs.add(norm_dist)
                if (
                    norm_dist not in KNOWN_THIRD_PARTY_PACKAGES
                    and norm not in KNOWN_THIRD_PARTY_PACKAGES
                    and norm not in IMPORT_TO_DISTRIBUTION
                    and mod not in IMPORT_TO_DISTRIBUTION
                    and norm not in doc_hints
                ):
                    amb_pkgs.add(mod)

        return ext_pkgs, loc_pkgs, std_pkgs, amb_pkgs

    prod_ext, prod_loc, prod_std, prod_amb = process_imports(prod_imports)
    test_ext, test_loc, test_std, test_amb = process_imports(test_imports)

    all_inferred = set(prod_ext).union(test_ext).union(doc_hints)
    all_inferred = {p for p in all_inferred if p not in local_modules and p not in stdlib_modules}

    return {
        "inferred_packages": sorted(list(all_inferred)),
        "production_packages": sorted(list(prod_ext)),
        "test_packages": sorted(list(test_ext)),
        "local_modules_detected": sorted(list(prod_loc.union(test_loc))),
        "standard_library_detected": sorted(list(prod_std.union(test_std))),
        "hints_from_docs": sorted(list(doc_hints)),
        "hints_sources": hints_data["sources"],
        "ambiguous_imports": sorted(list(prod_amb.union(test_amb))),
    }

DEPENDENCY_STATES = {
    "DEPENDENCIES_READY",
    "DEPENDENCY_INSTALL_FAILED",
    "MISSING_DEPENDENCY",
    "DEPENDENCY_INSTALL_BLOCKED",
    "DEPENDENCY_INSTALL_TIMEOUT",
    "DEPENDENCY_INSTALL_CANCELLED",
    "DEPENDENCY_INSTALL_OOM",
    "DEPENDENCY_INSTALL_NETWORK_ERROR",
    "DEPENDENCY_INSTALL_INVALID_ENVIRONMENT",
    "DEPENDENCY_CACHE_ERROR",
    "TESTS_PASSED",
    "TESTS_FAILED",
}


def get_system_resource_info() -> dict[str, Any]:
    """Retrieve host system information (CPU, memory, OS) for installation diagnostics."""
    info: dict[str, Any] = {
        "cpu_count": os.cpu_count() or 1,
        "platform": sys.platform,
        "architecture": platform.machine(),
    }
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        phys_pages = os.sysconf("SC_PHYS_PAGES")
        avail_pages = os.sysconf("SC_AVPHYS_PAGES")
        info["total_memory_mb"] = round((page_size * phys_pages) / (1024 * 1024))
        info["available_memory_mb"] = round((page_size * avail_pages) / (1024 * 1024))
    except (ValueError, AttributeError, OSError):
        pass
    return info


def detect_nvidia_gpu() -> dict[str, Any]:
    """
    Accurately detect whether a usable NVIDIA GPU device and runtime are present.
    Does not infer GPU presence solely from installed Python packages.
    """
    visible = os.environ.get("NVIDIA_VISIBLE_DEVICES", "").strip().lower()
    if visible in {"none", "void"}:
        return {
            "available": False,
            "reason": "NVIDIA_VISIBLE_DEVICES is set to none/void",
            "devices": [],
            "driver_version": None,
            "cuda_version": None,
        }

    smi_path = shutil.which("nvidia-smi")
    if not smi_path:
        return {
            "available": False,
            "reason": "nvidia-smi binary not found in PATH",
            "devices": [],
            "driver_version": None,
            "cuda_version": None,
        }

    try:
        res = subprocess.run(
            [smi_path, "--query-gpu=name,driver_version", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if res.returncode == 0 and res.stdout.strip():
            lines = [l.strip() for l in res.stdout.strip().splitlines() if l.strip()]
            devices = []
            driver_ver = None
            for line in lines:
                parts = [p.strip() for p in line.split(",")]
                devices.append(parts[0])
                if len(parts) > 1 and not driver_ver:
                    driver_ver = parts[1]
            if devices:
                cuda_ver = None
                try:
                    smi_gen = subprocess.run([smi_path], capture_output=True, text=True, timeout=2)
                    m = re.search(r"CUDA Version:\s*(\d+\.\d+)", smi_gen.stdout)
                    if m:
                        cuda_ver = m.group(1)
                except Exception:
                    pass

                return {
                    "available": True,
                    "reason": f"nvidia-smi detected {len(devices)} active GPU(s)",
                    "devices": devices,
                    "driver_version": driver_ver,
                    "cuda_version": cuda_ver,
                }
    except Exception as exc:
        return {
            "available": False,
            "reason": f"nvidia-smi execution error: {exc}",
            "devices": [],
            "driver_version": None,
            "cuda_version": None,
        }

    dev_nodes = ["/dev/nvidia0", "/dev/nvidiactl"]
    has_dev = any(Path(p).exists() for p in dev_nodes)
    if not has_dev:
        return {
            "available": False,
            "reason": "No NVIDIA GPU device nodes found in /dev",
            "devices": [],
            "driver_version": None,
            "cuda_version": None,
        }

    return {
        "available": False,
        "reason": "GPU device nodes present but nvidia-smi failed to communicate with driver",
        "devices": [],
        "driver_version": None,
        "cuda_version": None,
    }


def check_pytorch_cpu_wheel_support(python_version: str, platform_name: str, arch: str) -> dict[str, Any]:
    """
    Verify whether official CPU-only PyTorch wheels are available for the target Python, OS, and arch.
    Returns: {"supported": bool, "recommended_index": str | None, "warning": str | None}
    """
    plat = platform_name.lower()
    norm_arch = arch.lower()
    py_parts = [int(p) for p in re.findall(r"\d+", python_version)]
    py_maj, py_min = (py_parts[0], py_parts[1]) if len(py_parts) >= 2 else (3, 13)

    cpu_index = "https://download.pytorch.org/whl/cpu"

    # Python 3.13 requires PyTorch >= 2.6.0
    if py_maj == 3 and py_min >= 13:
        if plat.startswith("linux") and norm_arch in {"x86_64", "amd64", "aarch64", "arm64"}:
            return {
                "supported": True,
                "recommended_index": cpu_index,
                "warning": None,
                "min_torch_version": "2.6.0",
            }
        elif plat.startswith("darwin"):
            # macOS uses default PyPI CPU/MPS wheels
            return {
                "supported": True,
                "recommended_index": None,
                "warning": None,
                "min_torch_version": "2.6.0",
            }
        elif plat.startswith("win"):
            return {
                "supported": True,
                "recommended_index": cpu_index,
                "warning": None,
                "min_torch_version": "2.6.0",
            }

    # Python 3.8 - 3.12 has broad PyTorch wheel support
    if py_maj == 3 and 8 <= py_min <= 12:
        return {
            "supported": True,
            "recommended_index": cpu_index,
            "warning": None,
            "min_torch_version": "1.10.0",
        }

    return {
        "supported": False,
        "recommended_index": cpu_index,
        "warning": f"Python {python_version} on {platform_name}-{arch} may lack official pre-built PyTorch CPU wheels.",
        "min_torch_version": "2.6.0",
    }


def inspect_code_imports(workspace: Path) -> dict[str, set[str]]:
    """Scan Python source and test files to determine which modules are actually imported."""
    prod_imports: set[str] = set()
    test_imports: set[str] = set()

    for py_file in workspace.rglob("*.py"):
        try:
            rel_parts = py_file.relative_to(workspace).parts
        except ValueError:
            rel_parts = py_file.parts
        if any(part.startswith(".") or part in {"site-packages", "__pycache__", "venv", "venvs", "pip_cache"} for part in rel_parts):
            continue
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8", errors="replace"))
            is_test = "test" in py_file.name.lower() or any("test" in p.lower() for p in rel_parts[:-1])
            target_set = test_imports if is_test else prod_imports
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        target_set.add(normalize_package_name(alias.name.split(".")[0]))
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        target_set.add(normalize_package_name(node.module.split(".")[0]))
        except Exception:
            continue

    return {"production": prod_imports, "tests": test_imports}


def parse_pip_telemetry(log: str) -> dict[str, Any]:
    """
    Extract PyTorch wheel, sizes, transitive NVIDIA CUDA dependencies,
    and download telemetry from pip output.
    """
    torch_wheel = None
    torch_version = None
    torch_size_mb = None
    nvidia_deps: set[str] = set()
    speeds: list[str] = []

    # Regex patterns
    wheel_pat = re.compile(r"Downloading\s+([^\s/]+\.whl)\s*\(([\d\.]+)\s*([kKmMgG][bB])\)")
    speed_pat = re.compile(r"([\d\.]+\s*(?:kB/s|KB/s|MB/s|GB/s))")
    collected_pat = re.compile(r"Collecting\s+([a-zA-Z0-9_\-]+)")

    for line in log.splitlines():
        m_wheel = wheel_pat.search(line)
        if m_wheel:
            fname, size_val, unit = m_wheel.group(1), float(m_wheel.group(2)), m_wheel.group(3).upper()
            size_mb = size_val
            if unit.startswith("K"):
                size_mb = round(size_val / 1024, 2)
            elif unit.startswith("G"):
                size_mb = round(size_val * 1024, 2)

            if "torch-" in fname:
                torch_wheel = fname
                torch_size_mb = size_mb
                m_ver = re.search(r"torch-([0-9a-zA-Z\.\+\-_]+?)-cp", fname)
                if m_ver:
                    torch_version = m_ver.group(1)

            if any(k in fname.lower() for k in ("nvidia", "cuda", "cudnn", "cublas", "cusparse", "cufft", "curand", "cusolver", "nvtx", "triton")):
                nvidia_deps.add(fname.split("-")[0].replace("_", "-"))

        if any(k in line.lower() for k in ("nvidia", "cuda", "cudnn", "cublas", "cusparse", "cufft", "curand", "cusolver", "nvtx", "triton")):
            m_col = collected_pat.search(line)
            if m_col:
                pkg = m_col.group(1).lower()
                if any(k in pkg for k in ("nvidia", "cuda", "cudnn", "cublas", "cusparse", "cufft", "curand", "cusolver", "nvtx", "triton")):
                    nvidia_deps.add(pkg)

        m_speed = speed_pat.findall(line)
        for s in m_speed:
            speeds.append(s)

    return {
        "selected_pytorch_version": torch_version,
        "selected_wheel_filename": torch_wheel,
        "pytorch_wheel_size_mb": torch_size_mb,
        "transitive_nvidia_dependencies": sorted(list(nvidia_deps)),
        "recorded_download_speeds": speeds[-5:] if speeds else [],
    }


class AsyncFileLock:
    """Non-blocking file lock with async sleep polling to prevent concurrent virtualenv collisions."""
    def __init__(self, lock_path: Path, timeout: float = 120.0):
        self.lock_path = lock_path
        self.timeout = timeout
        self.fd = None

    async def __aenter__(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = open(self.lock_path, "a")
        start = time.perf_counter()
        while True:
            try:
                import fcntl
                fcntl.flock(self.fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except (BlockingIOError, IOError):
                if (time.perf_counter() - start) > self.timeout:
                    raise TimeoutError(f"Timed out acquiring lock on {self.lock_path}")
                await asyncio.sleep(0.5)
            except ImportError:
                # Platforms without fcntl (Windows)
                return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.fd:
            try:
                import fcntl
                fcntl.flock(self.fd.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                self.fd.close()
            except Exception:
                pass


async def verify_environment_imports(
    python_bin: Path,
    declared_packages: list[str],
    cwd: Path,
    timeout: float = 20.0,
) -> tuple[bool, str | None, str | None]:
    """
    Verify that declared Python packages can actually be imported in the isolated venv.
    Returns: (success, failed_package, error_message)
    """
    excluded = {"pytest", "pytest-cov", "pytest-xdist", "pip", "setuptools", "wheel"}
    modules_to_test: list[tuple[str, str]] = []
    for pkg in declared_packages:
        norm = normalize_package_name(pkg)
        if norm in excluded:
            continue
        mod_name = DISTRIBUTION_TO_IMPORT.get(norm, norm.replace("-", "_"))
        modules_to_test.append((pkg, mod_name))

    if not modules_to_test:
        return True, None, None

    script_parts = ["import sys"]
    for pkg, mod in modules_to_test:
        script_parts.append(
            f"try:\n"
            f"    __import__({repr(mod)})\n"
            f"except Exception as exc:\n"
            f"    print({repr(f'IMPORT_FAIL:{pkg}:{mod}:')} + str(exc))\n"
            f"    sys.exit(1)"
        )
    script_parts.append("print('IMPORT_SUCCESS')")
    test_code = "\n".join(script_parts)

    rc, stdout, stderr, timed_out = await run_process_safely(
        [str(python_bin), "-c", test_code],
        cwd=cwd,
        timeout=timeout,
    )
    if timed_out:
        return False, "timeout", f"Import verification timed out after {timeout}s"
    if rc != 0:
        for line in stdout.splitlines():
            if line.startswith("IMPORT_FAIL:"):
                parts = line.split(":", 3)
                failed_pkg = parts[1] if len(parts) > 1 else "unknown"
                err_detail = parts[3] if len(parts) > 3 else line
                return False, failed_pkg, f"Failed to import package '{failed_pkg}': {err_detail}"
        return False, "unknown", stderr.strip() or stdout.strip() or "Import verification check failed"

    return True, None, None


def classify_dependency_error(
    exit_code: int,
    timed_out: bool,
    combined_log: str,
    timeout_budget: float,
    dur: int,
) -> tuple[str, str, str]:
    """
    Classify dependency preparation errors into granular states, diagnostic reasons, and actionable suggestions.
    Returns: (status, reason, actionable_suggestion)
    """
    log_lower = combined_log.lower()

    if timed_out:
        return (
            "DEPENDENCY_INSTALL_TIMEOUT",
            "TIMEOUT",
            (
                f"Dependency installation exceeded {timeout_budget} seconds (took {dur} ms). "
                "Consider increasing AGENTHARNESS_DEPENDENCY_TIMEOUT_SECONDS, or ensure pre-built wheels are cached."
            ),
        )

    if exit_code in (137, -9) or "out of memory" in log_lower or "oom" in log_lower or "killed" in log_lower:
        return (
            "DEPENDENCY_INSTALL_OOM",
            "OUT_OF_MEMORY",
            (
                f"Process was terminated by the OS (exit code {exit_code}, likely Out Of Memory / SIGKILL). "
                "Increase container memory allocation or host Docker memory limits."
            ),
        )

    network_indicators = [
        "could not find a version that satisfies the requirement",
        "no matching distribution found",
        "proxyerror",
        "sslerror",
        "certificate verify failed",
        "temporary failure in name resolution",
        "network is unreachable",
        "failed to establish a new connection",
        "connection refused",
        "connection reset",
        "403 forbidden",
        "401 unauthorized",
        "502 bad gateway",
        "503 service unavailable",
        "504 gateway timeout",
    ]
    if any(ind in log_lower for ind in network_indicators):
        return (
            "DEPENDENCY_INSTALL_NETWORK_ERROR",
            "NETWORK_ERROR",
            (
                "Package download failed due to network connectivity or unreachable package repository. "
                "Verify internet access, firewall/proxy settings, or configure AGENTHARNESS_PIP_INDEX_URL."
            ),
        )

    if "invalid environment" in log_lower or "bad interpreter" in log_lower or ("no such file or directory" in log_lower and "python" in log_lower):
        return (
            "DEPENDENCY_INSTALL_INVALID_ENVIRONMENT",
            "INVALID_ENVIRONMENT",
            "The selected Python interpreter or environment is corrupted or incompatible with the host architecture.",
        )

    return (
        "DEPENDENCY_INSTALL_FAILED",
        "INSTALL_FAILED",
        "Dependency installation failed. Review pip output for compilation errors, conflicting version requirements, or missing system C libraries.",
    )


def sanitize_credentials(text: str) -> str:
    """Mask credentials in package manager logs and URLs."""
    if not text:
        return ""
    sanitized = re.sub(r'(https?://)[^:\s/]+:[^@\s/]+@', r'\1***:***@', text)
    sanitized = re.sub(r'([a-zA-Z0-9_\-\+\.]+://)[^:\s/]+:[^@\s/]+@', r'\1***:***@', sanitized)
    return sanitized


def normalize_package_name(name: str) -> str:
    """Normalize a package name according to PEP 503 (lowercase, dashes instead of underscores/dots)."""
    clean = re.sub(r"[-_.]+", "-", name.strip()).lower()
    return clean


def parse_requirements_txt(content: str) -> list[str]:
    """Extract package names from a requirements.txt file."""
    packages: list[str] = []
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
    return sorted(list(dict.fromkeys(packages)))


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
    packages: list[str] = []
    matches = re.findall(r'["\']([a-zA-Z0-9_\-\.]+)(?:[><=~!\s][^"\']*)?["\']', content)
    for m in matches:
        if m.lower() not in {"setup.py", "utf-8", "text/markdown", "setuptools"}:
            packages.append(normalize_package_name(m))
    return sorted(list(dict.fromkeys(packages)))


def parse_poetry_lock(content: str) -> list[str]:
    """Extract package names from a poetry.lock file."""
    packages: list[str] = []
    try:
        import tomllib
        data = tomllib.loads(content)
        for pkg in data.get("package", []):
            name = pkg.get("name")
            if name and name.lower() != "python":
                packages.append(normalize_package_name(name))
        if packages:
            return sorted(list(dict.fromkeys(packages)))
    except Exception:
        pass

    matches = re.findall(r'name\s*=\s*["\']([a-zA-Z0-9_\-\.]+)["\']', content)
    for m in matches:
        if m.lower() != "python":
            packages.append(normalize_package_name(m))
    return sorted(list(dict.fromkeys(packages)))


def parse_uv_lock(content: str) -> list[str]:
    """Extract package names from a uv.lock file."""
    packages: list[str] = []
    try:
        import tomllib
        data = tomllib.loads(content)
        for pkg in data.get("package", []):
            name = pkg.get("name")
            if name and name.lower() != "python":
                packages.append(normalize_package_name(name))
        if packages:
            return sorted(list(dict.fromkeys(packages)))
    except Exception:
        pass

    matches = re.findall(r'name\s*=\s*["\']([a-zA-Z0-9_\-\.]+)["\']', content)
    for m in matches:
        if m.lower() != "python":
            packages.append(normalize_package_name(m))
    return sorted(list(dict.fromkeys(packages)))


def detect_python_version_requirement(workspace: Path) -> dict[str, Any] | None:
    """Detect required Python version specification from repository metadata."""
    # 1. .python-version
    pv_file = workspace / ".python-version"
    if pv_file.is_file():
        text = pv_file.read_text(encoding="utf-8", errors="replace").strip()
        if text:
            return {"source": ".python-version", "specifier": text}

    # 2. runtime.txt
    rt_file = workspace / "runtime.txt"
    if rt_file.is_file():
        text = rt_file.read_text(encoding="utf-8", errors="replace").strip()
        m = re.search(r"python-?(\d+\.\d+(?:\.\d+)?)", text, re.IGNORECASE)
        spec = m.group(1) if m else text
        if spec:
            return {"source": "runtime.txt", "specifier": spec}

    # 3. pyproject.toml
    pyproject = workspace / "pyproject.toml"
    if pyproject.is_file():
        try:
            import tomllib
            data = tomllib.loads(pyproject.read_text(encoding="utf-8", errors="replace"))
            rp = (data.get("project") or {}).get("requires-python")
            if rp:
                return {"source": "pyproject.toml (project.requires-python)", "specifier": str(rp).strip()}
            pp = ((data.get("tool") or {}).get("poetry") or {}).get("dependencies", {}).get("python")
            if pp:
                return {"source": "pyproject.toml (poetry.dependencies.python)", "specifier": str(pp).strip()}
        except Exception:
            pass

    # 4. Pipfile
    pipfile = workspace / "Pipfile"
    if pipfile.is_file():
        try:
            import tomllib
            data = tomllib.loads(pipfile.read_text(encoding="utf-8", errors="replace"))
            req = data.get("requires") or {}
            pv = req.get("python_version") or req.get("python_full_version")
            if pv:
                return {"source": "Pipfile", "specifier": str(pv).strip()}
        except Exception:
            pass

    return None


def check_python_version_satisfies(version_str: str, spec_str: str) -> bool:
    """Check if a Python version string satisfies a version specifier."""
    if not spec_str:
        return True
    clean_spec = spec_str.strip()

    # Convert poetry caret syntax (^3.10 -> >=3.10.0,<4.0.0)
    if clean_spec.startswith("^"):
        base = clean_spec[1:].strip()
        nums = [int(p) for p in re.findall(r"\d+", base)]
        major = nums[0] if nums else 3
        clean_spec = f">={base},<{major+1}.0.0"
    # Convert poetry tilde syntax (~3.10 -> >=3.10.0,<3.11.0)
    elif clean_spec.startswith("~"):
        base = clean_spec[1:].strip()
        nums = [int(p) for p in re.findall(r"\d+", base)]
        major = nums[0] if nums else 3
        minor = nums[1] if len(nums) > 1 else 0
        clean_spec = f">={base},<{major}.{minor+1}.0"
    # Bare versions like "3.11"
    elif re.match(r"^\d+\.\d+$", clean_spec):
        clean_spec = f"=={clean_spec}.*"

    clean_ver = version_str.split("+")[0].strip()

    if SpecifierSet and Version:
        try:
            spec = SpecifierSet(clean_spec)
            v = Version(clean_ver)
            return spec.contains(v, prereleases=True)
        except Exception:
            pass

    # Fallback comparison
    m = re.search(r"(\d+)\.(\d+)", clean_spec)
    vm = re.search(r"(\d+)\.(\d+)", clean_ver)
    if m and vm:
        t_maj, t_min = int(m.group(1)), int(m.group(2))
        v_maj, v_min = int(vm.group(1)), int(vm.group(2))
        if "<" in clean_spec and ">=" not in clean_spec:
            return (v_maj, v_min) < (t_maj, t_min)
        if ">=" in clean_spec:
            return (v_maj, v_min) >= (t_maj, t_min)
        return (v_maj, v_min) == (t_maj, t_min)
    return True


def resolve_compatible_python(
    workspace: Path,
    current_executable: str = sys.executable,
) -> tuple[str, str, dict[str, Any]]:
    """
    Select an appropriate Python interpreter based on repository metadata and system availability.
    Returns: (executable_path, version_string, details)
    """
    curr_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    req_info = detect_python_version_requirement(workspace)

    details: dict[str, Any] = {
        "required_specifier": req_info["specifier"] if req_info else None,
        "source": req_info["source"] if req_info else None,
        "selected_executable": current_executable,
        "version": curr_ver,
        "switched": False,
        "warning": None,
    }

    if not req_info or not req_info["specifier"]:
        return current_executable, curr_ver, details

    spec = req_info["specifier"]
    if check_python_version_satisfies(curr_ver, spec):
        return current_executable, curr_ver, details

    # Current interpreter does not satisfy requirement; search candidate binaries
    candidates = [
        "python3.12",
        "python3.11",
        "python3.10",
        "python3.13",
        "python3.9",
        "python3",
        "python",
    ]

    # Include explicit common directories if available
    extra_dirs = [
        Path("/usr/local/bin"),
        Path("/usr/bin"),
        Path("/opt/homebrew/bin"),
        Path.home() / ".local/bin",
        Path.home() / ".pyenv/shims",
    ]

    candidate_paths: list[str] = []
    for c in candidates:
        w = shutil.which(c)
        if w and w not in candidate_paths:
            candidate_paths.append(w)
        for ed in extra_dirs:
            p = ed / c
            if p.is_file() and os.access(p, os.X_OK) and str(p) not in candidate_paths:
                candidate_paths.append(str(p))

    for cand in candidate_paths:
        try:
            res = subprocess.run(
                [cand, "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')"],
                capture_output=True,
                text=True,
                timeout=4,
            )
            if res.returncode == 0:
                cand_ver = res.stdout.strip()
                if cand_ver and check_python_version_satisfies(cand_ver, spec):
                    details.update({
                        "selected_executable": cand,
                        "version": cand_ver,
                        "switched": True,
                        "warning": f"Selected compatible interpreter {cand} ({cand_ver}) satisfying {spec} from {req_info['source']}.",
                    })
                    return cand, cand_ver, details
        except Exception:
            continue

    # No compatible alternate found; fallback to current with informative diagnostic
    details["warning"] = (
        f"Repository specifies Python requirement '{spec}' (from {req_info['source']}), "
        f"but current interpreter is {curr_ver} and no compatible alternate was found in PATH. "
        "Proceeding with current interpreter."
    )
    return current_executable, curr_ver, details


async def run_process_safely(
    cmd: list[str],
    cwd: Path,
    timeout: float,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str, bool]:
    """
    Run an async subprocess with process group isolation, graceful termination, and output capture.
    Returns: (returncode, stdout, stderr, timed_out)
    """
    is_posix = sys.platform != "win32"
    timed_out = False
    proc = None
    so_buf: list[bytes] = []
    se_buf: list[bytes] = []

    async def _read_stream(stream: asyncio.StreamReader | None, buf: list[bytes]):
        if not stream:
            return
        while True:
            try:
                chunk = await stream.read(4096)
                if not chunk:
                    break
                buf.append(chunk)
            except (asyncio.CancelledError, Exception):
                break

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=is_posix,
        )

        t_out = asyncio.create_task(_read_stream(proc.stdout, so_buf))
        t_err = asyncio.create_task(_read_stream(proc.stderr, se_buf))

        try:
            await asyncio.wait_for(proc.wait(), timeout=max(0.5, timeout))
        except asyncio.TimeoutError:
            timed_out = True
            if proc:
                try:
                    if is_posix and hasattr(proc, "pid") and proc.pid:
                        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                    else:
                        proc.terminate()
                except (ProcessLookupError, PermissionError, OSError):
                    pass

                try:
                    await asyncio.wait_for(proc.wait(), timeout=3.0)
                except (asyncio.TimeoutError, Exception):
                    try:
                        if is_posix and hasattr(proc, "pid") and proc.pid:
                            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                        else:
                            proc.kill()
                    except (ProcessLookupError, PermissionError, OSError):
                        pass
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=1.0)
                    except Exception:
                        pass

        # Drain output streams with bounded timeout
        try:
            await asyncio.wait_for(asyncio.gather(t_out, t_err, return_exceptions=True), timeout=1.0)
        except asyncio.TimeoutError:
            t_out.cancel()
            t_err.cancel()

        stdout = b"".join(so_buf).decode(errors="replace")
        stderr = b"".join(se_buf).decode(errors="replace")
        rc = -1 if timed_out else (proc.returncode if proc and proc.returncode is not None else 0)
        return rc, stdout, stderr, timed_out

    except Exception as exc:
        stdout = b"".join(so_buf).decode(errors="replace")
        stderr = b"".join(se_buf).decode(errors="replace")
        return -1, stdout, f"Failed to execute command: {exc}\n{stderr}", False


def inspect_repository_dependencies(workspace: Path) -> dict[str, Any]:
    """Examine repository dependency manifests across multiple languages and compute a content hash."""
    manifests: list[dict[str, Any]] = []
    declared_packages: set[str] = set()
    language = "unknown"

    # 1. Python manifests: requirements*.txt
    req_files = list(workspace.glob("requirements*.txt")) + list(workspace.glob("requirements/*.txt"))
    for rf in sorted(req_files):
        if rf.is_file():
            text = rf.read_text(encoding="utf-8", errors="replace")
            pkgs = parse_requirements_txt(text)
            declared_packages.update(pkgs)
            h = hashlib.sha256(text.encode("utf-8")).hexdigest()
            manifests.append({"type": "requirements.txt", "path": str(rf.relative_to(workspace)), "hash": h, "packages": pkgs})
            language = "python"

    # 2. pyproject.toml
    pyproject = workspace / "pyproject.toml"
    if pyproject.is_file():
        text = pyproject.read_text(encoding="utf-8", errors="replace")
        pkgs = parse_pyproject_toml(text)
        declared_packages.update(pkgs)
        h = hashlib.sha256(text.encode("utf-8")).hexdigest()
        manifests.append({"type": "pyproject.toml", "path": "pyproject.toml", "hash": h, "packages": pkgs})
        language = "python"

    # 3. poetry.lock
    poetry_lock = workspace / "poetry.lock"
    if poetry_lock.is_file():
        text = poetry_lock.read_text(encoding="utf-8", errors="replace")
        pkgs = parse_poetry_lock(text)
        declared_packages.update(pkgs)
        h = hashlib.sha256(text.encode("utf-8")).hexdigest()
        manifests.append({"type": "poetry.lock", "path": "poetry.lock", "hash": h, "packages": pkgs})
        language = "python"

    # 4. uv.lock
    uv_lock = workspace / "uv.lock"
    if uv_lock.is_file():
        text = uv_lock.read_text(encoding="utf-8", errors="replace")
        pkgs = parse_uv_lock(text)
        declared_packages.update(pkgs)
        h = hashlib.sha256(text.encode("utf-8")).hexdigest()
        manifests.append({"type": "uv.lock", "path": "uv.lock", "hash": h, "packages": pkgs})
        language = "python"

    # 5. setup.py
    setup_py = workspace / "setup.py"
    if setup_py.is_file():
        text = setup_py.read_text(encoding="utf-8", errors="replace")
        pkgs = parse_setup_py(text)
        declared_packages.update(pkgs)
        h = hashlib.sha256(text.encode("utf-8")).hexdigest()
        manifests.append({"type": "setup.py", "path": "setup.py", "hash": h, "packages": pkgs})
        language = "python"

    # 6. Pipfile
    pipfile = workspace / "Pipfile"
    if pipfile.is_file():
        text = pipfile.read_text(encoding="utf-8", errors="replace")
        pkgs = parse_requirements_txt(text)
        declared_packages.update(pkgs)
        h = hashlib.sha256(text.encode("utf-8")).hexdigest()
        manifests.append({"type": "Pipfile", "path": "Pipfile", "hash": h, "packages": pkgs})
        language = "python"

    # 6b. Conda environment.yml / environment.yaml
    for env_name in ("environment.yml", "environment.yaml"):
        env_file = workspace / env_name
        if env_file.is_file():
            text = env_file.read_text(encoding="utf-8", errors="replace")
            pkgs = parse_conda_environment_yml(text)
            declared_packages.update(pkgs)
            h = hashlib.sha256(text.encode("utf-8")).hexdigest()
            manifests.append({"type": "environment.yml", "path": env_name, "hash": h, "packages": pkgs})
            language = "python"

    # 7. JavaScript / TypeScript
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

    for lock in ("package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb"):
        lf = workspace / lock
        if lf.is_file():
            h = hashlib.sha256(lf.read_bytes()).hexdigest()
            manifests.append({"type": lock, "path": lock, "hash": h, "packages": []})

    # 8. Rust
    cargo_toml = workspace / "Cargo.toml"
    if cargo_toml.is_file():
        h = hashlib.sha256(cargo_toml.read_bytes()).hexdigest()
        manifests.append({"type": "Cargo.toml", "path": "Cargo.toml", "hash": h, "packages": []})
        if language == "unknown":
            language = "rust"

    # 9. Go
    go_mod = workspace / "go.mod"
    if go_mod.is_file():
        h = hashlib.sha256(go_mod.read_bytes()).hexdigest()
        manifests.append({"type": "go.mod", "path": "go.mod", "hash": h, "packages": []})
        if language == "unknown":
            language = "go"

    # 10. Java
    pom_xml = workspace / "pom.xml"
    if pom_xml.is_file():
        h = hashlib.sha256(pom_xml.read_bytes()).hexdigest()
        manifests.append({"type": "pom.xml", "path": "pom.xml", "hash": h, "packages": []})
        if language == "unknown":
            language = "java"

    for g_file in ("build.gradle", "build.gradle.kts"):
        gf = workspace / g_file
        if gf.is_file():
            h = hashlib.sha256(gf.read_bytes()).hexdigest()
            manifests.append({"type": g_file, "path": g_file, "hash": h, "packages": []})
            if language == "unknown":
                language = "java"

    # 11. C/C++
    cmake = workspace / "CMakeLists.txt"
    if cmake.is_file():
        h = hashlib.sha256(cmake.read_bytes()).hexdigest()
        manifests.append({"type": "CMakeLists.txt", "path": "CMakeLists.txt", "hash": h, "packages": []})
        if language == "unknown":
            language = "c_cpp"

    has_manifest = len(manifests) > 0
    inference_diagnostics: dict[str, Any] = {}
    inferred_packages: list[str] = []
    dependency_source = "manifest" if has_manifest else "none"

    # Adaptive dependency detection: when no manifest exists for a Python repository
    has_python_files = bool(list(workspace.glob("*.py")) or list(workspace.glob("*/*.py")) or list(workspace.glob("**/*.py")))
    if not has_manifest and (language == "python" or has_python_files):
        language = "python"
        inference = infer_external_dependencies(workspace)
        inference_diagnostics = inference
        inferred_packages = inference.get("inferred_packages", [])

        if inferred_packages:
            dependency_source = "inferred"
            inferred_file = workspace / ".agentharness_inferred_requirements.txt"
            inferred_content = "\n".join(inferred_packages) + "\n"
            inferred_file.write_text(inferred_content, encoding="utf-8")

            # Prevent git contamination by excluding the temporary manifest
            git_info = workspace / ".git" / "info"
            if git_info.is_dir() or (workspace / ".git").is_dir():
                try:
                    git_info.mkdir(parents=True, exist_ok=True)
                    exclude_file = git_info / "exclude"
                    cur_ex = exclude_file.read_text(encoding="utf-8") if exclude_file.is_file() else ""
                    if ".agentharness_inferred_requirements.txt" not in cur_ex:
                        exclude_file.write_text(cur_ex.rstrip() + "\n.agentharness_inferred_requirements.txt\n", encoding="utf-8")
                except Exception:
                    pass

            h = hashlib.sha256(inferred_content.encode("utf-8")).hexdigest()
            manifests.append({
                "type": "inferred_requirements.txt",
                "path": ".agentharness_inferred_requirements.txt",
                "hash": h,
                "packages": inferred_packages,
                "inferred": True,
            })
            declared_packages.update(inferred_packages)

    # Combined hash across all manifests
    combined_hasher = hashlib.sha256()
    for m in sorted(manifests, key=lambda x: x["path"]):
        combined_hasher.update(m["path"].encode("utf-8"))
        combined_hasher.update(m["hash"].encode("utf-8"))
    combined_hash = combined_hasher.hexdigest()

    req_python = detect_python_version_requirement(workspace)

    return {
        "language": language,
        "manifests": manifests,
        "declared_packages": sorted(declared_packages),
        "inferred_packages": sorted(inferred_packages),
        "dependency_source": dependency_source,
        "combined_hash": combined_hash,
        "has_manifest": has_manifest,
        "required_python": req_python,
        "inference_diagnostics": inference_diagnostics,
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


def build_cache_key(
    repo_name: str,
    manifest_hash: str,
    python_version: str,
    platform_name: str | None = None,
    arch_name: str | None = None,
) -> str:
    """
    Generate an environment cache key incorporating repository name, platform,
    architecture, Python major.minor version, and dependency manifest hash.
    """
    clean_repo = re.sub(r"[^a-zA-Z0-9_\-]+", "_", repo_name)
    plat = (platform_name or sys.platform).lower()
    arch = (arch_name or platform.machine()).lower()
    # Normalize python tag to pyX.Y
    py_parts = [p for p in re.findall(r"\d+", python_version)]
    py_tag = f"py{py_parts[0]}.{py_parts[1]}" if len(py_parts) >= 2 else f"py{python_version}"
    return f"{clean_repo}-{plat}-{arch}-{py_tag}-{manifest_hash[:16]}"


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
    inferred_packages = dep_info.get("inferred_packages", [])
    dependency_source = dep_info.get("dependency_source", "manifest")
    inference_diagnostics = dep_info.get("inference_diagnostics", {})
    manifest_hash = dep_info["combined_hash"] or "no-manifest"
    started = time.perf_counter()

    has_heavyweight_ml = any(p in declared_packages for p in ("torch", "tensorflow", "transformers", "scipy", "triton"))
    base_timeout = float(settings.agentharness_dependency_timeout_seconds)
    # Heavyweight ML stacks (e.g. PyTorch + Transformers) require downloading >1.5GB of wheels.
    # Automatically grant at least 1800s (30 minutes) if the configured timeout is lower,
    # ensuring large binary wheels finish downloading on standard internet connections.
    timeout_budget = max(base_timeout, 1800.0 if has_heavyweight_ml and base_timeout < 1800.0 else base_timeout)
    deadline = started + timeout_budget

    def remaining_time() -> float:
        return max(1.0, deadline - time.perf_counter())

    def emit(typ: str, msg: str, data: dict[str, Any] | None = None):
        if event_callback:
            event_callback(typ, msg, data or {})

    # Non-Python repositories
    if language != "python" and not list(workspace.glob("*.py")) and not (workspace / "requirements.txt").exists():
        if language == "javascript/typescript":
            pkg = workspace / "package.json"
            if pkg.is_file() and not (workspace / "node_modules").exists() and shutil.which("npm"):
                emit("dependency_preparation_started", "Installing JavaScript dependencies via npm", {"language": language})
                rc, stdout, stderr, timed_out = await run_process_safely(
                    ["npm", "install", "--no-audit", "--prefer-offline"],
                    cwd=workspace,
                    timeout=remaining_time(),
                )
                dur = round((time.perf_counter() - started) * 1000)
                if timed_out:
                    return {
                        "status": "DEPENDENCY_INSTALL_BLOCKED",
                        "language": language,
                        "duration_ms": dur,
                        "error": f"npm install timed out after {timeout_budget}s",
                        "output": stdout + "\n" + stderr,
                        "diagnostics": {"reason": "TIMEOUT", "timeout_seconds": timeout_budget, "elapsed_ms": dur},
                    }
                if rc == 0:
                    emit("dependencies_ready", "Node dependencies installed", {"duration_ms": dur})
                    return {
                        "status": "DEPENDENCIES_READY",
                        "language": language,
                        "duration_ms": dur,
                        "cached": False,
                        "declared_packages": declared_packages,
                        "exit_code": 0,
                        "output": stdout,
                    }
                else:
                    emit("dependency_install_failed", "npm install exited with non-zero code", {"exit_code": rc})
                    return {
                        "status": "DEPENDENCY_INSTALL_FAILED",
                        "language": language,
                        "duration_ms": dur,
                        "cached": False,
                        "declared_packages": declared_packages,
                        "exit_code": rc,
                        "error": stderr,
                        "output": stdout + "\n" + stderr,
                    }
        return {
            "status": "DEPENDENCIES_READY",
            "language": language,
            "duration_ms": round((time.perf_counter() - started) * 1000),
            "cached": True,
            "declared_packages": declared_packages,
            "exit_code": 0,
            "output": f"Repository framework ({language}) detected; standard runtime environment active.",
        }

    # Python interpreter resolution
    selected_python, selected_py_ver, py_details = resolve_compatible_python(workspace)
    if py_details.get("warning"):
        emit("dependency_info", py_details["warning"], py_details)

    # Build cache key incorporating platform, architecture, python version, and manifest hash
    plat = sys.platform
    arch = platform.machine().lower()
    py_parts = [p for p in re.findall(r"\d+", selected_py_ver)]
    py_major_minor = f"{py_parts[0]}.{py_parts[1]}" if len(py_parts) >= 2 else selected_py_ver

    # Detect GPU availability and PyTorch wheel support
    gpu_info = detect_nvidia_gpu()
    has_gpu = gpu_info["available"]
    wheel_check = check_pytorch_cpu_wheel_support(selected_py_ver, plat, arch)

    cache_key = build_cache_key(repo_name, manifest_hash, selected_py_ver, plat, arch)
    settings.venvs_root.mkdir(parents=True, exist_ok=True)
    venv_dir = (settings.venvs_root / cache_key).resolve()
    lock_file = settings.venvs_root / f"{cache_key}.lock"
    ready_marker = venv_dir / ".agentharness_ready"
    python_bin = venv_dir / "bin" / "python"
    pytest_bin = venv_dir / "bin" / "pytest"
    workspace_venv_link = workspace / ".venv"

    try:
        async with AsyncFileLock(lock_file, timeout=min(120.0, remaining_time())):
            # Check cache validity inside the lock
            if ready_marker.is_file() and python_bin.is_file() and pytest_bin.is_file():
                try:
                    marker_data = json.loads(ready_marker.read_text(encoding="utf-8"))
                    m_hash = marker_data.get("hash")
                    m_plat = marker_data.get("platform")
                    m_arch = marker_data.get("architecture")
                    m_py = marker_data.get("python_version", "")

                    if (
                        m_hash == manifest_hash
                        and m_plat == plat
                        and m_arch == arch
                        and m_py.startswith(py_major_minor)
                        and marker_data.get("status") == "DEPENDENCIES_READY"
                    ):
                        # Fast sanity verification
                        rc, so, _, to = await run_process_safely(
                            [str(python_bin), "-c", "import sys; print('ready')"],
                            cwd=workspace,
                            timeout=5.0,
                        )
                        if rc == 0 and "ready" in so:
                            dur = round((time.perf_counter() - started) * 1000)
                            if not workspace_venv_link.exists():
                                try:
                                    workspace_venv_link.symlink_to(venv_dir, target_is_directory=True)
                                except OSError:
                                    pass
                            emit("dependencies_ready", "Using cached Python virtual environment", {
                                "venv": str(venv_dir),
                                "duration_ms": dur,
                                "python_version": selected_py_ver,
                                "manifest_hash": manifest_hash,
                            })
                            return {
                                "status": "DEPENDENCIES_READY",
                                "language": "python",
                                "cached": True,
                                "duration_ms": dur,
                                "manifest_hash": manifest_hash,
                                "declared_packages": declared_packages,
                                "inferred_packages": marker_data.get("inferred_packages", inferred_packages),
                                "dependency_source": marker_data.get("dependency_source", dependency_source),
                                "venv_path": venv_dir,
                                "python_bin": python_bin,
                                "pytest_bin": pytest_bin,
                                "python_version": selected_py_ver,
                                "python_executable": selected_python,
                                "exit_code": 0,
                                "output": "Reused cached environment matching dependency manifest hash.",
                                "diagnostics": {
                                    "system_resources": get_system_resource_info(),
                                    "gpu_profile": marker_data.get("gpu_profile") or {
                                        "has_gpu": has_gpu,
                                        "devices": gpu_info.get("devices", []),
                                        "reason": gpu_info.get("reason"),
                                    },
                                    "pytorch_telemetry": marker_data.get("pytorch_info", {}).get("pip_telemetry", {}),
                                    "pytorch_wheel_check": wheel_check,
                                    "cuda_packages_detected_on_cpu": False,
                                    "dependency_source": marker_data.get("dependency_source", dependency_source),
                                    "inferred_packages": marker_data.get("inferred_packages", inferred_packages),
                                },
                            }
                except Exception:
                    pass

            emit("dependency_preparation_started", f"Provisioning isolated virtual environment for {repo_name}", {
                "cache_dir": str(venv_dir),
                "python": selected_python,
                "python_version": selected_py_ver,
            })

            # Create fresh virtual environment directory
            if venv_dir.exists():
                shutil.rmtree(venv_dir, ignore_errors=True)
            venv_dir.mkdir(parents=True, exist_ok=True)

            # 1. Create venv using selected Python interpreter
            cmd_venv = [selected_python, "-m", "venv", str(venv_dir)]
            rc_v, so_v, se_v, to_v = await run_process_safely(cmd_venv, cwd=workspace, timeout=min(60.0, remaining_time()))
            if to_v:
                dur = round((time.perf_counter() - started) * 1000)
                return {
                    "status": "DEPENDENCY_INSTALL_BLOCKED",
                    "failure_code": "DEPENDENCY_INSTALL_TIMEOUT",
                    "language": "python",
                    "duration_ms": dur,
                    "error": "Virtual environment creation timed out",
                    "declared_packages": declared_packages,
                    "diagnostics": {
                        "reason": "TIMEOUT",
                        "failure_code": "DEPENDENCY_INSTALL_TIMEOUT",
                        "timeout_seconds": 60,
                        "elapsed_ms": dur,
                        "actionable_suggestion": "Virtual environment creation exceeded 60s. Check host filesystem performance.",
                        "system_resources": get_system_resource_info(),
                    },
                }
            if rc_v != 0:
                dur = round((time.perf_counter() - started) * 1000)
                emit("dependency_install_failed", "Failed to create Python virtual environment", {"error": se_v})
                return {
                    "status": "DEPENDENCY_INSTALL_FAILED",
                    "failure_code": "DEPENDENCY_INSTALL_INVALID_ENVIRONMENT",
                    "language": "python",
                    "duration_ms": dur,
                    "error": f"venv creation failed: {se_v.strip() or so_v.strip()}",
                    "declared_packages": declared_packages,
                    "diagnostics": {
                        "reason": "VENV_CREATION_FAILED",
                        "failure_code": "DEPENDENCY_INSTALL_INVALID_ENVIRONMENT",
                        "exit_code": rc_v,
                        "stderr": se_v,
                        "python_executable": selected_python,
                        "actionable_suggestion": "Ensure the selected Python interpreter has the standard 'venv' module installed.",
                        "system_resources": get_system_resource_info(),
                    },
                }

            pip_bin = venv_dir / "bin" / "pip"
            pip_cache = settings.pip_cache_path
            pip_cache.mkdir(parents=True, exist_ok=True)

            # Base pip installation command with caching and robust flags
            base_pip_cmd = [
                str(pip_bin),
                "install",
                "--prefer-binary",
                "--disable-pip-version-check",
                "--no-input",
                "--retries", "5",
                "--timeout", "60",
                "--cache-dir", str(pip_cache),
            ]

            # Optional user-configured index URLs
            if getattr(settings, "agentharness_pip_index_url", None):
                base_pip_cmd.extend(["--index-url", settings.agentharness_pip_index_url])
            if getattr(settings, "agentharness_pip_extra_index_url", None):
                base_pip_cmd.extend(["--extra-index-url", settings.agentharness_pip_extra_index_url])

            # Optimize heavyweight ML dependencies: PyTorch CPU wheels for CPU-only environments
            has_torch = any(p in declared_packages for p in ("torch", "torchvision", "torchaudio"))
            extra_index_used = None

            if has_torch and not getattr(settings, "agentharness_pip_extra_index_url", None):
                if not has_gpu:
                    # Provide official pre-built CPU wheels for CPU-only environments across Linux (x86_64, aarch64), Windows, etc.
                    # download.pytorch.org/whl/cpu hosts pre-built CPU wheels for both x86_64 and aarch64
                    # which do not pull multi-gigabyte NVIDIA CUDA runtime packages.
                    rec_index = wheel_check.get("recommended_index") or "https://download.pytorch.org/whl/cpu"
                    base_pip_cmd.extend(["--extra-index-url", rec_index])
                    extra_index_used = rec_index

            # Prepare installation command arguments
            install_targets: list[str] = ["pytest"]
            req_file_found = False

            # If on a CPU-only host, filter out GPU-specific requirement files (e.g. requirements-gpu.txt, requirements-cuda.txt)
            # if standard or CPU requirement files exist.
            applicable_manifests = manifests
            if not has_gpu:
                has_cpu_or_std = any(
                    m["type"] in {"requirements.txt", "inferred_requirements.txt"} and not any(k in m["path"].lower() for k in ("gpu", "cuda"))
                    for m in manifests
                )
                if has_cpu_or_std:
                    applicable_manifests = [
                        m for m in manifests
                        if not (m["type"] in {"requirements.txt", "inferred_requirements.txt"} and any(k in m["path"].lower() for k in ("gpu", "cuda")))
                    ]

            for m in applicable_manifests:
                if m["type"] in {"requirements.txt", "inferred_requirements.txt"}:
                    req_path = (workspace / m["path"]).resolve()
                    if req_path.is_file():
                        install_targets.extend(["-r", str(req_path)])
                        req_file_found = True

            if not req_file_found:
                # If pyproject.toml, poetry.lock, or uv.lock without requirements.txt
                if (workspace / "pyproject.toml").is_file():
                    install_targets.append(str(workspace))
                elif declared_packages:
                    install_targets.extend(list(declared_packages))

            full_install_cmd = base_pip_cmd + install_targets
            cmd_str = sanitize_credentials(" ".join(full_install_cmd))
            emit("dependency_install_step", f"Running: {cmd_str}", {"command": full_install_cmd})

            rem_time = remaining_time()
            rc, stdout, stderr, timed_out = await run_process_safely(
                full_install_cmd,
                cwd=workspace,
                timeout=rem_time,
            )

            dur = round((time.perf_counter() - started) * 1000)
            so_clean = sanitize_credentials(stdout)
            se_clean = sanitize_credentials(stderr)
            combined_log = so_clean + ("\n" + se_clean if se_clean else "")

            # Parse pip telemetry from installation log
            pip_telemetry = parse_pip_telemetry(combined_log)
            cuda_on_cpu = bool(not has_gpu and pip_telemetry.get("transitive_nvidia_dependencies"))

            if timed_out:
                emit("dependency_install_failed", f"Dependency installation timed out after {timeout_budget}s", {"elapsed_ms": dur})
                diag_status, diag_reason, diag_suggestion = classify_dependency_error(
                    exit_code=-1,
                    timed_out=True,
                    combined_log=combined_log,
                    timeout_budget=timeout_budget,
                    dur=dur,
                )
                if cuda_on_cpu:
                    diag_suggestion = (
                        f"Unnecessary NVIDIA CUDA packages were detected in a CPU-only environment ({', '.join(pip_telemetry['transitive_nvidia_dependencies'])}). "
                        "AgentHarness uses '--extra-index-url https://download.pytorch.org/whl/cpu' to avoid CUDA package explosion."
                    )
                diagnostics = {
                    "reason": diag_reason,
                    "failure_code": diag_status,
                    "timeout_seconds": timeout_budget,
                    "elapsed_ms": dur,
                    "python_version": selected_py_ver,
                    "python_executable": selected_python,
                    "failed_command": cmd_str,
                    "declared_packages": declared_packages,
                    "actionable_suggestion": diag_suggestion,
                    "gpu_profile": {"has_gpu": has_gpu, "reason": gpu_info.get("reason"), "devices": gpu_info.get("devices", [])},
                    "pytorch_telemetry": pip_telemetry,
                    "pytorch_wheel_check": wheel_check,
                    "cuda_packages_detected_on_cpu": cuda_on_cpu,
                    "stderr_tail": se_clean[-2000:],
                    "stdout_tail": so_clean[-2000:],
                    "system_resources": get_system_resource_info(),
                }
                return {
                    "status": "DEPENDENCY_INSTALL_BLOCKED",
                    "failure_code": diag_status,
                    "language": "python",
                    "duration_ms": dur,
                    "cached": False,
                    "manifest_hash": manifest_hash,
                    "declared_packages": declared_packages,
                    "venv_path": venv_dir,
                    "exit_code": -1,
                    "error": f"Dependency installation timed out after {timeout_budget}s (took {dur} ms).",
                    "output": combined_log[-5000:],
                    "diagnostics": diagnostics,
                }

            if rc != 0:
                diag_status, diag_reason, diag_suggestion = classify_dependency_error(
                    exit_code=rc,
                    timed_out=False,
                    combined_log=combined_log,
                    timeout_budget=timeout_budget,
                    dur=dur,
                )
                state = "DEPENDENCY_INSTALL_BLOCKED" if diag_status == "DEPENDENCY_INSTALL_NETWORK_ERROR" else "DEPENDENCY_INSTALL_FAILED"
                emit("dependency_install_failed", f"Dependency installation failed with code {rc}", {"state": state, "exit_code": rc})
                if cuda_on_cpu:
                    diag_suggestion = (
                        f"Unnecessary NVIDIA CUDA packages were detected in a CPU-only environment ({', '.join(pip_telemetry['transitive_nvidia_dependencies'])}). "
                        "AgentHarness uses '--extra-index-url https://download.pytorch.org/whl/cpu' to avoid CUDA package explosion."
                    )
                diagnostics = {
                    "reason": diag_reason,
                    "failure_code": diag_status,
                    "exit_code": rc,
                    "elapsed_ms": dur,
                    "python_version": selected_py_ver,
                    "python_executable": selected_python,
                    "failed_command": cmd_str,
                    "declared_packages": declared_packages,
                    "actionable_suggestion": diag_suggestion,
                    "gpu_profile": {"has_gpu": has_gpu, "reason": gpu_info.get("reason"), "devices": gpu_info.get("devices", [])},
                    "pytorch_telemetry": pip_telemetry,
                    "pytorch_wheel_check": wheel_check,
                    "cuda_packages_detected_on_cpu": cuda_on_cpu,
                    "stderr_tail": se_clean[-2000:],
                    "stdout_tail": so_clean[-2000:],
                    "system_resources": get_system_resource_info(),
                }
                return {
                    "status": state,
                    "failure_code": diag_status,
                    "language": "python",
                    "duration_ms": dur,
                    "cached": False,
                    "manifest_hash": manifest_hash,
                    "declared_packages": declared_packages,
                    "venv_path": venv_dir,
                    "exit_code": rc,
                    "error": se_clean[-1000:] or so_clean[-1000:] or f"pip install exited with code {rc}",
                    "output": combined_log[-5000:],
                    "diagnostics": diagnostics,
                }

            # Verify pytest is available in venv
            if not pytest_bin.is_file():
                rc_pt, so_pt, se_pt, to_pt = await run_process_safely(
                    [str(pip_bin), "install", "--prefer-binary", "--cache-dir", str(pip_cache), "pytest"],
                    cwd=workspace,
                    timeout=remaining_time(),
                )
                if rc_pt != 0 or not pytest_bin.is_file():
                    emit("dependency_install_failed", "pytest installation failed", {"error": se_pt})
                    return {
                        "status": "DEPENDENCY_INSTALL_FAILED",
                        "failure_code": "DEPENDENCY_INSTALL_FAILED",
                        "language": "python",
                        "duration_ms": round((time.perf_counter() - started) * 1000),
                        "cached": False,
                        "manifest_hash": manifest_hash,
                        "declared_packages": declared_packages,
                        "venv_path": venv_dir,
                        "exit_code": rc_pt,
                        "error": "Failed to install pytest into virtual environment.",
                        "output": se_pt,
                        "diagnostics": {
                            "reason": "PYTEST_INSTALL_FAILED",
                            "failure_code": "DEPENDENCY_INSTALL_FAILED",
                            "stderr": se_pt,
                            "system_resources": get_system_resource_info(),
                        },
                    }

            # Post-installation import verification: confirm declared packages are genuinely usable
            imports_ok, failed_pkg, import_err = await verify_environment_imports(
                python_bin=python_bin,
                declared_packages=declared_packages,
                cwd=workspace,
                timeout=min(30.0, remaining_time()),
            )
            if not imports_ok:
                dur = round((time.perf_counter() - started) * 1000)
                emit("dependency_install_failed", f"Import verification failed: {import_err}", {"failed_package": failed_pkg})
                diagnostics = {
                    "reason": "IMPORT_VERIFICATION_FAILED",
                    "failure_code": "DEPENDENCY_INSTALL_FAILED",
                    "failed_package": failed_pkg,
                    "elapsed_ms": dur,
                    "python_version": selected_py_ver,
                    "python_executable": selected_python,
                    "failed_command": f"{python_bin} -c import {failed_pkg}",
                    "declared_packages": declared_packages,
                    "actionable_suggestion": (
                        f"Declared package '{failed_pkg}' was not importable after installation ({import_err}). "
                        "Verify package dependencies and build prerequisites."
                    ),
                    "gpu_profile": {"has_gpu": has_gpu, "reason": gpu_info.get("reason"), "devices": gpu_info.get("devices", [])},
                    "pytorch_telemetry": pip_telemetry,
                    "pytorch_wheel_check": wheel_check,
                    "cuda_packages_detected_on_cpu": cuda_on_cpu,
                    "stderr_tail": import_err or "",
                    "stdout_tail": "",
                    "system_resources": get_system_resource_info(),
                }
                return {
                    "status": "DEPENDENCY_INSTALL_FAILED",
                    "failure_code": "DEPENDENCY_INSTALL_FAILED",
                    "language": "python",
                    "duration_ms": dur,
                    "cached": False,
                    "manifest_hash": manifest_hash,
                    "declared_packages": declared_packages,
                    "venv_path": venv_dir,
                    "exit_code": 1,
                    "error": import_err or f"Failed to import package '{failed_pkg}'",
                    "output": import_err or "",
                    "diagnostics": diagnostics,
                }

            # Write readiness marker with comprehensive metadata ONLY after imports are verified
            ready_marker.write_text(json.dumps({
                "hash": manifest_hash,
                "repo": repo_name,
                "python_version": selected_py_ver,
                "python_executable": selected_python,
                "platform": plat,
                "architecture": arch,
                "created_at": time.time(),
                "declared_packages": declared_packages,
                "inferred_packages": inferred_packages,
                "dependency_source": dependency_source,
                "status": "DEPENDENCIES_READY",
                "verified_imports": True,
                "gpu_profile": {
                    "has_gpu": has_gpu,
                    "reason": gpu_info.get("reason"),
                    "devices": gpu_info.get("devices", []),
                },
                "pytorch_info": {
                    "has_torch": has_torch,
                    "wheel_check": wheel_check,
                    "pip_telemetry": pip_telemetry,
                    "extra_index_used": extra_index_used,
                },
            }, indent=2), encoding="utf-8")

            if not workspace_venv_link.exists():
                try:
                    workspace_venv_link.symlink_to(venv_dir, target_is_directory=True)
                except OSError:
                    pass

            emit("dependencies_ready", f"Dependencies installed successfully into isolated venv ({dur} ms)", {
                "duration_ms": dur,
                "declared_packages": len(declared_packages),
                "inferred_packages": len(inferred_packages),
                "dependency_source": dependency_source,
                "python_version": selected_py_ver,
            })

            return {
                "status": "DEPENDENCIES_READY",
                "language": "python",
                "cached": False,
                "duration_ms": dur,
                "manifest_hash": manifest_hash,
                "declared_packages": declared_packages,
                "inferred_packages": inferred_packages,
                "dependency_source": dependency_source,
                "venv_path": venv_dir,
                "python_bin": python_bin,
                "pytest_bin": pytest_bin,
                "python_version": selected_py_ver,
                "python_executable": selected_python,
                "exit_code": 0,
                "output": combined_log[-5000:],
                "diagnostics": {
                    "system_resources": get_system_resource_info(),
                    "gpu_profile": {
                        "has_gpu": has_gpu,
                        "reason": gpu_info.get("reason"),
                        "devices": gpu_info.get("devices", []),
                    },
                    "pytorch_telemetry": pip_telemetry,
                    "pytorch_wheel_check": wheel_check,
                    "cuda_packages_detected_on_cpu": cuda_on_cpu,
                    "dependency_source": dependency_source,
                    "inferred_packages": inferred_packages,
                    "inference_diagnostics": inference_diagnostics,
                },
            }

    except TimeoutError as exc:
        dur = round((time.perf_counter() - started) * 1000)
        return {
            "status": "DEPENDENCY_CACHE_ERROR",
            "failure_code": "DEPENDENCY_CACHE_ERROR",
            "language": "python",
            "duration_ms": dur,
            "error": str(exc),
            "declared_packages": declared_packages,
            "diagnostics": {
                "reason": "CACHE_LOCK_TIMEOUT",
                "failure_code": "DEPENDENCY_CACHE_ERROR",
                "elapsed_ms": dur,
                "actionable_suggestion": "Another process held the virtual environment lock for too long. Check for hung background processes.",
                "system_resources": get_system_resource_info(),
            },
        }
