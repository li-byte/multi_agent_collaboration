"""提示词模板自检。

为什么要单独检一次：

  · `agents.py` 里是 `prompts.XXX.format(...)`，**占位符名字写错只有跑到那条路径才会
    抛 KeyError** —— 比如「修正器」那条分支平时很少触发，错了可能很久都没人发现。
  · 模板里漏填一个占位符同理。
  · 提示词是这个系统的**行为定义**，关键约束（比如「绝不许换成另一张同构表」）
    被误删了不会报错，只会悄悄变傻。

所以这里把每个模板都渲染一遍，并检查关键约束还在。

用法：
    python scripts/check_prompts.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import prompts  # noqa: E402

# 每个模板配一份假参数。**键名必须和模板里的占位符完全一致** ——
# 不一致就会 KeyError，这正是要检出来的东西。
TEMPLATES: dict[str, dict] = {
    "PLANNER_USER": dict(prior="", question="查一下北京客户的订单总额"),
    "PLANNER_PRIOR": dict(turns="[]"),
    "PLANNER_REPLAN_USER": dict(question="q", total=3, done=1, plan="(计划)",
                                facts="(事实)"),
    "HANDOFF_BLOCK": dict(sub_task_id="st-1", intent="i", kind="查询", depends="（无）",
                          facts="(事实)", assumptions="a", allowed="customers",
                          artifacts="art", acceptance="acc"),
    "FACT_ITEM": dict(sub_task_id="st-1", claim="c", sql="SELECT 1", result_desc="返回 1 行",
                      entity_lines=""),
    "GENERATOR_FILTER_USER": dict(question="q", sub_task_id="st-1", intent="i",
                                  kind="查询", tier1="(表结构第一层)"),
    "GENERATOR_USER": dict(question="q", handoff="(交接状态)", tables="customers",
                           detail="(表结构第二层)"),
    "VALIDATOR_USER": dict(question="q", intent="i", kind="查询", tables="customers",
                           sql="SELECT 1", detail="(表结构)", runtime_checks="[]"),
    "EXECUTOR_USER": dict(intent="i", sql="SELECT 1", result="(结果)", rowcount=0,
                          sample="(样例)"),
    "FIXER_USER": dict(question="q", handoff="(交接状态)", tables="customers",
                       sql="SELECT 1", failed="（无）", checks_reason="r", db_error="（无）",
                       exec_error="（无）", error_hint="（无）", tried="（第一次）",
                       allowed="customers", detail="(表结构)"),
    "REVIEWER_SYSTEM": dict(mode_rule="", blocked_rule=""),
    "REVIEWER_USER": dict(question="q", prior="[]", plan="(规划)", error="（无）",
                          history="[]", memory="（没有）", result="(结果)", report="(报告)"),
}

# 每个提示词必须还在的**关键约束** —— 这些被删掉不会报错，只会悄悄变傻
MUST_CONTAIN: dict[str, list[str]] = {
    "CONTRACT_HINT": ["handoff_to"],
    "PLANNER_SYSTEM": ["chat", "execute", "不能给 SQL", "safety", "refusal", "sub_task_id",
                       "depends_on", "acceptance"],
    "PLANNER_REPLAN_SYSTEM": ["continue", "revise", "finish", "不要把任务描述改写成 SQL"],
    "HANDOFF_BLOCK": ["已确认的事实", "仍在猜什么", "允许做什么", "产物在哪里", "怎样算完成"],
    "FACTS_MISSING": ["不要猜"],
    "GENERATOR_SYSTEM": ["逐字", "禁止任何 DDL", "WHERE", "LIMIT"],
    "GENERATOR_FILTER_SYSTEM": ["只从给出的表名里选", "看不到字段"],
    "VALIDATOR_SYSTEM": ["passed=false", "不能访问数据库"],
    "EXECUTOR_SYSTEM": ["0 行不等于失败", "error_hint"],
    "FIXER_SYSTEM": ["绝不许换成另一张同构表", "不要加 schema 前缀", "give_up", "what_changed"],
    "REVIEWER_SYSTEM": ["veto", "Markdown", "不要为了套格式而套格式", "不许编",
                        "基于"],
    "REVIEWER_BLOCKED_RULE": ["没有执行", "可用的表"],
}


def main() -> int:
    bad = 0

    print("提示词模板渲染：\n")
    for name, kwargs in TEMPLATES.items():
        tpl = getattr(prompts, name)
        try:
            out = tpl.format(**kwargs)
        except KeyError as exc:
            bad += 1
            print(f"  ✕ {name}")
            print(f"      模板里有占位符 {exc}，但检查清单里没有 → 名字对不上，"
                  f"真实运行时会 KeyError")
            continue
        # 只找**未填的占位符**；假值里自带的 {} 是合法内容
        left = re.findall(r"\{[a-z_]+\}", out)
        if left:
            bad += 1
            print(f"  ✕ {name}: 渲染后还有没填的占位符 {left}")
        else:
            print(f"  ✓ {name}  （{len(out)} 字符）")

    print("\n关键约束是否还在：\n")
    for name, musts in MUST_CONTAIN.items():
        text = getattr(prompts, name)
        miss = [m for m in musts if m not in text]
        if miss:
            bad += 1
            print(f"  ✕ {name}: 少了 {miss}")
        else:
            print(f"  ✓ {name}  （{len(musts)} 项都在）")

    print(f"\n共 {len(TEMPLATES) + len(MUST_CONTAIN)} 项，失败 {bad} 项。")
    if bad:
        print("× 提示词有问题 —— 它定义的是系统行为，改完必须用真实模型再验一次")
    else:
        print("✓ 提示词模板全部可用（注意：离线 mock 不走这些提示词，"
              "改完要用真实模型验证）")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
