"""FastAPI 服务：REST + SSE + 静态单页。

关键接口：
    POST /api/runs                建任务（自然语言问题）
    POST /api/runs/{id}/start     开始跑
    POST /api/runs/{id}/confirm   人工确认（删除类操作暂停后由它放行/取消）
    GET  /api/runs/{id}/stream    SSE：先从账本回放，再推实时
    GET  /api/runs/{id}/usage     本轮大模型消耗明细（每次调用的 token / 耗时）
    GET  /api/usage               全部会话的累计消耗 + 按会话排名

两库隔离在这个文件里体现为：账本走 agent_sql，执行走 cs_v1 的受限角色。
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from . import catalog, examples
from .config import BASE_DIR, get_settings
from .graph import build_orchestrator

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("sql_agent.server")

settings = get_settings()
WEB_DIR = BASE_DIR / "web"
RUN_SEMAPHORE = asyncio.Semaphore(2)


class UTF8JSONResponse(JSONResponse):
    """明确写出 `charset=utf-8`。

    响应里全是中文，而**Windows PowerShell 的 `Invoke-RestMethod` 在
    `Content-Type` 没带 charset 时按 ISO-8859-1 解码** —— 于是你用命令行
    拉一次 `/api/runs`，看到的是 `æ¥çæå¤å°åå`。
    数据一直是好的（UTF-8 存、UTF-8 取），坏的是客户端的猜测；
    但把 charset 写出来能让文档里那条 curl / PowerShell 例子真的可用。
    """

    media_type = "application/json; charset=utf-8"


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with build_orchestrator(settings) as orch:
        app.state.orch = orch
        yield


app = FastAPI(title="sql-agent · 多智能体自然语言转 SQL", version="0.1.0",
              lifespan=lifespan, default_response_class=UTF8JSONResponse)


class RunRequest(BaseModel):
    question: str = Field(min_length=1, description="用户的自然语言问题")
    conversation_id: str | None = Field(
        default=None,
        description="在同一个会话里追问时传它（多轮对话）；不传就是新会话。",
    )


class ConfirmRequest(BaseModel):
    approve: bool = True
    by: str = "用户"


# ---------------------------------------------------------------- 工具

def _iso(v):
    """时间转字符串 —— **幂等**：已经是字符串就原样返回。

    账本里不同方法返回的时间形状不一致：`list_tasks` / `list_audit` 给的是
    datetime，而 `list_events` / `list_memory` / `list_llm_calls` 已经在账本层
    转成 isoformat 字符串了（因为它们要进 state 与事件流）。
    序列化层不该去猜是哪种 —— 猜错就是 `'str' object has no attribute 'isoformat'`，
    而且**只有真有数据的时候才会触发**（空列表时一路无事，正好骗过冒烟测试）。
    """
    if v is None or isinstance(v, str):
        return v
    return v.isoformat() if hasattr(v, "isoformat") else v


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
    info["catalog"] = catalog.info()
    return info


class ReloadRequest(BaseModel):
    path: str | None = Field(default=None, description="留空则重新读当前那份文件")


@app.post("/api/catalog/reload")
async def reload_catalog(request: Request, req: ReloadRequest | None = None) -> dict:
    """运行时换一套表 —— 表信息是动态的，不该要求重启服务。

    换完立刻生效：白名单、生成器的两层表结构、校验器的表/字段核对全都跟着变。
    """
    path = (req.path if req else None) or request.app.state.orch.settings.tables_file or None
    try:
        catalog.reload(path)
    except catalog.CatalogError as exc:
        raise HTTPException(400, str(exc)) from exc
    # 注意别写成 {"ok": True, **catalog.info()} —— info() 里也有一个 ok，
    # 后者会把前者覆盖掉（dict 字面量里重复键不报错，只会悄悄生效）
    return {**catalog.info(), "reloaded": True}


@app.get("/api/samples")
async def list_samples() -> list[dict]:
    return examples.get_samples()


# ---------------------------------------------------------------- 大模型消耗

def _call_json(row: dict) -> dict:
    return {
        "call_id": row["call_id"], "role": row["role"], "stage": row.get("stage"),
        "round": row.get("round"), "cursor": row.get("cursor"), "attempt": row.get("attempt"),
        "model": row.get("model"),
        "prompt_tokens": row.get("prompt_tokens") or 0,
        "completion_tokens": row.get("completion_tokens") or 0,
        "total_tokens": row.get("total_tokens") or 0,
        "cached_tokens": row.get("cached_tokens") or 0,
        "reasoning_tokens": row.get("reasoning_tokens") or 0,
        "duration_ms": row.get("duration_ms"), "ok": row.get("ok"),
        "error": row.get("error"), "created_at": _iso(row.get("created_at")),
    }


@app.get("/api/runs/{run_id}/usage")
async def run_usage(request: Request, run_id: str) -> dict:
    """本轮消耗：每次调用的明细 + 按角色/步骤分组的汇总。

    数字来自 `llm_call` 表（token 用量是响应里带回来的真数）。
    离线 mock 模式没有大模型调用 —— 如实说明，而不是显示一堆 0 装成"很省"。
    """
    orch = request.app.state.orch
    run = await orch.ledger.get_run(run_id)
    if run is None:
        raise HTTPException(404, "未知任务")
    calls = await orch.ledger.list_llm_calls(run_id)
    summary = await orch.ledger.usage_summary(run_id)
    return {"global_task_id": run_id, "llm_mode": settings.llm_mode,
            "model": settings.deepseek_model,
            "llm_enabled": orch.rt.llm_enabled,
            "calls": [_call_json(c) for c in calls], **summary}


@app.get("/api/usage")
async def all_usage(request: Request, limit: int = 30) -> dict:
    """全部会话的累计消耗 + 按会话排名 —— 「消耗清单」看整体时用。"""
    orch = request.app.state.orch
    summary = await orch.ledger.usage_summary()
    convs = await orch.ledger.usage_by_conversation(limit)
    for c in convs:
        if c.get("conversation_id") is not None:
            c["conversation_id"] = str(c["conversation_id"])
        if c.get("last_at") is not None:
            c["last_at"] = c["last_at"].isoformat()
    return {"llm_mode": settings.llm_mode, "model": settings.deepseek_model,
            "llm_enabled": orch.rt.llm_enabled,
            "by_conversation": convs, **summary}


# ---------------------------------------------------------------- 任务

@app.get("/api/runs")
async def list_runs(request: Request, limit: int = 40) -> list[dict]:
    """会话列表：**一个会话一条**（多轮提问合并），按最后活动时间排序。"""
    rows = await request.app.state.orch.ledger.list_runs(limit)
    return [{"conv_id": str(r["conv_id"]),
             "global_task_id": str(r["global_task_id"]),
             "question": r["question"], "status": r["status"],
             "turn": int(r.get("turn") or 1), "turns": int(r.get("turns") or 1),
             "reads": r.get("reads", 0), "writes": r.get("writes", 0),
             "affected": r.get("affected", 0), "write_tables": r.get("write_tables") or [],
             "llm_calls": int(r.get("llm_calls") or 0),
             "llm_tokens": int(r.get("llm_tokens") or 0),
             "created_at": _iso(r.get("created_at")), "updated_at": _iso(r.get("updated_at"))}
            for r in rows]


@app.post("/api/runs", status_code=201)
async def create_run(request: Request, req: RunRequest) -> dict:
    run = await request.app.state.orch.create_run(req.question.strip(),
                                                  conversation_id=req.conversation_id)
    return {"global_task_id": str(run["global_task_id"]),
            "conversation_id": str(run["conversation_id"]),
            "turn": int(run["turn"])}


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
                "conversation_id": str(run.get("conversation_id") or run["global_task_id"]),
                "turn": int(run.get("turn") or 1),
                "created_at": _iso(run.get("created_at"))},
        "tasks": [_task_json(t) for t in tasks],
        "audit": [_audit_json(a) for a in audits],
        "events": events,
    }


@app.get("/api/conversations/{conversation_id}")
async def get_conversation(request: Request, conversation_id: str) -> dict:
    """一个会话的全部轮次 —— 前端刷新后据此重建整条多轮对话。"""
    orch = request.app.state.orch
    turns = await orch.ledger.list_turns(conversation_id)
    if not turns:
        raise HTTPException(404, "未知会话")
    out = []
    for t in turns:
        rid = str(t["global_task_id"])
        out.append({
            "global_task_id": rid, "question": t["question"], "status": t["status"],
            "turn": int(t.get("turn") or 1), "created_at": _iso(t.get("created_at")),
            "events": await orch.ledger.list_events(rid),
            "audit": [_audit_json(a) for a in await orch.ledger.list_audit(rid)],
        })
    return {"conversation_id": conversation_id, "turns": out}


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
