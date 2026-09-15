"""离线兜底：LLM_MODE=mock 时的预置输出。

行为与真实模式一致：只能在给定表结构内选表和字段，
生成的 SQL 刻意**安全**（删除条件永远匹配不到数据），
但走的是完全相同的链路 —— 静态防线、EXPLAIN、执行、人工确认一个都不少。
"""

from __future__ import annotations

from .state import (
    ExecutorOutput,
    FixerOutput,
    GeneratorOutput,
    PlannerOutput,
    QueryIntent,
    ReviewerOutput,
    SqlCheck,
    SqlDraft,
    ValidatorOutput,
)

# 关键词 → 表（只为让离线模式能挑一张合理的表）
HINTS: dict[str, list[str]] = {
    "customers": ["客户", "用户", "会员", "customer", "北京", "上海", "城市"],
    "products": ["商品", "产品", "库存", "品类", "价格", "product", "stock"],
    "order_items": ["明细", "条目", "买了几件", "item"],
    "orders": ["订单", "金额", "总额", "下单", "order", "已取消", "已发货"],
}


def _pick_table(question: str, allowed: set[str]) -> str:
    for table, words in HINTS.items():
        if table in allowed and any(w in question for w in words):
            return table
    for t in ("orders", "customers", "products", "order_items"):
        if t in allowed:
            return t
    return sorted(allowed)[0] if allowed else "orders"


def _pick_kind(question: str) -> str:
    if any(w in question for w in ("删除", "删掉", "清理", "delete")):
        return "删除"
    if any(w in question for w in ("修改", "更新", "改成", "标记", "update", "insert", "新增")):
        return "修改"
    return "查询"


# ---------------------------------------------------------------- Planner

def mock_planner(question: str, schema: dict) -> PlannerOutput:
    allowed = set(schema.get("allowed") or set())
    table = _pick_table(question, allowed)
    kind = _pick_kind(question)
    return PlannerOutput(
        understanding=f"用户想对 {table} 表做一次「{kind}」操作",
        plan_reason="问题只需要一次数据操作，因此只拆一个子任务；"
                    "若涉及多表关联，生成智能体会用 JOIN 处理。",
        intents=[QueryIntent(sub_task_id="st-1", intent=question[:60] or f"{kind} {table}",
                             kind=kind, tables=[table], depends_on=[])],
        handoff_to="generator",
    )


# ---------------------------------------------------------------- Generator

def _safe_sql(table: str, kind: str, schema: dict) -> tuple[str, str]:
    cols = [c["name"] for c in (schema.get("tables", {}).get(table, {}).get("columns") or [])]
    pk = (schema.get("tables", {}).get(table, {}).get("pk") or ["id"])[0]
    if kind == "删除":
        return (f"DELETE FROM {table} WHERE {pk} = -1",
                f"删除条件刻意用不存在的 {pk}，离线模式下不会真的删掉数据")
    if kind == "修改":
        target = next((c for c in cols if c not in (pk,)), pk)
        return (f"UPDATE {table} SET {target} = {target} WHERE {pk} = -1",
                f"用 {pk} = -1 限定范围，离线模式下不会真的改到数据")
    return (f"SELECT * FROM {table} LIMIT 20", "只读查询，限制 20 行")


def mock_generator(intent: dict, schema: dict, allowed: set[str]) -> GeneratorOutput:
    table = (intent.get("tables") or [None])[0]
    if table not in allowed:
        table = _pick_table(intent.get("intent", ""), allowed)
    kind = intent.get("kind") or "查询"
    sql, note = _safe_sql(table, kind, schema)
    return GeneratorOutput(
        draft=SqlDraft(sub_task_id=intent.get("sub_task_id", "st-1"),
                       intent=intent.get("intent", ""), sql=sql, note=note),
        handoff_to="validator",
    )


# ---------------------------------------------------------------- Validator

def mock_validator(v, runtime_checks: list[dict], intent: dict) -> ValidatorOutput:
    """只做「结构自查」：运行时检查过了就说通过。

    如果运行时 / EXPLAIN 有 fail，模型再乐观也没用 —— 硬门禁会强制否决。
    """
    fails = [c for c in runtime_checks if c.get("result") == "fail"]
    return ValidatorOutput(
        passed=not fails,
        checks=[SqlCheck(check_id=f"m-{i + 1}", item=c["item"],
                         result="pass", detail="模型侧检查通过")
                for i, c in enumerate(runtime_checks)],
        reason="运行时检查通过，模型侧未发现语义问题" if not fails
               else "运行时检查未通过：" + "；".join(c.get("detail", "") for c in fails),
        handoff_to="executor" if not fails else "fixer",
    )


# ---------------------------------------------------------------- Executor

def mock_executor(exec_result: dict) -> ExecutorOutput:
    ok = bool(exec_result.get("ok"))
    kind = exec_result.get("kind")
    if not ok:
        return ExecutorOutput(ok=False, summary=f"执行失败：{exec_result.get('error', '')[:60]}",
                              error_hint=exec_result.get("error", ""),
                              handoff_to="fixer")
    if kind == "rows":
        n = exec_result.get("rowcount", 0)
        return ExecutorOutput(ok=True, summary=f"查询返回 {n} 行", handoff_to="reviewer")
    n = exec_result.get("rowcount", 0)
    return ExecutorOutput(ok=True, summary=f"影响 {n} 行", handoff_to="reviewer")


# ---------------------------------------------------------------- Fixer

def mock_fixer(draft: dict, checks: dict, result: dict,
               schema: dict, allowed: set[str]) -> FixerOutput:
    sql = draft.get("sql", "")
    fixed = sql
    changed = "按失败原因调整了 SQL"
    if "LIMIT" not in sql.upper() and sql.strip().upper().startswith("SELECT"):
        fixed = f"{sql} LIMIT 20"
        changed = "查询补上了 LIMIT，避免全表返回"
    elif sql.strip().upper().startswith(("UPDATE", "DELETE")) and " WHERE " not in sql.upper():
        fixed = sql
        changed = "保持原样（无 WHERE 的操作本来就需要人工确认，不能自动改条件）"
    return FixerOutput(
        draft=SqlDraft(sub_task_id=draft.get("sub_task_id", "st-1"),
                       intent=draft.get("intent", ""), sql=fixed, note=changed),
        what_changed=changed, handoff_to="validator",
    )


# ---------------------------------------------------------------- Reviewer

def mock_reviewer(report, history: list[dict], question: str) -> ReviewerOutput:
    done = [h for h in history if h.get("executed")]
    lines = [f"针对「{question}」，共处理 {len(done)} 个数据操作："]
    for h in done:
        if h.get("kind") == "cancelled":
            lines.append(f"· {h.get('sub_task_id')}：你取消了执行，未做任何改动")
        elif h.get("exec_ok"):
            lines.append(f"· {h.get('sub_task_id')}：{h.get('intent')} —— "
                         f"影响 {h.get('affected_rows')} 行（{h.get('action')}）")
        else:
            lines.append(f"· {h.get('sub_task_id')}：执行失败 —— {h.get('error')}")
    if not done:
        lines.append("· 没有实际执行任何数据操作")
    text = "\n".join(lines)
    return ReviewerOutput(
        decision="veto" if not report.passed else "approve",
        final_answer=text,
        checks=[SqlCheck(check_id=f"r-{i + 1}", item=r.title,
                         result=r.result, detail=r.detail)
                for i, r in enumerate(report.results)],
        reason="一致性校验全部通过" if report.passed else "存在不变量未通过",
        handoff_to="done",
    )
