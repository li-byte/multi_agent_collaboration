"""FastAPI 服务：REST + SSE + 静态单页。

通用版接口：
    POST /api/runs   {question, materials: [原文…]}
      · 没给资料 → 400 need_material，让前端去追问用户
      · 给了资料 → 切段存库，四个智能体只允许引用这些片段

SSE 的关键点：事件**先落账再推送**，浏览器刷新或断线重连都能完整回放。
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from . import mock_data
from .config import BASE_DIR, get_settings
from .graph import build_orchestrator

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("multi_agent.server")

settings = get_settings()
WEB_DIR = BASE_DIR / "web"
RUN_SEMAPHORE = asyncio.Semaphore(2)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with build_orchestrator(settings) as orch:
        app.state.orch = orch
        yield


app = FastAPI(title="多 Agent 协作一致性演示", version="0.2.0", lifespan=lifespan)


# ---------------------------------------------------------------- 请求模型

class RunRequest(BaseModel):
    question: str = Field(min_length=1, description="用户的问题")
    materials: list[str] = Field(default_factory=list, description="用户提供的资料（原文，可多份）")


# ---------------------------------------------------------------- 工具

def _iso(value):
    return value.isoformat() if value is not None else None


def _task_json(row: dict) -> dict:
    return {
        "sub_task_id": row["sub_task_id"],
        "role": row["role"],
        "agent_id": row["agent_id"],
        "attempt": row["attempt"],
        "status": row["status"],
        "version": row["version"],
        "lease_until": _iso(row.get("lease_until")),
        "input_context": row.get("input_context"),
        "citations": row.get("citations") or [],
        "result_ref": row.get("result_ref"),
        "idempotency_key": row.get("idempotency_key"),
        "error": row.get("error"),
        "created_at": _iso(row.get("created_at")),
    }


def _finding_json(row: dict) -> dict:
    return {
        "finding_id": row["finding_id"],
        "sub_task_id": row["sub_task_id"],
        "round": row["round"],
        "claim": row["claim"],
        "status": row["status"],
        "citations": row.get("citations") or [],
        "insufficient": bool(row.get("insufficient")),
        "assumptions": row.get("assumptions") or [],
        "created_at": _iso(row.get("created_at")),
    }


def _sse(event: dict) -> dict:
    return {"event": event["event_type"], "id": str(event["seq"]),
            "data": json.dumps(event, ensure_ascii=False, default=str)}


# ---------------------------------------------------------------- 静态页 / 健康

@app.get("/")
async def index() -> FileResponse:
    path = WEB_DIR / "index.html"
    if not path.exists():
        raise HTTPException(404, "web/index.html 未找到")
    return FileResponse(path)


@app.get("/api/health")
async def health(request: Request) -> dict:
    orch = request.app.state.orch
    try:
        pg_ok = await orch.ledger.ping()
    except Exception:  # noqa: BLE001
        pg_ok = False
    return {
        "pg": "ok" if pg_ok else "error",
        "llm_mode": settings.llm_mode,
        "llm": "ok" if (settings.llm_mode == "mock" or orch.llm is not None) else "error",
        "checkpointer": orch._saver is not None,
        "model": settings.deepseek_model,
        "max_rounds": settings.max_rounds,
    }


# ---------------------------------------------------------------- 示例资料

@app.get("/api/samples")
async def list_samples() -> list[dict]:
    return mock_data.get_samples()


@app.get("/api/samples/{sample_id}")
async def get_sample(sample_id: str) -> dict:
    s = mock_data.find_sample(sample_id)
    if s is None:
        raise HTTPException(404, "没有这个示例")
    return s


# ---------------------------------------------------------------- 任务

@app.get("/api/runs")
async def list_runs(request: Request, limit: int = 40) -> list[dict]:
    rows = await request.app.state.orch.ledger.list_runs(limit)
    return [
        {"global_task_id": str(r["global_task_id"]), "question": r["question"],
         "status": r["status"], "round": r["round"], "version": r["version"],
         "chunk_count": r["chunk_count"], "created_at": _iso(r.get("created_at"))}
        for r in rows
    ]


@app.post("/api/runs", status_code=201)
async def create_run(request: Request, req: RunRequest) -> dict:
    """建任务。**没有资料就直接拒绝** —— 让前端去问用户要资料。"""
    materials = [m for m in (req.materials or []) if (m or "").strip()]
    if not materials:
        raise HTTPException(400, detail={
            "error": "need_material",
            "message": "我的结论必须引用你提供的资料。请把相关的原文贴进来（可以是任何行业的内容）。",
        })
    orch = request.app.state.orch
    run = await orch.create_run(req.question.strip(), materials)
    return {"global_task_id": str(run["global_task_id"]), "chunk_count": run["chunk_count"]}


async def _run_guarded(orch, run_id: str) -> None:
    async with RUN_SEMAPHORE:
        try:
            await orch.run(run_id)
        except Exception:  # noqa: BLE001
            logger.exception("任务执行失败：%s", run_id)


@app.post("/api/runs/{run_id}/start", status_code=202)
async def start_run(request: Request, run_id: str) -> dict:
    orch = request.app.state.orch
    run = await orch.ledger.get_run(run_id)
    if run is None:
        raise HTTPException(404, "未知任务")
    if run["status"] in ("done", "failed"):
        raise HTTPException(409, f"任务已处于终态：{run['status']}")
    asyncio.create_task(_run_guarded(orch, run_id))
    return {"global_task_id": run_id, "accepted": True}


@app.get("/api/runs/{run_id}")
async def get_run(request: Request, run_id: str) -> dict:
    orch = request.app.state.orch
    run = await orch.ledger.get_run(run_id)
    if run is None:
        raise HTTPException(404, "未知任务")

    chunks = await orch.ledger.list_chunks(run_id)
    tasks = await orch.ledger.list_tasks(run_id)
    findings = await orch.ledger.list_findings(run_id)
    events = await orch.ledger.list_events(run_id)

    return {
        "run": {
            "global_task_id": str(run["global_task_id"]),
            "question": run["question"],
            "status": run["status"],
            "round": run["round"],
            "version": run["version"],
            "chunk_count": run["chunk_count"],
            "created_at": _iso(run.get("created_at")),
        },
        "chunks": [{"chunk_id": c["chunk_id"], "doc_no": c["doc_no"], "seq": c["seq"],
                    "content": c["content"], "char_len": c["char_len"]} for c in chunks],
        "tasks": [_task_json(t) for t in tasks],
        "findings": [_finding_json(f) for f in findings],
        "events": events,
    }


@app.delete("/api/runs/{run_id}", status_code=204)
async def delete_run(request: Request, run_id: str) -> None:
    await request.app.state.orch.ledger.delete_run(run_id)


# ---------------------------------------------------------------- SSE

@app.get("/api/runs/{run_id}/stream")
async def stream(request: Request, run_id: str):
    orch = request.app.state.orch
    run = await orch.ledger.get_run(run_id)
    if run is None:
        raise HTTPException(404, "未知任务")

    last_event_id = request.headers.get("last-event-id")
    try:
        after = int(last_event_id) if last_event_id else 0
    except ValueError:
        after = 0

    async def generator():
        queue = orch.bus.subscribe(run_id)
        last_seq = after
        try:
            history = await orch.ledger.list_events(run_id, after_seq=after)
            for event in history:
                last_seq = max(last_seq, event["seq"])
                yield _sse(event)
            if any(e["event_type"] == "done" for e in history):
                return
            while True:
                event = await queue.get()
                if event["seq"] <= last_seq:
                    continue
                last_seq = event["seq"]
                yield _sse(event)
                if event["event_type"] == "done":
                    break
        finally:
            orch.bus.unsubscribe(run_id, queue)

    return EventSourceResponse(generator(), ping=15)
