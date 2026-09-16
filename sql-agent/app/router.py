"""路由护栏：**模型决定「想」去哪，运行时决定「能」去哪。**

这是「链路不固定」的落点。每个智能体在结构化输出里给一个 `handoff_to`
（它自己建议下一步交给谁），但路由器先用状态算出**候选集**：

  · 候选集只有一个元素时 —— 直接去，模型的建议无效（硬边界）
  · 候选集有多个元素时   —— 让模型在合法范围内自己选（真正的协作自由度）

于是：
  · 不同问题会走不同路径（简单问题一条 SQL 直达；复杂问题可能反复修正、
    甚至退回重新规划），**链路确实不固定**；
  · 但「没校验就不能执行」「禁止语句永不执行」这些边界一条都破不了。

候选集为多的两个地方，正是真实存在的分歧点：
  · 校验不通过 —— 小修（fixer）还是重新理解问题（planner）？
  · 执行失败   —— 改 SQL（fixer）还是重新拆问题（planner）？
"""

from __future__ import annotations

from .state import TaskState, now_hash

# 用户意图模式的中文名（显示用）
MODE_CN: dict[str, str] = {
    "chat": "打招呼 / 闲聊",
    "execute": "执行",
}


def _needs_confirm(verdict: dict) -> bool:
    """需确认的判断加一层兜底：即使 verdict 里没写 needs_confirm，看 level 也能认出来。"""
    return bool(verdict.get("needs_confirm")) or verdict.get("level") == "需确认"


def _stall_reason(state: TaskState) -> str | None:
    """检测「修正没有进展」，有就返回原因。

    这是之前**漏掉的一环**：修正智能体反复生成同一类 SQL，
    系统只是把轮次耗尽，而且没有任何人察觉。
    现在明确识别两种情况并主动停下来：

      · 同一条 SQL（hash 相同）已经失败 ≥2 次 —— 修正没有产生新内容
      · 同一个错误特征连续出现 ≥2 次 —— 修正没有解决根因
    """
    bad = [a for a in (state.get("attempts") or []) if not a.get("ok")]
    if not bad:
        return None
    cur_hash = now_hash((state.get("draft") or {}).get("sql", ""))
    same_sql = [a for a in bad if a.get("sql_hash") == cur_hash]
    if len(same_sql) >= 2:
        return f"同一条 SQL 已经失败 {len(same_sql)} 次，修正没有产生新内容"
    sigs = [a.get("error_sig") for a in bad if a.get("error_sig")]
    if sigs:
        same_err = sum(1 for s in sigs if s == sigs[-1])
        if same_err >= 2:
            return f"同一个错误已经连续出现 {same_err} 次，修正没有解决根因"
    return None


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

    # ---------- ⓪'' 修正智能体判定「修不了」 ----------
    if state.get("give_up"):
        return "reviewer", f"修正智能体判断无法修复：{str(state['give_up'])[:48]}"

    # ---------- ⓪''' 打招呼 / 闲聊 —— 不进生成，直接给答复 ----------
    if state.get("mode") == "chat":
        return "reviewer", "打招呼 / 闲聊，不需要动数据库，直接答复"

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

    # ---------- ④ 刚做完一个子任务 —— 先回规划器**复查**（循环的落点）----------
    #
    # 这是「后面的计划要根据前面执行过的结果评估」的落点：
    # 每跑完一个子任务，就把规划器叫回来看**实际结果**，由它决定
    # 继续做 / 调整剩余任务 / 到此为止 —— 而不是闷头把剩下的任务按老计划跑完。
    #
    # 三个条件缺一不可：
    #   · 游标已经往前走过了（cursor > 0，说明确实有子任务跑完）；
    #   · 后面还有任务（cursor < len(intents)，全做完就走 ② 汇总）；
    #   · **这个位置还没复查过**（`replanned_cursor`）—— 不然复查完回来又满足前两条，
    #     就会在原地无限绕圈。
    # 还要求上一步是「执行成功」：失败该走修正/重新规划，不该当成"做完了"。
    if (cursor > 0 and cursor < len(intents)
            and int(state.get("replanned_cursor", -1)) != cursor
            and (result or {}).get("ok")):
        return "planner", (f"子任务 {intents[cursor - 1].get('sub_task_id')} 已执行完成，"
                           f"回到规划器复查剩余 {len(intents) - cursor} 个任务是否仍成立")

    # ---------- ⑤ 当前子任务还没有 SQL ----------
    # SQL **只有生成器能产出**（没有任何"用户贴 SQL"的入口），所以这里固定去生成器。
    current = intents[cursor].get("sub_task_id")
    if not draft or draft.get("sub_task_id") != current:
        done_ids = [i.get("sub_task_id") for i in intents[:cursor]]
        if draft and draft.get("sub_task_id") in done_ids:
            # 这是**正常推进**：上一条 SQL 属于已经做完的子任务，现在该做下一个了。
            # （跟"标注错了"要分开说 —— 不然每一步都像出了故障。）
            return "generator", (f"子任务 {draft.get('sub_task_id')} 已完成，"
                                 f"接着为 {current} 生成 SQL")
        if draft and draft.get("sub_task_id") != current:
            # 有 SQL，但标注的子任务对不上 —— 说清楚，别让人以为是"还没生成"
            return "generator", (f"当前 SQL 标注的是子任务 {draft.get('sub_task_id')}，"
                                 f"与待办的 {current} 不一致，重新生成")
        if want in (None, "generator", "planner"):
            return "generator", f"为子任务 {current} 生成 SQL"
        return "generator", (f"子任务 {current} 还没有 SQL ⇒ 护栏改去生成器"
                             f"（模型想交给{_cn(want)}）")

    # ---------- ⑥ 静态防线判定为禁止 —— 永远不进执行 ----------
    if verdict.get("level") == "禁止":
        return "reviewer", f"静态防线判定禁止：{'；'.join(verdict.get('reasons') or [])}"

    # ---------- ⑦ 还没做校验 ----------
    if checks is None:
        if want in (None, "validator"):
            return "validator", "SQL 必须经过校验才能执行"
        return "validator", (f"SQL 还没校验，不能跳过校验 ⇒ 护栏改去校验器"
                             f"（模型想交给{_cn(want)}）")

    # ---------- ⑧ 校验不通过 —— 先看有没有进展，再让模型选 ----------
    if not checks.get("passed"):
        stall = _stall_reason(state)
        if stall:
            return "reviewer", f"停止重试：{stall}"
        candidates = {"fixer", "planner"}
        if want in candidates:
            return want, f"校验不通过，模型选择交给{_cn(want)}"
        return "fixer", "校验不通过，退回修正 SQL"

    # ---------- ⑧' 保险丝：只有 execute 才允许走到执行 ----------
    #
    # 现在只有 chat / execute 两种模式，chat 在前面就被拦去汇总了，这条平时不会触发。
    # 留着是因为它守的是一条**硬性质**：任何非执行意图都不许落进执行器。
    # 以后再加模式时，忘了在这里处理也不会漏。
    mode = state.get("mode") or "execute"
    if mode != "execute":
        return "reviewer", (f"{MODE_CN.get(mode, mode)}：不做执行，"
                            f"校验通过后直接给结论")

    # ---------- ⑨ 校验通过但还需要人工确认 ----------
    if _needs_confirm(verdict) and not state.get("confirmed"):
        return "executor", "校验通过，但属于需确认操作，执行器会先暂停等用户确认"

    # ---------- ⑩ 还没执行 ----------
    if result is None:
        if want in (None, "executor"):
            return "executor", "校验通过，可以执行"
        return "executor", (f"SQL 已通过校验，下一步应当执行 ⇒ 护栏改去执行器"
                            f"（模型想交给{_cn(want)}）")

    # ---------- ⑪ 执行失败 —— 先看有没有进展，再让模型选 ----------
    if not result.get("ok"):
        stall = _stall_reason(state)
        if stall:
            return "reviewer", f"停止重试：{stall}"
        candidates = {"fixer", "planner"}
        if want in candidates:
            return want, f"执行失败，模型选择交给{_cn(want)}"
        return "fixer", "执行失败，退回修正 SQL"

    # ---------- ⑫ 执行成功，推进到下一个子任务 ----------
    # 走到这里说明「④ 的复查已经在当前位置做过了」（否则上面 ④ 就先拦下了），
    # 所以直接去生成器接着做下一个。
    return "generator", f"子任务 {current} 已完成，继续下一个"


def _cn(name: str) -> str:
    return {"planner": "规划器", "generator": "生成智能体", "validator": "校验智能体",
            "executor": "执行智能体", "fixer": "修正智能体", "reviewer": "汇总审查器",
            "done": "结束"}.get(name, name)
