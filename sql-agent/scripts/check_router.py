"""路由护栏自检：模型能去的地方，护栏允不允许。

重点校验四件事：
  1. 硬边界：没校验不能执行；判定禁止永不执行
  2. 自由度：该由模型选的地方（小修 vs 重新规划）确实听模型的
  3. **无进展检测**：同一 SQL 反复失败 / 同一错误反复出现 → 主动停下来
  4. **复查循环**：每做完一个子任务回规划器复查一次，且**只复查一次**（不绕圈）

用法：
    python scripts/check_router.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.router import decide  # noqa: E402
from app.state import now_hash  # noqa: E402

DRAFT = {"sub_task_id": "st-1", "sql": "SELECT 1", "intent": "数一下"}
BASE = {"global_task_id": "x", "question": "q", "intents": [{"sub_task_id": "st-1"}],
        "cursor": 0, "round": 0, "version": 3, "attempts": [], "history": []}


def st(**kw) -> dict:
    s = dict(BASE)
    s.update(kw)
    return s


def fail_with(sql: str, sig: str) -> list[dict]:
    return [{"round": 0, "sql_hash": now_hash(sql), "sql": sql, "ok": False,
             "error_sig": sig, "why": "模拟失败"}]


# (说明, state, 期望去哪)
CASES: list[tuple[str, dict, str]] = [
    # ---------- 还没开始 ----------
    ("还没规划 → 规划器", st(intents=[]), "planner"),
    ("有意图但没 SQL → 生成", st(draft=None), "generator"),

    # ---------- 硬边界：不许跳过校验 ----------
    ("有 SQL 没校验 → 必须先去校验", st(draft=DRAFT, verdict={"level": "只读"}, checks=None),
     "validator"),
    ("模型想直接执行也没用（护栏改道）",
     st(draft=DRAFT, verdict={"level": "只读"}, checks=None, next_agent="executor"),
     "validator"),

    # ---------- 硬边界：禁止级永不执行 ----------
    ("判定禁止 → 直接去汇总，永不执行",
     st(draft=DRAFT, verdict={"level": "禁止", "reasons": ["DROP"]}, checks=None),
     "reviewer"),
    ("判定禁止 + 模型想去执行（护栏拦住）",
     st(draft=DRAFT, verdict={"level": "禁止", "reasons": ["DROP"]}, checks=None,
        next_agent="executor"),
     "reviewer"),

    # ---------- 硬边界：只有真的执行过才算完成 ----------
    # 执行器成功后会推进 cursor（这里必须一并模拟，否则测的是"还有子任务"那条分支）
    ("执行成功且子任务全跑完 → 汇总",
     st(draft=DRAFT, verdict={"level": "只读"}, checks={"passed": True},
        result={"ok": True}, cursor=1,
        history=[{"sub_task_id": "st-1", "executed": True}]), "reviewer"),
    ("执行成功但还有下一个子任务 → 继续生成",
     st(draft=DRAFT, verdict={"level": "只读"}, checks={"passed": True},
        result={"ok": True}, cursor=0,
        history=[{"sub_task_id": "st-1", "executed": True}]), "generator"),

    # ---------- 硬边界：SQL 只能由生成器产出 ----------
    ("没有 SQL → 只能去生成器（没有别的入口）",
     st(intents=[{"sub_task_id": "st-1"}], draft=None), "generator"),
    ("模型想直接跳过生成器也没用（护栏改道）",
     st(intents=[{"sub_task_id": "st-1"}], draft=None, next_agent="fixer"), "generator"),

    # ---------- 硬边界：聊天不进生成/执行 ----------
    ("打招呼（chat）→ 不进生成，直接答复",
     st(mode="chat", intents=[], draft=None, next_agent="generator"), "reviewer"),
    ("被拒绝 → 直接答复，不进生成",
     st(refusal="超出允许范围", intents=[], draft=None, next_agent="generator"), "reviewer"),

    # ---------- 分歧点：让模型选 ----------
    ("校验不通过 → 模型选修正", st(draft=DRAFT, verdict={"level": "只读"},
                                   checks={"passed": False}, next_agent="fixer"), "fixer"),
    ("校验不通过 → 模型选重新规划", st(draft=DRAFT, verdict={"level": "只读"},
                                       checks={"passed": False}, next_agent="planner"), "planner"),
    ("执行失败 → 模型选修正", st(draft=DRAFT, verdict={"level": "只读"},
                                 checks={"passed": True}, result={"ok": False},
                                 next_agent="fixer"), "fixer"),
    ("执行失败 → 模型选重新规划", st(draft=DRAFT, verdict={"level": "只读"},
                                     checks={"passed": True}, result={"ok": False},
                                     next_agent="planner"), "planner"),

    # ---------- 正常推进 ----------
    ("校验通过 → 执行", st(draft=DRAFT, verdict={"level": "只读"}, checks={"passed": True}),
     "executor"),
    ("需确认未确认 → 执行（执行器内部暂停）",
     st(draft=DRAFT, verdict={"level": "需确认"}, checks={"passed": True}), "executor"),
    ("执行成功 + 还有子任务 → 生成下一个",
     st(draft=DRAFT, verdict={"level": "只读"}, checks={"passed": True}, result={"ok": True},
        cursor=0, intents=[{"sub_task_id": "st-1"}, {"sub_task_id": "st-2"}]), "generator"),
    ("全部完成 → 汇总",
     st(draft=DRAFT, verdict={"level": "只读"}, checks={"passed": True}, result={"ok": True},
        cursor=2, intents=[{"sub_task_id": "st-1"}, {"sub_task_id": "st-2"}]), "reviewer"),

    # ---------- 复查循环（后面的计划要按前面**实际结果**评估）----------
    ("做完一个子任务、后面还有 → 先回规划器复查",
     st(draft=DRAFT, verdict={"level": "只读"}, checks={"passed": True}, result={"ok": True},
        cursor=1, intents=[{"sub_task_id": "st-1"}, {"sub_task_id": "st-2"}]), "planner"),
    ("同一位置**复查过了** → 不再绕圈，去生成下一个",
     st(draft=DRAFT, verdict={"level": "只读"}, checks={"passed": True}, result={"ok": True},
        cursor=1, replanned_cursor=1,
        intents=[{"sub_task_id": "st-1"}, {"sub_task_id": "st-2"}]), "generator"),
    ("复查后计划改了（draft 被清空）→ 重新生成",
     st(draft=None, verdict={}, checks=None, result=None,
        cursor=1, replanned_cursor=1,
        intents=[{"sub_task_id": "st-1"}, {"sub_task_id": "st-2"}]), "generator"),
    ("最后一个子任务做完 → 不复查，直接汇总",
     st(draft=DRAFT, verdict={"level": "只读"}, checks={"passed": True}, result={"ok": True},
        cursor=2, replanned_cursor=1,
        intents=[{"sub_task_id": "st-1"}, {"sub_task_id": "st-2"}]), "reviewer"),
    ("执行**失败** → 不当成做完了，不进复查（走修正）",
     # 执行失败时游标不会推进（执行器只在成功时 +1），所以这里 cursor 仍是 0
     st(draft=DRAFT, verdict={"level": "只读"}, checks={"passed": True}, result={"ok": False},
        cursor=0, intents=[{"sub_task_id": "st-1"}, {"sub_task_id": "st-2"}]), "fixer"),
    ("还没做完任何子任务（cursor=0）→ 不复查",
     st(draft=DRAFT, verdict={"level": "只读"}, checks={"passed": True}, result={"ok": True},
        cursor=0, intents=[{"sub_task_id": "st-1"}, {"sub_task_id": "st-2"}]), "generator"),

    # ---------- 提前收尾 ----------
    ("规划器拒绝 → 汇总", st(intents=[], refusal="删表超出允许范围"), "reviewer"),
    ("规划器没拆出东西 → 汇总", st(intents=[], last_agent="planner"), "reviewer"),
    ("修正智能体放弃 → 汇总", st(draft=DRAFT, give_up="改不了", checks={"passed": False}),
     "reviewer"),
    ("轮次用尽 → 汇总", st(draft=DRAFT, verdict={"level": "只读"},
                           checks={"passed": False}, round=3), "reviewer"),

    # ---------- 无进展检测（本轮新增，之前是漏掉的）----------
    ("同一条 SQL 失败 2 次 → 停止重试",
     st(draft=DRAFT, verdict={"level": "只读"}, checks={"passed": False},
        attempts=fail_with("SELECT 1", "a") + fail_with("SELECT 1", "b")), "reviewer"),
    ("同一错误出现 2 次 → 停止重试",
     st(draft={"sub_task_id": "st-1", "sql": "SELECT 2"}, verdict={"level": "只读"},
        checks={"passed": False},
        attempts=[{"round": 0, "sql_hash": "h1", "ok": False, "error_sig": "same"},
                  {"round": 1, "sql_hash": "h2", "ok": False, "error_sig": "same"}]),
     "reviewer"),
    ("只失败过 1 次 → 仍然允许再修一次（不能过早放弃）",
     st(draft={"sub_task_id": "st-1", "sql": "SELECT 3"}, verdict={"level": "只读"},
        checks={"passed": False}, attempts=fail_with("SELECT 9", "a")), "fixer"),
]


def main() -> int:
    print("路由护栏自检：\n")
    bad = 0
    for label, state, want in CASES:
        got, reason = decide(state, 3)
        ok = got == want
        if not ok:
            bad += 1
        mark = "✓" if ok else "✕"
        tail = "" if ok else f"   期望 {want}"
        print(f"  {mark} {label:<38} → {got:<10}{tail}")
        if ok and (got == "reviewer" or "护栏" in label or "停止重试" in label):
            print(f"        理由：{reason}")
    print(f"\n共 {len(CASES)} 项，失败 {bad} 项。")
    print("✓ 护栏符合预期" if not bad else "× 护栏有问题，必须修")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
