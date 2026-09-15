"""命令行跑一次完整链路（不走 Web），便于调试与验证。

用法：
    python scripts/run_cli.py            # 用内置示例资料
    python scripts/run_cli.py --reset    # 先重建表
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import mock_data  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.graph import build_orchestrator  # noqa: E402


def _short(text: str, limit: int = 84) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def main() -> int:
    settings = get_settings()
    reset = "--reset" in sys.argv
    print(f"LLM_MODE={settings.llm_mode}  MAX_ROUNDS={settings.max_rounds}\n")

    async with build_orchestrator(settings, reset=reset) as orch:
        sample = mock_data.SAMPLES[0]
        run = await orch.create_run(sample["question"], [sample["material"]])
        run_id = str(run["global_task_id"])
        print(f"run_id = {run_id}")
        print(f"问题：{sample['question']}")
        print(f"资料：{run['chunk_count']} 段\n")

        final = await orch.run(run_id)

        print("—— 协作链路 ——")
        for e in await orch.ledger.list_events(run_id):
            p = e.get("payload") or {}
            line = ""
            t = e["event_type"]
            if t == "node_start":
                line = f"开始（第 {e['round'] + 1} 轮）"
            elif t == "node_end":
                line = p.get("summary", "")
            elif t == "handoff":
                line = p.get("label", "")
            elif t == "finding":
                cites = p.get("citations") or []
                line = f"[{p.get('status')}] {p.get('claim')} ← {cites[0]['source_id'] if cites else '无依据'}"
            elif t == "answer":
                line = f"{p.get('answer_id')} · {_short(p.get('summary', ''), 60)}"
            elif t == "consistency":
                fails = [r["invariant_id"] for r in (p.get("results") or []) if r["result"] == "fail"]
                line = "全部通过" if p.get("passed") else f"失败：{', '.join(fails)}"
            elif t == "review":
                line = f"{p.get('decision').upper()} {_short(p.get('reason', ''), 50)}"
                if p.get("forced_by_runtime"):
                    line += "（运行时强制）"
            elif t == "done":
                line = f"{p.get('status')} · {p.get('reason', '')}"
            elif t == "error":
                line = p.get("message", "")
            print(f"[{e['seq']:>3}] {e['role']:<10} {t:<12} r{e['round']}  {_short(line)}")

        print("\n—— 一致性校验明细（最后一轮）——")
        for r in (final.get("consistency") or {}).get("results", []):
            print(f"  {'✓' if r['result'] == 'pass' else '✕'} {r['invariant_id']:<28} {r['detail']}")

        ans = (final.get("answers") or [{}])[-1]
        print("\n—— 返回给用户的答案 ——")
        print(f"  {ans.get('summary', '')}")
        for p in ans.get("points") or []:
            c = (p.get("citations") or [{}])[0]
            print(f"  · [{p.get('sub_task_id')}] {p.get('statement')}")
            print(f"      依据 {c.get('source_id')}：“{_short(c.get('quote', ''), 40)}”")
        if ans.get("caveats"):
            print("  注意：" + "；".join(ans["caveats"]))

        print("\n—— 账本 ——")
        for t in await orch.ledger.list_tasks(run_id):
            print(f"  {t['sub_task_id']:<16} att={t['attempt']} {t['status']:<10} "
                  f"ver={t['version']} idem=…{(t.get('idempotency_key') or '')[-12:]}")

        print(f"\n最终状态：{final.get('status')}  round={final.get('round')}  "
              f"version={final.get('version')}")
        return 0 if final.get("status") == "done" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
