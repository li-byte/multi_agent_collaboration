"""路由护栏：**模型决定「想」去哪，运行时决定「能」去哪。**

这是「链路不固定」的落点。每个智能体在结构化输出里给一个 `handoff_to`
（它自己建议下一步交给谁），但路由器先用状态算出**候选集**：

  · 候选集只有一个元素时 —— 直接去，模型的建议无效（硬边界）
  · 候选集有多个元素时   —— 让模型在合法范围内自己选（真正的协作自由度）

于是：
  · 不同问题会走不同路径（简单问题一条 SQL 直达；复杂问题可能反复修正、
    甚至退回重新规划），**链路确实不固定**；
  · 但「没验证就不能执行」「禁止语句永不执行」这些边界一条都破不了。

候选集为多的两个地方，正是真实存在的分歧点：
  · 验证不通过 —— 小修（fixer）还是重新理解问题（planner）？
  · 执行失败   —— 改 SQL（fixer）还是重新拆问题（planner）？
"""

from __future__ import annotations

from .state import TaskState

# 每个智能体「正常情况下」的下一步候选
AGENT_DEFAULT: dict[str, str] = {
    "planner": "generator",
    "generator": "validator",
    "validator": "executor",
    "executor": "reviewer",
    "fixer": "validator",
    "reviewer": "done",
}


def _progress(state: TaskState) -> int:
    """已经处理完（有执行结果）的子任务数量。"""
    return len({h.get("sub_task_id") for h in (state.get("history") or [])
                if h.get("executed")})


def _needs_confirm(verdict: dict) -> bool:
    """需确认的判断加一层兜底：即使 verdict 里没写 needs_confirm，看 level 也能认出来。"""
    return bool(verdict.get("needs_confirm")) or verdict.get("level") == "需确认"


def decide(state: TaskState, max_rounds: int) -> tuple[str, str]:
    """返回 (下一个节点, 为什么这么定)。"""
    want = state.get("next_agent") or None
    intents = list(state.get("intents") or [])
    draft = state.get("draft")
    verdict = state.get("verdict") or {}
    checks = state.get("checks")
    result = state.get("result")
    round_no = int(state.get("round", 0))
    cursor = int(state.get("cursor", 0))

    # ---------- ⓪ 某个智能体没干成 —— 别再往下转，直接收尾给用户交代 ----------
    if state.get("status") == "failed":
        return "reviewer", "有智能体未能完成工作，收尾并如实说明"

    # ---------- ⓪' 规划器判定「这个请求我不做」 ----------
    if state.get("refusal"):
        return "reviewer", f"规划器判定请求超出允许范围：{state['refusal'][:40]}"

    # ---------- ① 还没规划 ----------
    if not intents:
        if state.get("last_agent") == "planner":
            return "reviewer", "规划器没有拆出任何可执行的子任务"
        return "planner", "还没拆解问题，不能直接去 " + str(want or "下一步")

    # ---------- ② 全部子任务都跑完了 ----------
    if cursor >= len(intents):
        return "reviewer", f"{len(intents)} 个子任务全部完成，进入汇总"

    # ---------- ③ 修正轮次用尽 ----------
    if round_no >= max_rounds:
        return "reviewer", f"修正轮次已达上限 {max_rounds}，收尾并如实说明"

    # ---------- ④ 当前子任务还没有 SQL ----------
    current = intents[cursor].get("sub_task_id")
    if not draft or draft.get("sub_task_id") != current:
        if want in (None, "generator", "planner"):
            return "generator", f"为子任务 {current} 生成 SQL"
        return "generator", f"子任务 {current} 还没有 SQL，不能直接去 {want}"

    # ---------- ⑤ 静态防线判定为禁止 —— 永远不进执行 ----------
    if verdict.get("level") == "禁止":
        return "reviewer", f"静态防线判定禁止：{'；'.join(verdict.get('reasons') or [])}"

    # ---------- ⑥ 还没做验证 ----------
    if checks is None:
        if want in (None, "validator"):
            return "validator", "SQL 必须经过验证才能执行"
        return "validator", f"SQL 还没验证，不能直接去 {want}"

    # ---------- ⑦ 验证不通过 —— 让模型选：小修 还是 重新理解 ----------
    if not checks.get("passed"):
        candidates = {"fixer", "planner"}
        if want in candidates:
            return want, f"验证不通过，模型选择交给{_cn(want)}"
        return "fixer", "验证不通过，退回修正 SQL"

    # ---------- ⑧ 验证通过但还需要人工确认 ----------
    if _needs_confirm(verdict) and not state.get("confirmed"):
        return "executor", "验证通过，但属于需确认操作，执行器会先暂停等用户确认"

    # ---------- ⑨ 还没执行 ----------
    if result is None:
        if want in (None, "executor"):
            return "executor", "验证通过，可以执行"
        return "executor", f"SQL 已通过验证，下一步应当执行而不是 {want}"

    # ---------- ⑩ 执行失败 —— 让模型选：改 SQL 还是重新拆问题 ----------
    if not result.get("ok"):
        candidates = {"fixer", "planner"}
        if want in candidates:
            return want, f"执行失败，模型选择交给{_cn(want)}"
        return "fixer", "执行失败，退回修正 SQL"

    # ---------- ⑪ 执行成功，推进到下一个子任务 ----------
    return "generator", f"子任务 {current} 已完成，继续下一个"


def _cn(name: str) -> str:
    return {"planner": "规划器", "generator": "生成智能体", "validator": "验证智能体",
            "executor": "执行智能体", "fixer": "修正智能体", "reviewer": "汇总审查器",
            "done": "结束"}.get(name, name)


def route_target(state: TaskState, max_rounds: int) -> str:
    """LangGraph 条件边用：返回节点名或 'done'。"""
    nxt, reason = decide(state, max_rounds)
    state["route_reason"] = reason  # 便于调试与展示（不依赖它做副作用）
    return nxt if nxt in AGENT_DEFAULT else "done"
