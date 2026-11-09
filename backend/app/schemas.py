from datetime import datetime
from typing import Any, Literal
from pydantic import BaseModel, Field

RunStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]

class RunCreate(BaseModel):
    repository: str = Field(min_length=1, max_length=200)
    task: str = Field(min_length=10, max_length=8000)
    test_command: str = Field(default="auto", max_length=200)
    max_agent_steps: int | None = Field(default=None, ge=1, le=20)

class RepositoryRegister(BaseModel):
    path: str = Field(min_length=1, max_length=1000)

class RunSummary(BaseModel):
    id: str
    repository: str
    task: str
    status: RunStatus
    created_at: str
    updated_at: str
    duration_ms: int | None = None
    success: bool | None = None
    total_tokens: int = 0
    llm_calls: int = 0
    error: str | None = None

class RunDetail(RunSummary):
    result: dict[str, Any] | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)

class EventOut(BaseModel):
    id: int
    run_id: str
    timestamp: str
    event_type: str
    agent: str | None = None
    message: str
    data: dict[str, Any] = Field(default_factory=dict)

class HealthOut(BaseModel):
    status: str
    model: str
    provider_configured: bool
    repository_root: str


class BenchmarkStart(BaseModel):
    repetitions: int = Field(default=1, ge=1, le=5)
    dataset: str = Field(default="tasks.jsonl", min_length=1, max_length=100)
