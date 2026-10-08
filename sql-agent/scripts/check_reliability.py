"""离线安全与执行一致性回归：不连接真实 DB/LLM。"""
from __future__ import annotations

import asyncio
import sys
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import agents, catalog, sql_guard, validators, server
from app.graph import Orchestrator
from app.state import PlannerOutput, FixerOutput, SqlDraft, ExecutorOutput, TaskState
from app.state import now_hash
from langgraph.graph import StateGraph, START, END


class Ledger:
    def __init__(self):
        self.opened, self.finished, self.memories = [], [], []

    async def open_task(self, **kw):
        self.opened.append(kw)
        return kw

    async def finish_task(self, *args, **kw):
        self.finished.append((args[1], args[2]))
        return {}

    async def audit_sql(self, *args, **kw): return 1
    async def set_run_status(self, *args, **kw): return 2
    async def save_memory(self, *args, **kw):
        self.memories.append({"claim": args[2], **kw})
        return 1
    async def list_memory(self, *args): return self.memories
    async def list_tasks(self, *args): return [{"idempotency_key": "k"}]


class DB:
    def __init__(self): self.calls = 0
    async def explain(self, sql): return {"ok": True, "est_rows": 2}
    async def execute(self, sql, **kw):
        self.calls += 1
        return {"ok": True, "kind": "rows", "columns": ["id"],
                "rows": [[i] for i in range(60)], "rowcount": 60, "truncated": True}


class Runtime:
    def __init__(self):
        self.ledger, self.db, self.llm = Ledger(), DB(), None
        self.allowed_tables = catalog.allowed_tables()
        self.settings = SimpleNamespace(lease_seconds=300, max_rows=500)
    async def emit(self, *args, **kw): pass


def state(cursor=0):
    return {"global_task_id": "11111111-1111-1111-1111-111111111111", "question": "查询商品",
            "mode": "execute", "version": 1, "round": 0, "cursor": cursor,
            "intents": [{"sub_task_id": "st-1", "intent": "查询商品", "kind": "查询"}],
            "draft": {"sub_task_id": "st-1", "intent": "查询商品", "sql": "SELECT id FROM products LIMIT 60", "tables": ["products"]},
            "verdict": {"level": "只读", "action": "SELECT", "tables": ["products"]},
            "checks": {"passed": True, "sql_hash": now_hash("SELECT id FROM products LIMIT 60")}, "history": [], "memory": []}


def report(history):
    return validators.validate(allowed_tables={"products"}, intents=[{"sub_task_id": "st-1"}],
                               history=history, tasks=[{"idempotency_key": "k"}], version=1, round_no=0)


class Reliability(unittest.IsolatedAsyncioTestCase):
    async def test_checkpoint_initialization_failure_stops_startup_and_closes_resources(self):
        from app import graph
        orch = SimpleNamespace(ledger=SimpleNamespace(open=AsyncMock()),
                               migrate=AsyncMock(return_value={"ledger_statements": 1, "tables": ["products"]}),
                               close=AsyncMock())
        class BrokenSaver:
            @staticmethod
            def from_conn_string(dsn): raise RuntimeError("unavailable")
        module = SimpleNamespace(AsyncPostgresSaver=BrokenSaver)
        settings = SimpleNamespace(use_checkpointer=True, ledger_dsn="unused")
        with patch.object(graph, "Orchestrator", return_value=orch), patch.dict(sys.modules, {"langgraph.checkpoint.postgres.aio": module}):
            with self.assertLogs("app.graph", level="ERROR"):
                with self.assertRaises(RuntimeError):
                    async with graph.build_orchestrator(settings): self.fail("must not start")
        orch.close.assert_awaited_once()

    async def test_no_checkpoint_cannot_pause_for_confirmation(self):
        rt, s = Runtime(), state()
        s["draft"]["sql"] = "DELETE FROM products WHERE id=1"
        s.update(await agents.validator(s, rt))
        async def execute(s): return await agents.executor(s, rt)
        b = StateGraph(TaskState)
        b.add_node("executor", execute)
        b.add_edge(START, "executor")
        b.add_edge("executor", END)
        orch = Orchestrator.__new__(Orchestrator)
        orch.graph, orch._saver = b.compile(), None
        with self.assertRaises(RuntimeError): await orch._drive("no-checkpoint", s)
        self.assertEqual(rt.db.calls, 0)
    def test_legacy_memory_claim_is_not_presented_as_database_fact(self):
        legacy = {"sub_task_id": "st-1", "claim": "这是全部商品", "stage": "executed", "verified": True,
                  "sql_id": 1, "sql_text": "SELECT id FROM products LIMIT 60", "rowcount": 60}
        fact = agents._render_fact(legacy)
        self.assertNotIn("全部商品", fact)
        self.assertIn("60", fact)
        self.assertIn("不能当作全部数据", fact)
    def test_builtin_calls_are_bound_before_display_and_execution(self):
        from app import catalog_check
        raw = "SELECT sum(stock) FROM products"
        v = sql_guard.analyze(raw, {"products"})
        self.assertIn("pg_catalog.sum", v.normalized_sql)
        self.assertTrue(catalog_check.check_sql(v.normalized_sql)[0])
        self.assertEqual(sql_guard.analyze(v.normalized_sql, {"products"}).normalized_sql, v.normalized_sql)

    async def test_missing_resume_checkpoint_is_recorded_failed(self):
        orch = Orchestrator.__new__(Orchestrator)
        orch._saver, orch.rt, orch.ledger = object(), Runtime(), Ledger()
        orch.graph = SimpleNamespace(aget_state=AsyncMock(return_value=SimpleNamespace(next=())))
        orch._fail = AsyncMock()
        with self.assertLogs("app.graph", level="ERROR"):
            with self.assertRaises(RuntimeError): await orch.resume("id", True, "用户", "hash")
        orch._fail.assert_awaited_once()

    async def test_readonly_transaction_blocks_write_without_poisoning_next_transaction(self):
        from app.db import BizDatabase
        class Connection:
            def __init__(self): self.read_only, self.writes = False, 0
            @asynccontextmanager
            async def transaction(self):
                self.read_only = False
                try: yield
                finally: self.read_only = False
            @asynccontextmanager
            async def cursor(self): yield self
            async def execute(self, sql):
                if sql == "SET TRANSACTION READ ONLY": self.read_only = True
                elif sql.startswith("SET LOCAL"): pass
                elif sql.startswith("UPDATE"):
                    if self.read_only: raise RuntimeError("read-only transaction")
                    self.writes += 1
            description, rowcount = None, 1
        conn = Connection()
        class Pool:
            @asynccontextmanager
            async def connection(self): yield conn
        db = BizDatabase.__new__(BizDatabase)
        db._runner = Pool()
        result = await db.execute("UPDATE products SET stock=1 WHERE id=1", read_only=True)
        self.assertFalse(result["ok"])
        self.assertEqual(conn.writes, 0)
        result = await db.execute("UPDATE products SET stock=1 WHERE id=1", read_only=False)
        self.assertTrue(result["ok"])
        self.assertEqual(conn.writes, 1)
    async def test_stale_confirmation_cannot_approve_next_pause(self):
        class Admission:
            async def get_run(self, run_id): return {"status": "confirming", "version": 8}
            async def set_run_status(self, *a, **kw): return 9
        orch = SimpleNamespace(ledger=Admission(), _saver=object())
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(orch=orch)))
        def discard(coro): coro.close()
        with patch.object(server.asyncio, "create_task", side_effect=discard):
            with self.assertRaises(server.HTTPException) as caught:
                await server.confirm_run(request, "id", server.ConfirmRequest(approve=True, version=4, sql_hash="old"))
        self.assertEqual(caught.exception.status_code, 409)
    def test_quoted_unauthorized_tables_and_cte_scope_are_rejected(self):
        for sql in ['SELECT id FROM "Products"', 'SELECT id FROM "secrets"',
                    'WITH p AS (SELECT id FROM "secrets") SELECT * FROM p',
                    'SELECT * FROM (WITH secrets AS (SELECT id FROM products) SELECT * FROM secrets) p JOIN "secrets" s ON true']:
            with self.subTest(sql=sql): self.assertEqual(sql_guard.analyze(sql, {"products"}).level, "禁止")

    async def test_duplicate_confirmation_is_rejected(self):
        class Admission:
            def __init__(self): self.status, self.version = "confirming", 3
            async def get_run(self, run_id):
                snapshot = {"status": self.status, "version": self.version}
                await asyncio.sleep(0)
                return snapshot
            async def set_run_status(self, run_id, status, **kw):
                if kw.get("expected_version") != self.version: return None
                self.status, self.version = status, self.version + 1
                return self.version
        orch = SimpleNamespace(ledger=Admission(), _saver=object())
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(orch=orch)))
        def discard(coro): coro.close()
        with patch.object(server.asyncio, "create_task", side_effect=discard):
            results = await asyncio.gather(server.confirm_run(request, "id", server.ConfirmRequest(approve=True, version=3, sql_hash="h")),
                                           server.confirm_run(request, "id", server.ConfirmRequest(approve=True, version=3, sql_hash="h")), return_exceptions=True)
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        self.assertEqual(sum(getattr(r, "status_code", None) == 409 for r in results), 1)
    async def test_changed_sql_requires_new_validation(self):
        rt, s = Runtime(), state()
        s["draft"]["sql"] = "UPDATE products SET stock=0 WHERE id=1"
        await agents.executor(s, rt)
        self.assertEqual(rt.db.calls, 0)

    async def test_confirmed_boolean_cannot_bypass_human_decision(self):
        rt, s = Runtime(), state()
        s["draft"]["sql"] = "DELETE FROM products WHERE id=1"
        s["confirmed"] = True
        s.update(await agents.validator(s, rt))
        with patch.object(agents, "interrupt", return_value={"approve": False, "by": "用户"}):
            out = await agents.executor(s, rt)
        self.assertEqual(rt.db.calls, 0)
        self.assertEqual(out["result"]["kind"], "cancelled")

    async def test_confirmation_for_different_sql_is_rejected(self):
        rt, s = Runtime(), state()
        s["draft"]["sql"] = "DELETE FROM products WHERE id=1"
        s.update(await agents.validator(s, rt))
        with patch.object(agents, "interrupt", return_value={"approve": True, "by": "用户", "sql_hash": "other"}):
            with self.assertRaises(RuntimeError): await agents.executor(s, rt)
        self.assertEqual(rt.db.calls, 0)
    def test_unapproved_function_rejected(self):
        for sql in ["SELECT evil_function(id) FROM products", "SELECT pg_sleep(10)",
                    'SELECT "SUM"(id) FROM products', "SELECT sum(id,1) FROM products",
                    "SELECT public.evil_function(id) FROM products"]:
            with self.subTest(sql=sql): self.assertEqual(sql_guard.analyze(sql, {"products"}).level, "禁止")

    async def test_executor_rechecks_sql_before_database_access(self):
        rt, s = Runtime(), state()
        s["draft"]["sql"] = "WITH x AS (DELETE FROM products RETURNING id) SELECT * FROM x"
        out = await agents.executor(s, rt)
        self.assertEqual(rt.db.calls, 0)
        self.assertEqual(out["verdict"]["level"], "禁止")

    async def test_full_graph_cancel_remains_cancelled(self):
        from langgraph.checkpoint.memory import MemorySaver
        from langgraph.types import Command
        rt, s = Runtime(), state()
        rt.settings.max_rounds = 3
        orch = Orchestrator.__new__(Orchestrator)
        orch.rt, orch.settings, orch.ledger = rt, rt.settings, rt.ledger
        orch.compile(MemorySaver())
        s["draft"]["sql"] = "DELETE FROM products WHERE id=1"
        s["verdict"].update(level="需确认", action="DELETE", needs_confirm=True)
        s.update(await agents.validator(s, rt))
        # 使用真实 executor/router/reviewer 子图，避免初始 planner 覆盖场景。
        b = StateGraph(TaskState)
        async def execute(s): return await agents.executor(s, rt)
        async def review(s): return await agents.reviewer(s, rt)
        b.add_node("executor", execute)
        b.add_node("reviewer", review)
        b.add_edge(START, "executor")
        b.add_edge("executor", "reviewer")
        b.add_edge("reviewer", END)
        g = b.compile(checkpointer=MemorySaver())
        cfg = {"configurable": {"thread_id": "cancel"}}
        await g.ainvoke(s, cfg)
        out = await g.ainvoke(Command(resume={"approve": False, "by": "用户"}), cfg)
        self.assertEqual(out["status"], "cancelled")
        self.assertFalse(out["last_consistency"]["passed"])
        self.assertEqual(rt.db.calls, 0)

    async def test_duplicate_start_is_rejected(self):
        class Admission:
            def __init__(self): self.status, self.version = "created", 1
            async def get_run(self, run_id):
                snapshot = {"status": self.status, "version": self.version}
                await asyncio.sleep(0)
                return snapshot
            async def set_run_status(self, run_id, status, **kw):
                if kw.get("expected_version") != self.version: return None
                self.status, self.version = status, self.version + 1
                return self.version
        orch = SimpleNamespace(ledger=Admission())
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(orch=orch)))
        def discard(coro): coro.close()
        with patch.object(server.asyncio, "create_task", side_effect=discard):
            results = await asyncio.gather(server.start_run(request, "id"), server.start_run(request, "id"), return_exceptions=True)
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        self.assertEqual(sum(getattr(r, "status_code", None) == 409 for r in results), 1)
    def test_writable_ctes_rejected_including_nested_and_quoted(self):
        for sql in [
            'WITH "removed" AS (DELETE FROM products WHERE id=1 RETURNING id) SELECT id FROM "removed"',
            "WITH x AS (UPDATE products SET stock=0 RETURNING id) SELECT id FROM x",
            "WITH x AS (INSERT INTO products(name) VALUES ('x') RETURNING id) SELECT id FROM x",
            "WITH a AS (SELECT id FROM products), b AS (DELETE FROM products RETURNING id) SELECT * FROM a",
        ]:
            with self.subTest(sql=sql): self.assertEqual(sql_guard.analyze(sql, {"products"}).level, "禁止")

    def test_read_cte_and_upsert_remain_available(self):
        self.assertEqual(sql_guard.analyze("WITH p AS (SELECT id FROM products) SELECT id FROM p", {"products"}).level, "只读")
        self.assertEqual(sql_guard.analyze("INSERT INTO products(id,name) VALUES(1,'x') ON CONFLICT(id) DO UPDATE SET name='y'", {"products"}).level, "写入")

    def test_invalid_syntax_fails_closed(self):
        self.assertEqual(sql_guard.analyze("SELECT FROM WHERE", {"products"}).level, "禁止")

    def test_failed_execution_does_not_satisfy_coverage(self):
        h = {"sub_task_id": "st-1", "intent": "查询", "validated": True, "validated_passed": True,
             "executed": True, "exec_ok": False}
        self.assertFalse(report([h]).passed)

    def test_rejected_validation_blocks_final_report(self):
        h = {"sub_task_id": "st-1", "intent": "查询", "validated": True, "validated_passed": False,
             "executed": True, "exec_ok": True}
        self.assertFalse(report([h]).passed)

    async def test_cancel_is_not_success_or_downstream_progress(self):
        rt, s = Runtime(), state()
        s["draft"]["sql"] = "DELETE FROM products WHERE id=1"
        s["verdict"].update(level="需确认", needs_confirm=True, action="DELETE")
        s.update(await agents.validator(s, rt))
        with patch.object(agents, "interrupt", return_value={"approve": False, "by": "用户"}):
            out = await agents.executor(s, rt)
        self.assertFalse(out["history"][0]["executed"])
        self.assertFalse(out["result"]["ok"])
        self.assertEqual(out["cursor"], 0)
        self.assertFalse(report(out["history"]).passed)
        self.assertEqual(rt.db.calls, 0)

    async def test_planner_settles_claimed_identity(self):
        rt = Runtime()
        async def call(*a, **kw): return PlannerOutput(understanding="继续", plan_reason="复查", plan_action="continue"), 1, None
        with patch.object(agents, "_call", side_effect=call): await agents.planner(state(1), rt)
        self.assertEqual(rt.ledger.opened[0]["sub_task_id"], rt.ledger.finished[-1][0])

    async def test_fixer_retry_settles_same_round_identity(self):
        rt = Runtime()
        async def call(*a, **kw): return FixerOutput(draft=SqlDraft(sub_task_id="st-1", intent="查询", sql="SELECT id FROM products"), what_changed="改写"), 2, None
        with patch.object(agents, "_call", side_effect=call): await agents.fixer(state(), rt)
        self.assertEqual(rt.ledger.opened[0]["sub_task_id"], rt.ledger.finished[0][0])
        self.assertEqual(rt.ledger.opened[-1]["sub_task_id"], rt.ledger.finished[-1][0])

    async def test_memory_preserves_completeness_and_separates_model_claim(self):
        rt = Runtime()
        async def call(*a, **kw): return ExecutorOutput(ok=True, summary="这是全部商品"), 1, None
        with patch.object(agents, "_call", side_effect=call): await agents.executor(state(), rt)
        mem = rt.ledger.memories[0]
        self.assertNotIn("全部商品", mem["claim"])
        self.assertTrue(mem["truncated"])
        self.assertTrue(mem["sampled"])
        self.assertEqual(mem["interpretation"], "")
        self.assertFalse(mem["interpretation_verified"])

    async def test_no_checkpoint_finish_uses_stream_values(self):
        builder = StateGraph(TaskState)
        builder.add_node("finish", lambda s: {"status": "done"})
        builder.add_edge(START, "finish")
        builder.add_edge("finish", END)
        orch = Orchestrator.__new__(Orchestrator)
        orch.graph, orch._saver, orch.rt = builder.compile(), None, Runtime()
        paused, values = await orch._drive("test", {})
        self.assertFalse(paused)
        self.assertEqual((await orch._finish("test", values))["status"], "done")

    async def test_complete_mock_graph_without_checkpoint(self):
        rt = Runtime()
        rt.settings.max_rounds = 3
        orch = Orchestrator.__new__(Orchestrator)
        orch.rt, orch.settings, orch.ledger = rt, rt.settings, rt.ledger
        orch.compile()
        s = state()
        s.update(question="查询商品库存", intents=[], draft=None, checks=None, verdict=None)
        paused, values = await orch._drive(s["global_task_id"], s)
        self.assertFalse(paused)
        self.assertEqual(values["status"], "done")
        self.assertTrue(values["last_consistency"]["passed"])
        self.assertGreater(rt.db.calls, 0)

    async def test_correct_sql_confirmation_executes_once(self):
        from langgraph.checkpoint.memory import MemorySaver
        from langgraph.types import Command
        rt, s = Runtime(), state()
        s["draft"]["sql"] = "DELETE FROM products WHERE id=1"
        s.update(await agents.validator(s, rt))
        async def execute(s): return await agents.executor(s, rt)
        b = StateGraph(TaskState)
        b.add_node("executor", execute)
        b.add_edge(START, "executor")
        b.add_edge("executor", END)
        g = b.compile(checkpointer=MemorySaver())
        cfg = {"configurable": {"thread_id": "approve"}}
        await g.ainvoke(s, cfg)
        self.assertEqual(rt.db.calls, 0)
        await g.ainvoke(Command(resume={"approve": True, "by": "用户", "sql_hash": now_hash(s["draft"]["sql"])}), cfg)
        self.assertEqual(rt.db.calls, 1)


if __name__ == "__main__": unittest.main(verbosity=2)
