"""会话消耗接口及 SQL 过滤范围回归，不访问数据库或模型。"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import server
from app.ledger import Ledger

CONV = '00000000-0000-0000-0000-000000000001'

class SessionUsage(unittest.IsolatedAsyncioTestCase):
    async def test_summary_scopes_every_query_to_selected_conversation(self):
        ledger = Ledger.__new__(Ledger)
        ledger.fetchone = AsyncMock(return_value={'calls':2,'total_tokens':30})
        ledger.fetchall = AsyncMock(return_value=[])
        summary = await ledger.usage_summary(conversation_id=CONV)
        self.assertEqual(summary['total']['total_tokens'],30)
        for call in [*ledger.fetchone.call_args_list,*ledger.fetchall.call_args_list]:
            sql, params = call.args
            self.assertIn('FROM agent_run WHERE conversation_id = %s',sql)
            self.assertEqual(params,(UUID(CONV),))
        with self.assertRaises(ValueError):
            await ledger.usage_summary(CONV,conversation_id=CONV)

    async def test_endpoint_includes_all_turns_and_preserves_origin(self):
        ledger = SimpleNamespace(
            list_turns=AsyncMock(return_value=[{'global_task_id':'r1','turn':1},{'global_task_id':'r2','turn':2}]),
            list_llm_calls=AsyncMock(side_effect=[[{'call_id':1,'role':'planner','total_tokens':10}],
                                                  [{'call_id':2,'role':'generator','total_tokens':20}]]),
            usage_summary=AsyncMock(return_value={'total':{'calls':2,'total_tokens':30}}))
        request=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(orch=SimpleNamespace(ledger=ledger,rt=SimpleNamespace(llm_enabled=True)))))
        data=await server.conversation_usage(request,CONV)
        self.assertEqual([c['turn'] for c in data['calls']],[1,2])
        self.assertEqual([c['global_task_id'] for c in data['calls']],['r1','r2'])
        ledger.usage_summary.assert_awaited_once_with(conversation_id=CONV)
        ledger.list_turns.return_value=[]
        with self.assertRaises(server.HTTPException) as cm:
            await server.conversation_usage(request,CONV)
        self.assertEqual(cm.exception.status_code,404)

if __name__ == '__main__': unittest.main(verbosity=2)
