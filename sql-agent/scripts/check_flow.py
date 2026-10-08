"""正常链路精简与确认职责回归，不访问真实数据库/模型。"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from check_reliability import Runtime, state
from app import agents, router, prompts, sql_guard
from app.graph import Orchestrator
from app.state import (PlannerOutput, QueryIntent, GeneratorOutput, SqlDraft, ReviewerOutput,
                       ValidatorOutput, TableFilterOutput, ExecutorOutput)
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command


class Flow(unittest.IsolatedAsyncioTestCase):
    async def test_validator_is_program_gate_not_model_approval(self):
        rt, s = Runtime(), state()
        s["draft"]["sql"] = "DELETE FROM products WHERE id=1"
        with patch.object(agents, "_call", side_effect=AssertionError("validator must not call model")):
            out = await agents.validator(s, rt)
        self.assertTrue(out["checks"]["passed"])
        self.assertEqual(out["next_agent"], "executor")

    async def test_executor_does_not_call_model_to_interpret_database_result(self):
        rt = Runtime()
        with patch.object(agents, "_call", side_effect=AssertionError("executor must not call model")):
            out = await agents.executor(state(), rt)
        self.assertTrue(out["result"]["ok"])
        self.assertEqual(rt.ledger.memories[0]["interpretation"], "")

    def test_success_advances_without_mandatory_replanning(self):
        s = state()
        s.update(cursor=1, result={"ok": True}, intents=[{"sub_task_id": "st-1"}, {"sub_task_id": "st-2"}])
        self.assertEqual(router.decide(s, 3)[0], "generator")

    async def test_small_catalog_generation_uses_one_model_call(self):
        rt = Runtime()
        calls = []
        async def call(rt, schema, system, user, mock_fn, **kw):
            calls.append(schema.__name__)
            return mock_fn(), 1, None
        with patch.object(agents, "_call", side_effect=call): await agents.generator(state(), rt)
        self.assertEqual(calls, ["GeneratorOutput"])

    def test_scalar_count_has_no_result_truncation_warning(self):
        v = sql_guard.analyze("SELECT count(*) FROM orders", {"orders"})
        self.assertFalse(any("截断" in note for note in v.notes))

    async def test_user_scenario_reaches_confirmation_and_cancel_stops_next_query(self):
        rt, s = Runtime(), state()
        rt.settings.max_rounds = 3
        calls, executed = [], []
        plans = [QueryIntent(sub_task_id="st-1", intent="统计已取消订单", kind="查询"),
                 QueryIntent(sub_task_id="st-2", intent="删除状态为已取消的订单", kind="删除", depends_on=["st-1"]),
                 QueryIntent(sub_task_id="st-3", intent="查询剩余订单数", kind="查询", depends_on=["st-2"])]
        async def call(rt, schema, system, user, mock_fn, ctx=None):
            calls.append(schema.__name__)
            if schema is PlannerOutput:
                return PlannerOutput(understanding="统计、删除、再统计", plan_reason="按依赖执行", intents=plans), 1, None
            if schema is GeneratorOutput:
                i = ctx["cursor"]
                sqls = ["SELECT count(*) AS n FROM orders WHERE status='已取消'",
                        "DELETE FROM orders WHERE status='已取消'", "SELECT count(*) AS n FROM orders"]
                return GeneratorOutput(draft=SqlDraft(sub_task_id=f"st-{i+1}", intent=plans[i].intent, sql=sqls[i])), 1, None
            if schema is ValidatorOutput:
                return ValidatorOutput(passed=False, reason="还没有人工确认", checks=[]), 1, None
            if schema is ReviewerOutput:
                return ReviewerOutput(decision="veto", final_answer="统计完成，删除取消，最后统计未执行"), 1, None
            return mock_fn(), 1, None
        async def execute(sql, **kw):
            executed.append(sql)
            return {"ok": True, "kind": "rows", "columns": ["n"], "rows": [[4]], "rowcount": 1, "truncated": False}
        rt.db.execute = execute
        orch = Orchestrator.__new__(Orchestrator)
        orch.rt, orch.settings, orch.ledger = rt, rt.settings, rt.ledger
        orch.compile(MemorySaver())
        orch._on_interrupt = AsyncMock()
        s.update(question="先统计已取消订单数量，再经确认删除，成功后统计剩余订单；取消则停止", intents=[], draft=None, checks=None)
        with patch.object(agents, "_call", side_effect=call):
            paused, values = await orch._drive(s["global_task_id"], s)
            self.assertTrue(paused)
            self.assertEqual(calls, ["PlannerOutput", "GeneratorOutput", "GeneratorOutput"])
            paused, values = await orch._drive(s["global_task_id"], Command(resume={"approve": False, "by": "用户"}))
        self.assertFalse(paused)
        self.assertEqual(values["status"], "cancelled")
        self.assertEqual(len(executed), 1)
        self.assertEqual(calls, ["PlannerOutput", "GeneratorOutput", "GeneratorOutput", "ReviewerOutput"])


if __name__ == "__main__": unittest.main(verbosity=2)
