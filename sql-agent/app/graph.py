"""LangGraph 编排：**链路不固定**，由 router 节点动态决定下一步。

节点：planner / generator / validator / executor / fixer / reviewer ＋ router

    START → planner → router ⇄ {generator, validator, executor, fixer}
                                   router → reviewer → END

所有智能体都走回 router，由它根据当前状态算出**能去哪**，
再在合法范围内采纳模型自己建议的 `handoff_to`。
所以简单问题一条 SQL 直达，复杂问题可能反复修正、甚至退回重新规划。

人工确认（需删除/全表更新时）用 LangGraph 原生 `interrupt`：
图会在 executor 里暂停，等前端把用户的决定 `Command(resume=...)` 送回来。
"""

from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from . import agents, router
from .config import Settings
from .db import BizDatabase
from .ledger import Ledger, split_statements
from .llm import build_llm
from .runtime import EventBus, Runtime
from .state import TaskState

logger = logging.getLogger(__name__)

AGENT_NODES = ("planner", "generator", "validator", "executor", "fixer", "reviewer")


def _with_config(fn):
    """在节点里手动补上 runnable 上下文。

    为什么需要：LangGraph 给异步节点注入 config 靠的是
    `asyncio.create_task(coro, context=ctx)`，而 `context=` 参数是 **Python 3.11+** 才有的
    （见 langgraph/_internal/_runnable.py 的 ASYNCIO_ACCEPTS_CONTEXT 分支）。
    在 3.10 上它会走 `ret = await self.afunc(...)`，于是节点里的
    `interrupt()` / `get_config()` 会报 "Called get_config outside of a runnable context"。

    这里让节点多接一个 config 参数并自己 set 一下，效果等价于 3.11+ 的默认行为。
    """
    async def wrapper(state, config):
        from langchain_core.runnables.config import var_child_runnable_config

        token = var_child_runnable_config.set(config)
        try:
            return await fn(state)
        finally:
            var_child_runnable_config.reset(token)

    return wrapper


class Orchestrator:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.ledger = Ledger(settings.ledger_dsn)
        self.db = BizDatabase(settings.biz_admin_dsn, settings.biz_runner_dsn,
                              settings.statement_timeout_ms)
        self.llm = build_llm(settings)
        self.bus = EventBus()
        self.rt = Runtime(settings, self.ledger, self.db, self.llm, self.bus)
        self._saver = None
        self.graph = None

    # ------------------------------------------------------------ 启动

    async def migrate(self, reset: bool = False) -> dict:
        """建两边的表：agent_sql 的系统表 + cs_v1 的业务表（含授权）。"""
        await self.db.open()
        dropped = await self.ledger.reset() if reset else []

        ledger_sql = self.settings.ledger_schema_path.read_text(encoding="utf-8")
        n_ledger = await self.ledger.apply_schema(ledger_sql)

        biz_sql = self.settings.biz_schema_path.read_text(encoding="utf-8")
        n_biz = await self.db.apply_script(biz_sql, split_statements)

        await self.rt.refresh_schema()
        return {"dropped": dropped, "ledger_statements": n_ledger,
                "biz_statements": n_biz, "tables": sorted(self.rt.allowed_tables)}

    def compile(self, saver=None) -> None:
        self._saver = saver
        rt = self.rt
        builder = StateGraph(TaskState)

        async def node_planner(state):
            return await agents.planner(state, rt)

        async def node_generator(state):
            return await agents.generator(state, rt)

        async def node_validator(state):
            return await agents.validator(state, rt)

        async def node_executor(state):
            return await agents.executor(state, rt)

        async def node_fixer(state):
            return await agents.fixer(state, rt)

        async def node_reviewer(state):
            return await agents.reviewer(state, rt)

        async def node_router(state):
            """路由节点：模型建议 + 运行时候选集 → 实际去哪。"""
            run_id = state["global_task_id"]
            want = state.get("next_agent")
            nxt, reason = router.decide(state, rt.settings.max_rounds)
            await rt.emit(
                run_id, "router", "handoff",
                {"from": state.get("last_agent"), "to": nxt,
                 "reason": reason, "model_wanted": want,
                 "overridden": bool(want and want != nxt)},
                version=state.get("version", 0), round_no=state.get("round", 0))
            return {"route": nxt, "route_reason": reason}

        builder.add_node("planner", _with_config(node_planner))
        builder.add_node("generator", _with_config(node_generator))
        builder.add_node("validator", _with_config(node_validator))
        builder.add_node("executor", _with_config(node_executor))
        builder.add_node("fixer", _with_config(node_fixer))
        builder.add_node("reviewer", _with_config(node_reviewer))
        builder.add_node("router", node_router)

        builder.add_edge(START, "planner")
        for name in ("planner", "generator", "validator", "executor", "fixer"):
            builder.add_edge(name, "router")
        builder.add_edge("reviewer", END)
        builder.add_conditional_edges("router", lambda s: s.get("route") or "reviewer", {
            "planner": "planner", "generator": "generator", "validator": "validator",
            "executor": "executor", "fixer": "fixer", "reviewer": "reviewer",
            "done": END,
        })
        self.graph = builder.compile(checkpointer=saver)
        logger.info("LangGraph 已编译（checkpointer=%s）", saver is not None)

    # ------------------------------------------------------------ 运行

    async def create_run(self, question: str) -> dict:
        run_id = str(uuid.uuid4())
        run = await self.ledger.create_run(run_id, question, self.settings.max_rounds)
        await self.rt.emit(run_id, "system", "run_created",
                           {"question": question, "tables": sorted(self.rt.allowed_tables)},
                           version=run["version"])
        return run

    def _config(self, run_id: str) -> dict:
        return {"configurable": {"thread_id": run_id}}

    async def _drive(self, run_id: str, payload) -> bool:
        """推进图。返回 True 表示「停在人工确认」。"""
        paused = False
        async for chunk in self.graph.astream(payload, self._config(run_id), stream_mode="updates"):
            interrupts = chunk.get("__interrupt__")
            if interrupts:
                paused = True
                for item in interrupts:
                    await self._on_interrupt(run_id, getattr(item, "value", item) or {})
        return paused

    async def _on_interrupt(self, run_id: str, value: dict) -> None:
        """把「等待人工确认」落账 —— 前端据此渲染确认按钮。"""
        run = await self.ledger.get_run(run_id)
        round_no = int((run or {}).get("round") or 0)
        await self.rt.emit(run_id, "executor", "await_confirm",
                           value, version=(run or {}).get("version", 0), round_no=round_no)
        await self.ledger.audit_sql(
            run_id, round_no, value.get("sub_task_id") or "", "await_confirm",
            value.get("sql") or "", "",
            intent=value.get("intent"), risk_level=value.get("risk_level"),
            action=value.get("action"), tables=value.get("tables") or [],
            est_rows=value.get("est_rows"), need_confirm=True)
        await self.ledger.set_run_status(run_id, "confirming")

    async def run(self, run_id: str) -> dict:
        run = await self.ledger.get_run(run_id)
        if run is None:
            raise KeyError(f"未知任务：{run_id}")
        state: TaskState = {
            "global_task_id": run_id,
            "question": run["question"],
            "schema_text": self.rt.schema_text,
            "status": "created", "version": run["version"], "round": 0,
            "intents": [], "cursor": 0, "history": [],
            "draft": None, "verdict": None, "checks": None, "result": None,
            "confirmed": False,
        }
        try:
            paused = await self._drive(run_id, state)
        except Exception as exc:  # noqa: BLE001
            logger.exception("图执行失败：%s", run_id)
            await self._fail(run_id, str(exc))
            raise
        if paused:
            return {"status": "confirming"}
        return await self._finish(run_id)

    async def resume(self, run_id: str, approve: bool, by: str) -> dict:
        """用户点了「确认执行 / 取消」之后，把决定送回图里继续跑。"""
        if self._saver is None:
            raise RuntimeError("人工确认依赖 checkpointer，请把 USE_CHECKPOINTER 设为 on")
        snapshot = await self.graph.aget_state(self._config(run_id))
        if not snapshot.next:
            raise RuntimeError("这个任务当前没有在等待确认")
        await self.rt.emit(run_id, "system", "confirmed",
                           {"approve": approve, "by": by})
        await self.ledger.set_run_status(run_id, "executing")
        try:
            paused = await self._drive(run_id, Command(resume={"approve": approve, "by": by}))
        except Exception as exc:  # noqa: BLE001
            logger.exception("恢复执行失败：%s", run_id)
            await self._fail(run_id, str(exc))
            raise
        if paused:
            return {"status": "confirming"}
        return await self._finish(run_id)

    async def _finish(self, run_id: str) -> dict:
        config = self._config(run_id)
        values = (await self.graph.aget_state(config)).values or {}
        status = values.get("status", "done")
        version = values.get("version", 0)
        report = values.get("consistency") or {}
        reason = ("审查通过，且运行时一致性校验全部 pass" if status == "done"
                  else "存在未通过的一致性问题，已如实说明")
        await self.rt.emit(run_id, "system", "done",
                           {"status": status, "reason": reason,
                            "round": values.get("round", 0),
                            "consistency_passed": bool(
                                (values.get("last_consistency") or {}).get("passed", status == "done"))},
                           version=version, round_no=values.get("round", 0))
        return values

    async def _fail(self, run_id: str, message: str) -> None:
        await self.ledger.set_run_status(run_id, "failed")
        await self.rt.emit(run_id, "system", "error", {"where": "graph", "message": message[:500]})
        await self.rt.emit(run_id, "system", "done",
                           {"status": "failed", "reason": message[:300],
                            "round": 0, "consistency_passed": False})

    async def close(self) -> None:
        await self.db.close()
        await self.ledger.close()


@asynccontextmanager
async def build_orchestrator(settings: Settings, reset: bool = False):
    orch = Orchestrator(settings)
    await orch.ledger.open()
    info = await orch.migrate(reset=reset)
    logger.info("数据库就绪：账本 %s 条语句 · 业务库 %s 条语句 · 可访问表 %s",
                info["ledger_statements"], info["biz_statements"], info["tables"])

    saver_cm = None
    saver = None
    if settings.use_checkpointer:
        try:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
            saver_cm = AsyncPostgresSaver.from_conn_string(settings.ledger_dsn)
            saver = await saver_cm.__aenter__()
            await saver.setup()
            logger.info("LangGraph Postgres checkpoint 已就绪（agent_sql）")
        except Exception as exc:  # noqa: BLE001
            logger.warning("checkpoint 初始化失败，人工确认将不可用：%s", exc)
            saver = None
            if saver_cm is not None:
                try:
                    await saver_cm.__aexit__(None, None, None)
                except Exception:  # noqa: BLE001
                    pass
                saver_cm = None
    else:
        logger.warning("USE_CHECKPOINTER=off：需人工确认的 SQL 将无法执行")

    orch.compile(saver)
    try:
        yield orch
    finally:
        if saver_cm is not None:
            try:
                await saver_cm.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
        await orch.close()
