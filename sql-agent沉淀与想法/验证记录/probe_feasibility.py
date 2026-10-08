"""离线可行性探针：调用实际项目函数，DB/LLM/账本使用可控替身。

PASS 表示当前观察与断言相符；GAP 表示问题被复现，绝不表示问题已修复。
运行：在具备 sql-agent 依赖的 Python 中执行本文件，不连接真实数据库或模型。
"""
from __future__ import annotations

import asyncio
import copy
import importlib.metadata
import inspect
import json
import sys
import faulthandler
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sql-agent"))
faulthandler.dump_traceback_later(30, repeat=False)

from app import agents, catalog, catalog_check, router, sql_guard, validators
from app.graph import Orchestrator
from app.runtime import usage_of
from app.state import ExecutorOutput, FixerOutput, PlannerOutput, QueryIntent, SqlDraft, TaskState, ValidatorOutput
from langgraph.errors import InvalidUpdateError
from langgraph.graph import END, START, StateGraph

OBSERVATIONS = []
RUN_ID = "11111111-1111-1111-1111-111111111111"


def observe(code, kind, condition, evidence):
    assert condition, f"Unexpected result for {code}: {evidence}"
    OBSERVATIONS.append({"id": code, "kind": kind, "evidence": evidence})
    print(f"PASS [{kind}] {code}: {json.dumps(evidence, ensure_ascii=False)}")


def base_state(cursor=0):
    return {
        "global_task_id": RUN_ID, "question": "查询商品", "mode": "execute",
        "round": 0, "version": 1, "cursor": cursor,
        "intents": [{"sub_task_id": "st-1", "intent": "查询商品", "kind": "查询"},
                    {"sub_task_id": "st-2", "intent": "查询商品", "kind": "查询"}],
        "draft": {"sub_task_id": f"st-{cursor + 1}", "intent": "查询商品",
                  "sql": "SELECT id FROM products LIMIT 60", "tables": ["products"], "round": 0},
        "verdict": {"level": "只读", "action": "SELECT", "tables": ["products"], "needs_confirm": False},
        "checks": {"passed": True, "checks": []}, "history": [], "memory": [], "attempts": [],
    }


class FakeLedger:
    def __init__(self):
        self.opened = []
        self.finished = []
        self.memories = []
        self.audit = []

    async def open_task(self, **kw):
        self.opened.append(kw)
        return kw

    async def finish_task(self, *args, **kw):
        self.finished.append((args[1], args[2]))
        return {}

    async def audit_sql(self, *args, **kw):
        self.audit.append((args, kw))
        return len(self.audit)

    async def set_run_status(self, *args, **kw):
        return 2

    async def save_memory(self, *args, **kw):
        self.memories.append({"sub_task_id": args[1], "claim": args[2], **kw})
        return 1

    async def list_memory(self, *args, **kw):
        return self.memories


class FakeDB:
    def __init__(self):
        self.exec_calls = 0

    async def explain(self, sql):
        return {"ok": True, "est_rows": 50000}

    async def execute(self, sql, **kw):
        self.exec_calls += 1
        return {"ok": True, "kind": "rows", "columns": ["id"],
                "rows": [[i] for i in range(60)], "rowcount": 60,
                "truncated": True, "duration_ms": 1}


class FakeRT:
    def __init__(self):
        self.ledger, self.db = FakeLedger(), FakeDB()
        self.allowed_tables = catalog.allowed_tables()
        self.llm = None
        self.events = []
        self.settings = SimpleNamespace(lease_seconds=300, max_rows=500, confirm_row_threshold=1000)

    async def emit(self, *args, **kw):
        self.events.append((args, kw))


def consistency(history, tasks=None):
    return validators.validate(
        allowed_tables={"products"}, intents=[{"sub_task_id": "st-1"}],
        history=history, tasks=tasks or [{"idempotency_key": "k"}],
        version=1, round_no=0, mode="execute",
    )


async def main():
    # 1. 真实静态检查：带副作用 CTE 被顶层 SELECT 风险分级漏掉。
    cte_sql = "WITH removed AS (DELETE FROM products WHERE id = 1 RETURNING id) SELECT id FROM removed"
    verdict = sql_guard.analyze(cte_sql, {"products"})
    cat_ok, _, _ = catalog_check.check_sql(cte_sql)
    observe("G01_CTE_DELETE_READONLY", "GAP", verdict.level == "只读" and cat_ok,
            {"level": verdict.level, "action": verdict.action, "needs_confirm": verdict.needs_confirm,
             "catalog_passed": cat_ok, "sql": cte_sql})

    # 2. 真实节点走到执行替身，确认函数未被调用；不声称真实库已发生删除。
    rt, state = FakeRT(), base_state()
    state["draft"]["sql"] = cte_sql
    state["verdict"] = {"level": verdict.level, "action": verdict.action,
                        "tables": verdict.tables, "needs_confirm": verdict.needs_confirm}
    validation = await agents.validator(state, rt)
    state.update(validation)
    with patch.object(agents, "interrupt", side_effect=AssertionError("unexpected confirmation")) as prompt:
        await agents.executor(state, rt)
    observe("G02_CTE_REACHES_FAKE_EXECUTION", "GAP", validation["checks"]["passed"] and rt.db.exec_calls == 1 and prompt.call_count == 0,
            {"mock_semantic_validation": True, "fake_execute_calls": rt.db.exec_calls, "confirmation_calls": prompt.call_count})

    # 3. 程序一致性没有拦住 exec_ok=False。
    history = [{"sub_task_id": "st-1", "intent": "查询商品", "tables": ["products"],
                "validated": True, "validated_passed": True, "executed": True,
                "exec_ok": False, "risk_level": "只读", "need_confirm": False}]
    report = consistency(history)
    observe("G03_FAILED_EXECUTION_PASSES_REPORT", "GAP", report.passed,
            {"exec_ok": False, "consistency_passed": report.passed})
    unpassed = copy.deepcopy(history)
    unpassed[0]["validated_passed"] = False
    unpassed[0]["exec_ok"] = True
    observe("G04_VALIDATION_FLAG_TOO_WEAK", "GAP", consistency(unpassed).passed,
            {"validated": True, "validated_passed": False, "report_passed": True, "note": "synthetic corrupted history; router separately guards normal flow"})

    # 4. 真实取消分支把没有执行的动作记为成功执行。
    rt, state = FakeRT(), base_state()
    state["verdict"].update(level="需确认", needs_confirm=True, action="DELETE")
    state["draft"]["sql"] = "DELETE FROM products WHERE id=1"
    state.update(await agents.validator(state, rt))
    with patch.object(agents, "interrupt", return_value={"approve": False, "by": "用户"}):
        cancelled = await agents.executor(state, rt)
    h = cancelled["history"][0]
    report = consistency(cancelled["history"])
    observe("G05_CANCELLED_RECORDED_EXECUTED", "GAP",
            h["executed"] and h["exec_ok"] and report.passed and rt.db.exec_calls == 0,
            {"kind": h["kind"], "executed": h["executed"], "exec_ok": h["exec_ok"],
             "report_passed": report.passed, "actual_fake_execute_calls": rt.db.exec_calls})

    # 5. 节点真正完成时传给账本的 ID 与认领 ID 不一致。
    rt, state = FakeRT(), base_state(cursor=1)
    async def plan_call(*args, **kw):
        return PlannerOutput(understanding="继续", mode="execute", plan_action="continue", intents=[]), 1, None
    with patch.object(agents, "_call", side_effect=plan_call):
        await agents.planner(state, rt)
    opened, finished = rt.ledger.opened[0]["sub_task_id"], rt.ledger.finished[-1][0]
    observe("G06_PLANNER_LEDGER_ID_MISMATCH", "GAP", opened != finished,
            {"opened": opened, "finished": finished})
    rt, state = FakeRT(), base_state()
    async def fix_call(*args, **kw):
        return FixerOutput(draft=SqlDraft(sub_task_id="st-1", intent="查询商品", sql="SELECT id FROM products LIMIT 10"), what_changed="增加限制"), 1, None
    with patch.object(agents, "_call", side_effect=fix_call):
        fixed = await agents.fixer(state, rt)
    opened, finished = rt.ledger.opened[0]["sub_task_id"], rt.ledger.finished[-1][0]
    observe("G07_FIXER_LEDGER_ID_MISMATCH", "GAP", opened != finished,
            {"opened": opened, "finished": finished, "returned_round": fixed["round"]})

    # 6. 实际记忆写入丢失结果截断状态，且摘要被标作 verified。
    rt, state = FakeRT(), base_state()
    async def summary_call(*args, **kw):
        return ExecutorOutput(ok=True, summary="模型声称这就是全部商品"), 1, None
    with patch.object(agents, "_call", side_effect=summary_call):
        await agents.executor(state, rt)
    mem = rt.ledger.memories[0]
    observe("G08_MEMORY_LOSES_COMPLETENESS", "GAP", "truncated" not in mem and len(mem["rows"]) == 50,
            {"source_truncated": True, "memory_has_truncated": "truncated" in mem,
             "memory_rows": len(mem["rows"]), "entity_ids": len(mem["entities"]["values"])})
    observe("G09_MODEL_CLAIM_MARKED_VERIFIED", "GAP", "模型声称" in mem["claim"] and mem["verified"],
            {"claim": mem["claim"], "verified": mem["verified"]})

    # 7. 重复计数不是连续窗口，且不按 task_id 过滤。
    state = base_state()
    state["checks"] = {"passed": False}
    state["attempts"] = [{"ok": False, "error_sig": x, "sql_hash": "different", "sub_task_id": "other"}
                         for x in ["A", "B", "A"]]
    dest, reason = router.decide(state, 3)
    observe("G10_NONCONSECUTIVE_OTHER_TASK_ERRORS", "GAP", dest == "reviewer",
            {"error_sequence": ["A", "B", "A"], "task_ids": ["other"], "destination": dest, "reason": reason})

    # 8. 依赖 schema 没有图级验证，缺失依赖没有让 router 阻塞。
    self_dep = QueryIntent(sub_task_id="st-1", intent="查询商品", kind="查询", depends_on=["st-1"])
    state = base_state()
    state["draft"] = None
    state["intents"][0]["depends_on"] = ["missing"]
    dest, _ = router.decide(state, 3)
    observe("G11_DEPENDENCY_NOT_A_GATE", "GAP", self_dep.depends_on == ["st-1"] and dest == "generator",
            {"self_dependency_schema_accepted": True, "missing_dependency_destination": dest})

    # 9. LangGraph 当前 LastValue 状态不能接收两个并行草稿。
    builder = StateGraph(TaskState)
    builder.add_node("a", lambda s: {"draft": {"sql": "A"}})
    builder.add_node("b", lambda s: {"draft": {"sql": "B"}})
    for node in ["a", "b"]:
        builder.add_edge(START, node)
        builder.add_edge(node, END)
    try:
        await builder.compile().ainvoke({})
    except InvalidUpdateError as exc:
        observe("G12_PARALLEL_SHARED_DRAFT_CONFLICT", "GAP", True,
                {"exception": type(exc).__name__, "message": str(exc).splitlines()[0]})
    else:
        raise AssertionError("Expected LastValue update conflict")

    # 10. 模型调用异步化可行；验证实际 validator 节点可同时调用后端替身。
    active = peak = 0
    async def delayed_call(*args, **kw):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return ValidatorOutput(passed=True, checks=[], reason="离线替身"), 1, None
    rt = FakeRT()
    with patch.object(agents, "_call", side_effect=delayed_call):
        outputs = await asyncio.gather(agents.validator(base_state(0), rt), agents.validator(base_state(1), rt))
    observe("F01_ASYNC_ANALYSIS_BACKEND_FEASIBLE", "FEASIBLE", peak == 2 and all(o["checks"]["passed"] for o in outputs),
            {"peak_active_calls": peak, "output_count": len(outputs), "note": "LLM/ledger replaced; not LangGraph parallel integration"})

    # 11. 无 checkpoint 的结束读取会失败，当前 _finish 仍无条件 aget_state。
    graph = StateGraph(TaskState)
    graph.add_node("finish", lambda s: {"status": "done"})
    graph.add_edge(START, "finish")
    graph.add_edge("finish", END)
    compiled = graph.compile()
    await compiled.ainvoke({})
    try:
        await compiled.aget_state({"configurable": {"thread_id": RUN_ID}})
    except ValueError as exc:
        observe("G13_NO_CHECKPOINTER_STATE_READ", "GAP", "checkpointer" in str(exc).lower(),
                {"error": str(exc), "finish_unconditionally_reads_state": "aget_state" in inspect.getsource(Orchestrator._finish)})
    else:
        raise AssertionError("Expected no-checkpointer state read failure")

    # 12. 缺 usage 与真实零 usage 在当前表示中不可区分。
    observe("G14_MISSING_USAGE_COLLAPSES_TO_ZERO", "GAP", usage_of(None)["total_tokens"] == 0,
            {"missing_usage_total_tokens": usage_of(None)["total_tokens"]})

    versions = {}
    for package in ["pydantic", "langgraph", "langchain-core", "psycopg", "sqlparse", "pydantic-settings", "sse-starlette"]:
        versions[package] = importlib.metadata.version(package)
    result = {"python": sys.version, "versions": versions, "scope": "offline real functions with LLM/DB/ledger doubles",
              "observations": OBSERVATIONS, "count": len(OBSERVATIONS),
              "gaps": sum(o["kind"] == "GAP" for o in OBSERVATIONS)}
    Path(__file__).with_name("probe_results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Observations={result['count']}, reproduced_gaps={result['gaps']}; no production fixes applied.")


if __name__ == "__main__":
    asyncio.run(main())
