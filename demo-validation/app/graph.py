"""LangGraph 编排：planning → researching → answering → reviewing →（回流 / 结束）。

通用版：用户只给「问题 + 资料」，没有快照。
图的形状本身就是文章里那条链路：
  · 节点之间传的是**共享状态**（问题 + 资料引用 + 结论 + 验收），不是聊天记录；
  · 审查器的条件边让「否决」真的改变后续路由；
  · MAX_ROUNDS 兜底，防止死循环。
"""

from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager

from langgraph.graph import END, START, StateGraph

from . import agents
from .config import Settings
from .ledger import Ledger
from .llm import build_llm
from .runtime import EventBus, Runtime
from .sources import build_chunks
from .state import TaskState

logger = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.ledger = Ledger(settings.dsn)
        self.llm = build_llm(settings)
        self.bus = EventBus()
        self.rt = Runtime(settings, self.ledger, self.llm, self.bus)
        self._saver = None
        self.graph = None

    # ------------------------------------------------------------ 启动

    async def migrate(self, reset: bool = False) -> dict:
        sql = self.settings.schema_path.read_text(encoding="utf-8")
        dropped = await self.ledger.reset() if reset else []
        statements = await self.ledger.apply_schema(sql)
        return {"dropped": dropped, "statements": statements}

    def compile(self, saver=None) -> None:
        self._saver = saver
        rt = self.rt
        builder = StateGraph(TaskState)

        async def node_planner(state):
            return await agents.planner(state, rt)

        async def node_researcher(state):
            return await agents.researcher(state, rt)

        async def node_executor(state):
            return await agents.executor(state, rt)

        async def node_reviewer(state):
            return await agents.reviewer(state, rt)

        builder.add_node("planner", node_planner)
        builder.add_node("researcher", node_researcher)
        builder.add_node("executor", node_executor)
        builder.add_node("reviewer", node_reviewer)

        builder.add_edge(START, "planner")
        builder.add_edge("planner", "researcher")
        builder.add_edge("researcher", "executor")
        builder.add_edge("executor", "reviewer")
        builder.add_conditional_edges(
            "reviewer",
            self._route_after_review,
            {"researcher": "researcher", "executor": "executor", "end": END},
        )
        self.graph = builder.compile(checkpointer=saver)
        logger.info("LangGraph 已编译（checkpointer=%s）", saver is not None)

    def _route_after_review(self, state) -> str:
        review = state.get("review") or {}
        if review.get("decision") == "approve":
            return "end"
        if state.get("status") == "failed":
            return "end"
        return "executor" if (review.get("rework_target") == "executor") else "researcher"

    # ------------------------------------------------------------ 运行

    async def create_run(self, question: str, materials: list[str]) -> dict:
        chunks = build_chunks(materials)
        run_id = str(uuid.uuid4())
        run = await self.ledger.create_run(run_id, question, len(chunks))
        if chunks:
            await self.ledger.insert_chunks(run_id, chunks)
        await self.rt.emit(
            run_id, "system", "run_created",
            {"question": question, "chunk_count": len(chunks),
             "chunks": [{"chunk_id": c["chunk_id"], "doc_no": c["doc_no"],
                         "seq": c["seq"], "char_len": c["char_len"]} for c in chunks]},
            version=run["version"],
        )
        return run

    async def run(self, run_id: str) -> dict:
        run = await self.ledger.get_run(run_id)
        if run is None:
            raise KeyError(f"未知任务：{run_id}")

        state: TaskState = {
            "global_task_id": run_id,
            "question": run["question"],
            "status": "created",
            "version": run["version"],
            "round": 0,
            "sub_tasks": [],
            "findings": [],
            "answers": [],
        }
        config = {"configurable": {"thread_id": run_id}} if self._saver is not None else None

        try:
            final = (await self.graph.ainvoke(state, config)) if config else (await self.graph.ainvoke(state))
        except Exception as exc:  # noqa: BLE001
            logger.exception("图执行失败：%s", run_id)
            message = str(exc)[:500]
            await self.ledger.set_run_status(run_id, "failed")
            await self.rt.emit(run_id, "system", "error", {"where": "graph", "message": message})
            await self.rt.emit(run_id, "system", "done",
                               {"status": "failed", "round": 0, "reason": message,
                                "consistency_passed": False})
            raise

        status = final.get("status", "done")
        version = final.get("version", 0)
        round_no = final.get("round", 0)
        consistency = final.get("consistency") or {}
        reason = (f"达到最大轮次 {self.settings.max_rounds}，仍未通过一致性校验"
                  if status == "failed" else "审查通过，且一致性校验全部 pass")

        await self.rt.emit(run_id, "system", "done",
                           {"status": status, "reason": reason, "round": round_no,
                            "consistency_passed": bool(consistency.get("passed"))},
                           version=version, round_no=round_no)
        return final

    async def close(self) -> None:
        await self.ledger.close()


@asynccontextmanager
async def build_orchestrator(settings: Settings, reset: bool = False):
    """打开账本 → 建表 → 挂 checkpoint → 编译图。"""
    orch = Orchestrator(settings)
    await orch.ledger.open()
    info = await orch.migrate(reset=reset)
    logger.info("数据库就绪：%s 条语句%s", info["statements"],
                f"（已重建，丢弃 {len(info['dropped'])} 张旧表）" if info["dropped"] else "")

    saver_cm = None
    saver = None
    if settings.use_checkpointer:
        try:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

            saver_cm = AsyncPostgresSaver.from_conn_string(settings.dsn)
            saver = await saver_cm.__aenter__()
            await saver.setup()
            logger.info("LangGraph Postgres checkpoint 已就绪")
        except Exception as exc:  # noqa: BLE001
            logger.warning("checkpoint 初始化失败，降级为无 checkpoint 运行：%s", exc)
            saver = None
            if saver_cm is not None:
                try:
                    await saver_cm.__aexit__(None, None, None)
                except Exception:  # noqa: BLE001
                    pass
                saver_cm = None

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
