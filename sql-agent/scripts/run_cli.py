"""命令行跑一次完整链路（不走 Web）。

用法：
    python scripts/run_cli.py                        # 用第一个示例问题
    python scripts/run_cli.py "删掉所有已取消的订单"   # 指定问题
    python scripts/run_cli.py --auto "..."           # 需确认时自动放行（跑自检用）
    python scripts/run_cli.py --reset "..."          # 先重建系统表
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import examples  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.graph import build_orchestrator  # noqa: E402


def _short(text, limit: int = 78) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _line(event: dict) -> str:
    t, p = event["event_type"], event.get("payload") or {}
    if t == "node_start":
        return f"开始（{p.get('intent', '') or p.get('role', '')}）"
    if t == "node_end":
        return f"{p.get('summary', '')}   [建议下一步：{p.get('suggests') or '-'}]"
    if t == "plan":
        return f"理解：{_short(p.get('understanding'), 50)}｜拆出 {len(p.get('intents') or [])} 个子任务"
    if t == "sql":
        v = p.get("verdict") or {}
        return f"{p.get('stage', '')}：{_short((p.get('draft') or {}).get('sql'), 70)}  【{v.get('action')}·{v.get('level')}】"
    if t == "check":
        return f"{'✓' if p.get('result') == 'pass' else '✕'} {p.get('item')}：{_short(p.get('detail'), 60)}"
    if t == "verdict":
        return f"{'通过' if p.get('passed') else '不通过'}：{_short(p.get('reason'), 60)}"
    if t == "handoff":
        over = "（改了模型的建议）" if p.get("overridden") else ""
        return (f"{p.get('from')} → {p.get('to')}｜{_short(p.get('reason'), 44)}{over}")
    if t == "await_confirm":
        return f"⏸ 需要人工确认：{_short(p.get('sql'), 60)}"
    if t == "confirmed":
        return f"用户决定：{'放行' if p.get('approve') else '取消'}（{p.get('by')}）"
    if t == "result":
        if p.get("kind") == "rows":
            return f"返回 {p.get('rowcount')} 行（{p.get('duration_ms')}ms）"
        return f"影响 {p.get('rowcount')} 行（{p.get('duration_ms')}ms）" + (
            f"  ← {_short(p.get('error'), 40)}" if not p.get("ok") else "")
    if t == "consistency":
        fails = [r["guard_id"] for r in (p.get("results") or []) if r["result"] == "fail"]
        return "全部通过" if p.get("passed") else f"失败：{', '.join(fails)}"
    if t == "review":
        return f"{p.get('decision', '').upper()}｜{_short(p.get('reason'), 50)}"
    if t == "done":
        return f"{p.get('status')}｜{p.get('reason', '')}"
    if t == "error":
        return _short(p.get("message"), 90)
    return ""


async def main() -> int:
    settings = get_settings()
    argv = [a for a in sys.argv[1:] if not a.startswith("--")]
    question = argv[0] if argv else examples.SAMPLES[0]["question"]
    reset = "--reset" in sys.argv
    auto = "--auto" in sys.argv

    print(f"LLM_MODE={settings.llm_mode}  MAX_ROUNDS={settings.max_rounds}")
    print(f"记录库={settings.pg_db_ledger}  执行库={settings.pg_db_biz}  执行身份={settings.runner_user}\n")

    async with build_orchestrator(settings, reset=reset) as orch:
        run = await orch.create_run(question)
        run_id = str(run["global_task_id"])
        print(f"run_id = {run_id}")
        print(f"问题：{question}\n")

        result = await orch.run(run_id)

        # 需人工确认 → 暂停
        if result.get("status") == "confirming":
            events = await orch.ledger.list_events(run_id)
            req = [e for e in events if e["event_type"] == "await_confirm"]
            if req:
                payload = req[-1]["payload"]
                print("\n—— ⏸ 需要你确认 ——")
                print(f"  SQL      ：{payload.get('sql')}")
                print(f"  风险     ：{payload.get('risk_level')}（{payload.get('action')}）")
                print(f"  涉及表   ：{'、'.join(payload.get('tables') or [])}")
                print(f"  预估影响 ：{payload.get('est_rows')} 行")
                for note in (payload.get("notes") or []) + (payload.get("cascade") or []):
                    print(f"  ⚠ {note}")
            if auto:
                print("\n  [--auto] 自动放行")
                result = await orch.resume(run_id, True, "CLI 自动确认")
            else:
                answer = input("\n  确认执行？(y/N) ").strip().lower()
                result = await orch.resume(run_id, answer == "y", "CLI 用户")

        print("\n—— 协作链路（链路不固定，路由由护栏决定）——")
        for e in await orch.ledger.list_events(run_id):
            line = _line(e)
            if line:
                print(f"[{e['seq']:>3}] r{e['round']} {e['role']:<10} {e['event_type']:<14} {_short(line, 96)}")

        print("\n—— 运行时一致性不变量 ——")
        last = [e for e in await orch.ledger.list_events(run_id) if e["event_type"] == "consistency"]
        if last:
            for r in (last[-1]["payload"].get("results") or []):
                print(f"  {'✓' if r['result'] == 'pass' else '✕'} {r['guard_id']:<30} {_short(r['detail'], 60)}")

        print("\n—— SQL 审计（每条 SQL 的全程）——")
        for a in await orch.ledger.list_audit(run_id):
            print(f"  r{a['round']} {a['sub_task_id']:<8} {a['stage']:<14} {a['action'] or '-':<7} "
                  f"{(a['risk_level'] or '-'):<4} 影响={a['affected_rows'] if a['affected_rows'] is not None else '-'}"
                  f"  {_short(a['sql_text'], 46)}")

        print("\n—— 给用户的答复 ——")
        print(f"  {result.get('final_answer') or '（无）'}")

        print(f"\n最终状态：{result.get('status')}  round={result.get('round')}  "
              f"version={result.get('version')}")
        return 0 if result.get("status") == "done" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
