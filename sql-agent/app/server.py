"""FastAPI 服务：REST + SSE + 静态单页。

关键接口：
    POST /api/runs                建任务（自然语言问题）
    POST /api/runs/{id}/start     开始跑
    POST /api/runs/{id}/confirm   人工确认（删除类操作暂停后由它放行/取消）
    GET  /api/runs/{id}/stream    SSE：先从账本回放，再推实时

两库隔离在这个文件里体现为：账本走 agent_sql，执行走 cs_v1 的受限角色。
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

from . import examples
from .config import BASE_DIR, get_settings
from .graph import build_orchestrator

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("sql_agent.server")

settings = get_settings()
WEB_DIR = BASE_DIR / "web"
RUN_SEMAPHORE = asyncio.Semaphore(2)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with build_orchestrator(settings) as orch:
        app.state.orch = orch
        yield


app = FastAPI(title="sql-agent · 多智能体自然语言转 SQL", version="0.1.0", lifespan=lifespan)


class RunRequest(BaseModel):
    question: str = Field(min_length=1, description="用户的自然语言问题")


class ConfirmRequest(BaseModel):
    approve: bool = True
    by: str = "用户"


# ---------------------------------------------------------------- 工具

def _iso(v):
    return v.isoformat() if v is not None else None


def _task_json(row: dict) -> dict:
    return {
        "sub_task_id": row["sub_task_id"], "role": row["role"], "agent_id": row["agent_id"],
        "attempt": row["attempt"], "status": row["status"], "version": row["version"],
        "lease_until": _iso(row.get("lease_until")),
        "input_context": row.get("input_context"),
        "result_ref": row.get("result_ref"),
        "idempotency_key": row.get("idempotency_key"),
        "error": row.get("error"), "created_at": _iso(row.get("created_at")),
    }


def _audit_json(row: dict) -> dict:
    return {
        "sql_id": row["sql_id"], "round": row["round"], "sub_task_id": row["sub_task_id"],
        "stage": row["stage"], "sql": row["sql_text"], "sql_hash": row["sql_hash"],
        "intent": row.get("intent"), "risk_level": row.get("risk_level"),
        "action": row.get("action"), "tables": row.get("tables") or [],
        "est_rows": row.get("est_rows"), "affected_rows": row.get("affected_rows"),
        "need_confirm": row.get("need_confirm"), "confirmed_by": row.get("confirmed_by"),
        "confirmed_at": _iso(row.get("confirmed_at")), "error": row.get("error"),
        "duration_ms": row.get("duration_ms"), "created_at": _iso(row.get("created_at")),
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
    info: dict = {
        "llm_mode": settings.llm_mode,
        "model": settings.deepseek_model,
        "max_rounds": settings.max_rounds,
        "checkpointer": orch._saver is not None,
        "ledger_db": settings.pg_db_ledger,
        "biz_db": settings.pg_db_biz,
        "runner_user": settings.runner_user,
        "limits": {"max_rows": settings.max_rows,
                   "statement_timeout_ms": settings.statement_timeout_ms,
                   "confirm_row_threshold": settings.confirm_row_threshold},
    }
    try:
        info["ledger"] = "ok" if await orch.ledger.ping() else "error"
    except Exception:  # noqa: BLE001
        info["ledger"] = "error"
    try:
        identity = await orch.db.runner_identity()
        info["runner"] = "ok"
        info["runner_privileges"] = identity.get("privileges") or []
    except Exception as exc:  # noqa: BLE001
        info["runner"] = f"error: {exc}"
    info["tables"] = sorted(orch.rt.allowed_tables)
    return info


@app.get("/api/schema")
async def get_schema(request: Request) -> dict:
    orch = request.app.state.orch
    await orch.rt.refresh_schema()
    return {"text": orch.rt.schema_text,
            "tables": orch.rt.schema.get("tables") or {},
            "allowed": sorted(orch.rt.allowed_tables)}


@app.get("/api/samples")
async def list_samples() -> list[dict]:
    return examples.get_samples()


# ---------------------------------------------------------------- 任务

@app.get("/api/runs")
async def list_runs(request: Request, limit: int = 40) -> list[dict]:
    rows = await request.app.state.orch.ledger.list_runs(limit)
    return [{"global_task_id": str(r["global_task_id"]), "question": r["question"],
             "status": r["status"], "round": r["round"], "version": r["version"],
             "created_at": _iso(r.get("created_at"))} for r in rows]


@app.post("/api/runs", status_code=201)
async def create_run(request: Request, req: RunRequest) -> dict:
    run = await request.app.state.orch.create_run(req.question.strip())
    return {"global_task_id": str(run["global_task_id"])}


async def _guarded(orch, coro_factory, label: str) -> None:
    async with RUN_SEMAPHORE:
        try:
            await coro_factory()
        except Exception:  # noqa: BLE001
            logger.exception("%s 失败", label)


@app.post("/api/runs/{run_id}/start", status_code=202)
async def start_run(request: Request, run_id: str) -> dict:
    orch = request.app.state.orch
    run = await orch.ledger.get_run(run_id)
    if run is None:
        raise HTTPException(404, "未知任务")
    if run["status"] in ("done", "failed"):
        raise HTTPException(409, f"任务已处于终态：{run['status']}")
    asyncio.create_task(_guarded(orch, lambda: orch.run(run_id), f"任务 {run_id}"))
    return {"global_task_id": run_id, "accepted": True}


@app.post("/api/runs/{run_id}/confirm", status_code=202)
async def confirm_run(request: Request, run_id: str, req: ConfirmRequest) -> dict:
    """人工协作入口：删除类操作暂停后，由它把用户的决定送回去。"""
    orch = request.app.state.orch
    run = await orch.ledger.get_run(run_id)
    if run is None:
        raise HTTPException(404, "未知任务")
    if run["status"] != "confirming":
        raise HTTPException(409, f"任务当前不在等待确认（状态：{run['status']}）")
    if orch._saver is None:
        raise HTTPException(409, "人工确认依赖 checkpointer，请把 USE_CHECKPOINTER 设为 on")
    asyncio.create_task(_guarded(
        orch, lambda: orch.resume(run_id, req.approve, req.by), f"确认 {run_id}"))
    return {"global_task_id": run_id, "approve": req.approve, "by": req.by}


@app.get("/api/runs/{run_id}")
async def get_run(request: Request, run_id: str) -> dict:
    orch = request.app.state.orch
    run = await orch.ledger.get_run(run_id)
    if run is None:
        raise HTTPException(404, "未知任务")
    tasks = await orch.ledger.list_tasks(run_id)
    audits = await orch.ledger.list_audit(run_id)
    events = await orch.ledger.list_events(run_id)
    return {
        "run": {"global_task_id": str(run["global_task_id"]), "question": run["question"],
                "status": run["status"], "round": run["round"], "version": run["version"],
                "created_at": _iso(run.get("created_at"))},
        "tasks": [_task_json(t) for t in tasks],
        "audit": [_audit_json(a) for a in audits],
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
