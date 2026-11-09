import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from .config import get_settings
from .db import Database
from .llm import LLMClient
from .repository import RepositoryManager, WorkspaceError
from .runner import RunManager
from .benchmarks import BenchmarkManager
from .schemas import BenchmarkStart, HealthOut, RepositoryRegister, RunCreate

settings = get_settings()
db = Database(settings.database_path)
repos = RepositoryManager(settings)
llm = LLMClient(settings)
run_manager = RunManager(settings, db, llm, repos)
benchmark_manager = BenchmarkManager(settings, db, run_manager, repos)

@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    for task in list(run_manager.tasks.values()) + list(benchmark_manager.tasks.values()):
        if not task.done(): task.cancel()
    await llm.client.close()

app = FastAPI(title="AgentHarness Lab API", version="1.0.0", description="Local multi-agent software engineering workbench", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

@app.get("/api/health", response_model=HealthOut)
async def health():
    configured = bool(settings.ollama_api_key) or settings.is_local_ollama
    return {"status":"ok", "model":settings.ollama_model, "provider_configured":configured, "repository_root":str(settings.repos_root)}

@app.get("/api/models")
async def models():
    provider_name = "Local Ollama" if settings.is_local_ollama else "Ollama Cloud"
    return {"provider":provider_name, "base_url":settings.ollama_base_url, "model":settings.ollama_model,
            "context_note":"Model context and output limits are provider-side. Keep prompts focused; large context windows do not make every prompt free."}

@app.get("/api/repositories")
async def list_repositories():
    return {"repositories":repos.list_repositories(), "root":str(settings.repos_root)}

@app.post("/api/repositories/register")
async def register_repository(body: RepositoryRegister):
    try: item = repos.register_path(body.path)
    except WorkspaceError as exc: raise HTTPException(400, str(exc))
    return {"repository":item}

@app.post("/api/runs", status_code=202)
async def create_run(body: RunCreate):
    try: repo = repos.resolve_repository(body.repository)
    except WorkspaceError as exc: raise HTTPException(400, str(exc))
    run_id = uuid.uuid4().hex[:12]
    db.create_run(run_id, repo.name, body.task, body.test_command, body.max_agent_steps)
    task = asyncio.create_task(run_manager.execute(run_id), name=f"agentharness-run-{run_id}")
    run_manager.tasks[run_id] = task
    return {"id":run_id, "status":"queued"}

@app.get("/api/runs")
async def list_runs(limit: int = 50):
    limit = max(1, min(limit, 200))
    rows = db.list_runs(limit)
    return {"runs":[_serialize_run(row) for row in rows]}

@app.get("/api/runs/{run_id}")
async def get_run(run_id: str):
    row = db.get_run(run_id)
    if not row: raise HTTPException(404, "Run not found")
    return _serialize_run(row, detail=True)

@app.get("/api/runs/{run_id}/events")
async def get_events(run_id: str, after_id: int = 0):
    if not db.get_run(run_id): raise HTTPException(404, "Run not found")
    return {"events":db.get_events(run_id, after_id=after_id)}

@app.get("/api/runs/{run_id}/stream")
async def stream_events(run_id: str, request: Request, after_id: int = 0):
    if not db.get_run(run_id): raise HTTPException(404, "Run not found")
    async def generator():
        cursor = after_id
        while not await request.is_disconnected():
            events = db.get_events(run_id, after_id=cursor, limit=100)
            for event in events:
                cursor = event["id"]
                yield f"id: {cursor}\nevent: {event['event_type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
            row = db.get_run(run_id)
            if row and row["status"] in {"succeeded", "failed", "cancelled"} and not events:
                yield "event: done\ndata: {}\n\n"
                break
            await asyncio.sleep(0.7)
    return StreamingResponse(generator(), media_type="text/event-stream", headers={"Cache-Control":"no-cache", "X-Accel-Buffering":"no"})

@app.post("/api/runs/{run_id}/cancel")
async def cancel_run(run_id: str):
    row = db.get_run(run_id)
    if not row: raise HTTPException(404, "Run not found")
    task = run_manager.tasks.get(run_id)
    if task and not task.done():
        task.cancel()
        return {"id":run_id, "status":"cancelling"}
    if row["status"] in {"queued", "running"}:
        db.update_run(run_id, status="cancelled", error="Run cancelled.")
    return {"id":run_id, "status":"cancelled"}

@app.get("/api/metrics")
async def metrics():
    return db.aggregate_metrics()


@app.get("/api/benchmarks/datasets")
async def benchmark_datasets():
    return {"datasets": benchmark_manager.list_datasets()}

@app.get("/api/benchmarks/tasks")
async def benchmark_tasks(dataset: str = "tasks.jsonl"):
    try:
        return {"tasks": benchmark_manager.task_catalog(dataset), "dataset": dataset}
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(400, f"Benchmark manifest is invalid: {exc}")

@app.get("/api/benchmarks")
async def list_benchmarks(limit: int = 20):
    return {"benchmarks": benchmark_manager.list_runs(limit)}

@app.post("/api/benchmarks", status_code=202)
async def start_benchmark(body: BenchmarkStart):
    try:
        record = benchmark_manager.start(body.repetitions, body.dataset)
    except WorkspaceError as exc:
        raise HTTPException(400, str(exc))
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(400, str(exc))
    return {"id": record["id"], "status": record["status"], "planned_samples": record["planned_samples"]}

@app.get("/api/benchmarks/{benchmark_id}")
async def get_benchmark(benchmark_id: str):
    record = benchmark_manager.get(benchmark_id)
    if not record:
        raise HTTPException(404, "Benchmark run not found")
    return record

@app.post("/api/benchmarks/{benchmark_id}/cancel")
async def cancel_benchmark(benchmark_id: str):
    if not benchmark_manager.get(benchmark_id):
        raise HTTPException(404, "Benchmark run not found")
    cancelled = benchmark_manager.cancel(benchmark_id)
    return {"id": benchmark_id, "status": "cancelling" if cancelled else (benchmark_manager.get(benchmark_id) or {}).get("status", "unknown")}

@app.get("/api/settings")
async def get_public_settings():
    configured = bool(settings.ollama_api_key) or settings.is_local_ollama
    return {"model":settings.ollama_model, "base_url":settings.ollama_base_url,
      "repos_root":str(settings.repos_root), "runs_root":str(settings.runs_root),
      "max_agent_steps":settings.agentharness_max_agent_steps,
      "max_parallel_readers":settings.agentharness_max_parallel_readers,
      "test_timeout_seconds":settings.agentharness_test_timeout_seconds,
      "api_key_configured":configured}

def _serialize_run(row: dict, detail: bool = False):
    item = {"id":row["id"], "repository":row["repository"], "task":row["task"], "status":row["status"],
      "created_at":row["created_at"], "updated_at":row["updated_at"], "duration_ms":row["duration_ms"],
      "success":None if row["success"] is None else bool(row["success"]), "total_tokens":row["total_tokens"],
      "prompt_tokens":row["prompt_tokens"], "completion_tokens":row["completion_tokens"], "llm_calls":row["llm_calls"], "error":row["error"]}
    if detail:
        item["result"] = json.loads(row["result_json"]) if row.get("result_json") else None
        item["metrics"] = json.loads(row["metrics_json"] or "{}")
        item["workspace_path"] = row.get("workspace_path")
        item["events"] = db.get_events(row["id"])
    return item
