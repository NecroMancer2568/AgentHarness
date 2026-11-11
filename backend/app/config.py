import os
from functools import lru_cache
from pathlib import Path
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

def _find_project_root() -> Path:
    if "AGENTHARNESS_PROJECT_ROOT" in os.environ:
        return Path(os.environ["AGENTHARNESS_PROJECT_ROOT"]).resolve()
    resolved = Path(__file__).resolve()
    # If in standard repo layout with backend/
    for parent in resolved.parents:
        if (parent / "backend").exists() and (parent / "frontend").exists():
            return parent
    # If inside container where app is at /app/app and workspace is /app/workspace
    for parent in resolved.parents:
        if (parent / "workspace").exists():
            return parent
    if len(resolved.parents) > 1 and resolved.parents[1].name == "app":
        return resolved.parents[1]
    return resolved.parents[2] if len(resolved.parents) > 2 else resolved.parent

PROJECT_ROOT = _find_project_root()

class Settings(BaseSettings):
    port: int = 8000
    ollama_api_key: str = ""
    ollama_base_url: str = "https://ollama.com/v1"
    ollama_model: str = "deepseek-v4-pro:cloud"
    ollama_timeout_seconds: int = 180
    agentharness_repos_root: str = "workspace/repositories"
    agentharness_runs_root: str = "workspace/runs"
    agentharness_database: str = "workspace/agentharness.sqlite3"
    agentharness_max_agent_steps: int = 8
    agentharness_max_parallel_readers: int = 2
    agentharness_test_timeout_seconds: int = 180
    agentharness_benchmark_sample_timeout_seconds: int = 1800
    agentharness_max_tool_output_chars: int = 12000
    agentharness_cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173,http://localhost:8080,http://127.0.0.1:8080,http://localhost,http://127.0.0.1"
    agentharness_dependency_timeout_seconds: int = 300
    agentharness_venv_cache_root: str = "workspace/venvs"
    model_config = SettingsConfigDict(env_file=("backend/.env", ".env"), env_file_encoding="utf-8", extra="ignore")

    @field_validator("ollama_base_url")
    @classmethod
    def _normalize_ollama_base_url(cls, value: str) -> str:
        clean = value.strip().rstrip("/")
        if not clean.endswith("/v1"):
            clean = f"{clean}/v1"
        return clean

    @property
    def is_local_ollama(self) -> bool:
        url = self.ollama_base_url.lower()
        return (
            "localhost" in url
            or "127.0.0.1" in url
            or "ollama:" in url
            or "host.docker.internal" in url
            or url.startswith("http://ollama")
            or (url.startswith("http://") and "ollama.com" not in url)
        )

    def resolve_path(self, value: str) -> Path:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return path.resolve()

    @property
    def repos_root(self) -> Path:
        return self.resolve_path(self.agentharness_repos_root)

    @property
    def runs_root(self) -> Path:
        return self.resolve_path(self.agentharness_runs_root)

    @property
    def venvs_root(self) -> Path:
        return self.resolve_path(self.agentharness_venv_cache_root)

    @property
    def database_path(self) -> Path:
        return self.resolve_path(self.agentharness_database)

    @property
    def cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.agentharness_cors_origins.split(",") if origin.strip()]

@lru_cache
def get_settings() -> Settings:
    return Settings()
