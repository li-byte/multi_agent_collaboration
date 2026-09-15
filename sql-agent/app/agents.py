"""六个智能体节点。

    planner    规划器     —— 读表结构，把问题拆成查询意图
    generator  生成智能体 —— 为当前子任务生成 SQL
    validator  验证智能体 —— 静态防线 + EXPLAIN + 语义校验
    executor   执行智能体 —— 在受限连接上执行；需确认时 interrupt 暂停等人工
    fixer      修正智能体 —— 拿失败原因重写 SQL
    reviewer   汇总审查器 —— 汇总结果 + 硬门禁 + 给用户的答复

协作链路**不固定**：每个节点只负责"说话"和"建议下一步交给谁"（`next_agent`），
真正走哪条路由 `router` 节点用运行时护栏决定。
"""

from __future__ import annotations

import json

from langgraph.types import interrupt

from . import mock_llm, schema_info, sql_guard, validators
from .router import decide
from .state import (
    ExecutorOutput,
    FixerOutput,
    GeneratorOutput,
    PlannerOutput,
    ReviewerOutput,
    ValidatorOutput,
    now_hash,
    one_line,
)


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


_CONTRACT_HINT = (
    "\n输出要求：嵌套字段必须是对象/数组本身，不要序列化成 JSON 字符串；列表字段输出真正的数组。\n"
    "SQL 要求：只输出**一条**完整 SQL；不要以分号结尾；不要写注释；不要写多语句。\n"
    "最后必须给出 handoff_to —— 你建议下一步交给谁（这只是建议，运行时护栏会校验）。"
)


# ---------------------------------------------------------------- 公共骨架

async def _call(rt, schema, system: str, user: str, mock_fn):
    """结构化调用，失败重试 1 次。返回 (output, attempts, error)。"""
    if rt.llm is None:
        return mock_fn(), 1, None
    bound = rt.structured(schema)
    messages = [("system", system + _CONTRACT_HINT), ("human", user)]
    last_err: Exception | None = None
    for attempt in (1, 2):
        try:
            out = await bound.ainvoke(messages)
            if out is not None:
                return out, attempt, None
            last_err = RuntimeError("模型返回空结果")
        except Exception as exc:  # noqa: BLE001
            last_err = exc
    return None, 2, last_err


async def _claim(rt, state, role: str, action: str, attempt: int = 1) -> str:
    run_id = state["global_task_id"]
    round_no = state.get("round", 0)
    cursor = state.get("cursor", 0)
    sub_task_id = f"{role}-r{round_no}-c{cursor}"
    await rt.ledger.open_task(
        run_id=run_id, sub_task_id=sub_task_id, attempt=attempt, role=role, agent_id=role,
        context={"question": state.get("question", ""), "cursor": cursor,
                 "sub_task_id": (state.get("draft") or {}).get("sub_task_id")},
        idempotency_key=f"{run_id}:r{round_no}:c{cursor}:{role}:{action}:a{attempt}",
        lease_seconds=rt.settings.lease_seconds,
    )
    return sub_task_id


async def _settle_attempts(rt, state, role: str, action: str, attempts: int) -> int:
    if attempts <= 1:
        return 1
    run_id = state["global_task_id"]
    round_no = state.get("round", 0)
    sub_task_id = f"{role}-r{round_no}-c{state.get('cursor', 0)}"
    await rt.ledger.finish_task(run_id, sub_task_id, 1, "failed",
                                error="结构化输出未通过契约校验，已重试")
    await _claim(rt, state, role, action, attempt=2)
    return 2


async def _fail(rt, state, role: str, sub_task_id: str, version: int, err) -> None:
    run_id = state["global_task_id"]
    message = str(err)[:500]
    await rt.ledger.finish_task(run_id, sub_task_id, 1, "failed", error=message)
    await rt.emit(run_id, role, "error", {"where": role, "message": message},
                  version=version, round_no=state.get("round", 0))


async def _degrade(rt, state, role: str, sub_task_id: str, version: int, err) -> dict:
    """LLM 失败时不炸掉整个任务 —— 如实记下来，交给汇总审查器给用户一个交代。

    一个智能体挂了，用户应该看到「哪里没做成」，而不是一个 500。
    """
    await _fail(rt, state, role, sub_task_id, version, err)
    return {"error": f"{role} 没能完成：{err}", "status": "failed",
            "last_agent": role, "next_agent": "reviewer"}


def _current_intent(state) -> dict:
    intents = list(state.get("intents") or [])
    cursor = int(state.get("cursor", 0))
    return intents[cursor] if 0 <= cursor < len(intents) else {}


def _descriptor(state) -> dict:
    """当前这条 SQL 的**描述信息**（每次生成/修正后都会变）。"""
    draft = state.get("draft") or {}
    v = state.get("verdict") or {}
    return {
        "sub_task_id": draft.get("sub_task_id") or _current_intent(state).get("sub_task_id"),
        "round": state.get("round", 0),
        "intent": draft.get("intent", ""),
        "sql": draft.get("sql", ""),
        "sql_hash": now_hash(draft.get("sql", "")),
        "risk_level": v.get("level"),
        "action": v.get("action"),
        "tables": v.get("tables") or [],
        "need_confirm": bool(v.get("needs_confirm")) or v.get("level") == "需确认",
    }


def _upsert_history(state, **lifecycle) -> list[dict]:
    """把当前 SQL 的状态合并进 history：同一个 sub_task_id 只留一条。

    修正后 SQL 变了（round 变了）就把生命周期字段重置，避免把上一版的
    「执行过」状态带到新 SQL 上 —— 那会让覆盖率校验失真。
    """
    desc = _descriptor(state)
    sid = desc["sub_task_id"]
    hist = [dict(h) for h in (state.get("history") or [])]
    old = next((h for h in hist if h.get("sub_task_id") == sid), {})
    merged = {**old, **desc}
    if desc["round"] != old.get("round"):
        merged.update({"validated": False, "validated_passed": None,
                       "executed": False, "exec_ok": None, "affected_rows": None,
                       "confirmed_by": None, "error": None, "kind": None})
    merged.update(lifecycle)
    hist = [h for h in hist if h.get("sub_task_id") != sid]
    hist.append(merged)
    return hist


# ---------------------------------------------------------------- 规划器

async def planner(state, rt) -> dict:
    run_id = state["global_task_id"]
    version = state.get("version", 0)
    sub_task_id = f"planner-r{state.get('round', 0)}-c0"

    await rt.emit(run_id, "planner", "node_start",
                  {"role": "planner", "sub_task_id": sub_task_id}, version=version)
    await _claim(rt, state, "planner", "plan")

    system = (
        "你是规划器。用户会用自然语言提一个关于业务数据库的问题。\n"
        "你的任务：看懂问题，把它拆成 1-4 个数据操作意图。\n"
        "硬性要求：\n"
        "1. 每个意图有稳定唯一的 sub_task_id；\n"
        "2. kind 只能是 查询 / 修改 / 删除；\n"
        "3. tables 只能从下面给出的真实表名里选，绝对不许编造表名；\n"
        "4. 有依赖关系的写 depends_on；\n"
        "5. 拆解必须覆盖用户问题的全部方面，不要漏。\n"
        "如果用户的问题根本不需要动数据库（例如闲聊），也要如实说明。"
    )
    user = (
        f"用户的问题：{state.get('question', '')}\n\n"
        f"数据库里可访问的表（只能引用这些）：\n{state.get('schema_text', '')}\n\n"
        "请拆解。"
    )
    out, attempts, err = await _call(
        rt, PlannerOutput, system, user,
        lambda: mock_llm.mock_planner(state.get("question", ""), rt.schema))
    if err is not None or out is None:
        return await _degrade(rt, state, "planner", sub_task_id, version, err)

    attempt = await _settle_attempts(rt, state, "planner", "plan", attempts)
    intents = [i.model_dump() for i in out.intents]
    await rt.ledger.finish_task(run_id, sub_task_id, attempt, "completed",
                                result_ref=f"plan-{len(intents)}")
    new_version = await rt.ledger.set_run_status(run_id, "planning", 0) or version
    await rt.emit(run_id, "planner", "plan",
                  {"understanding": out.understanding, "plan_reason": out.plan_reason,
                   "intents": intents, "refusal": out.refusal, "attempts": attempts},
                  version=version, round_no=0)
    await rt.emit(run_id, "planner", "node_end",
                  {"summary": (f"拒绝执行：{one_line(out.refusal)[:50]}" if out.refusal
                               else f"拆出 {len(intents)} 个数据操作意图"),
                   "suggests": out.handoff_to},
                  version=version, round_no=0)

    return {"intents": intents, "refusal": out.refusal or "", "cursor": 0,
            "understanding": out.understanding,
            "status": "planning", "version": new_version, "last_agent": "planner",
            "next_agent": out.handoff_to or ("reviewer" if out.refusal else "generator")}


# ---------------------------------------------------------------- 生成智能体

async def generator(state, rt) -> dict:
    run_id = state["global_task_id"]
    version = state.get("version", 0)
    round_no = state.get("round", 0)
    intent = _current_intent(state)
    sub_task_id = f"generator-r{round_no}-c{state.get('cursor', 0)}"

    await rt.emit(run_id, "generator", "node_start",
                  {"role": "generator", "sub_task_id": sub_task_id,
                   "intent": intent.get("intent", "")}, version=version, round_no=round_no)
    await _claim(rt, state, "generator", "generate")

    system = (
        "你是 SQL 生成智能体。针对给出的**一个**数据操作意图，写出一条 PostgreSQL SQL。\n"
        "硬性要求：\n"
        "1. 只能使用给定的表和字段，表名/字段名必须**逐字**与表结构一致；\n"
        "2. 只写一条语句，不要多条，不要分号结尾；\n"
        "3. 禁止任何 DDL（DROP/TRUNCATE/ALTER/CREATE 等）；\n"
        "4. 删除和修改必须带 WHERE 条件，除非用户明确要求操作全表；\n"
        "5. 查询建议加 LIMIT；\n"
        "6. note 里说明你的 JOIN / 聚合 / 条件依据。"
    )
    user = (
        f"用户的问题：{state.get('question', '')}\n\n"
        f"本次要完成的意图（sub_task_id={intent.get('sub_task_id')}）："
        f"{intent.get('intent')}（{intent.get('kind')}）\n\n"
        f"可用的表结构：\n{state.get('schema_text', '')}\n\n"
        "请写出这一条 SQL。"
    )
    out, attempts, err = await _call(
        rt, GeneratorOutput, system, user,
        lambda: mock_llm.mock_generator(intent, rt.schema, rt.allowed_tables))
    if err is not None or out is None:
        return await _degrade(rt, state, "generator", sub_task_id, version, err)

    attempt = await _settle_attempts(rt, state, "generator", "generate", attempts)
    draft = out.draft.model_dump()
    draft["round"] = round_no
    v = sql_guard.analyze(draft["sql"], rt.allowed_tables)
    verdict = {"level": v.level, "action": v.action, "tables": v.tables,
               "reasons": v.reasons, "notes": v.notes, "statements": v.statements,
               "needs_confirm": v.needs_confirm}

    await rt.ledger.finish_task(run_id, sub_task_id, attempt, "completed",
                                result_ref=f"draft-{draft.get('sub_task_id')}")
    await rt.ledger.audit_sql(
        run_id, round_no, draft.get("sub_task_id", ""), "generated",
        draft["sql"], now_hash(draft["sql"]), intent=draft.get("intent"),
        risk_level=v.level, action=v.action, tables=v.tables, need_confirm=v.needs_confirm)

    new_version = await rt.ledger.set_run_status(run_id, "generating", round_no) or version
    await rt.emit(run_id, "generator", "sql",
                  {"draft": draft, "verdict": verdict, "stage": "generated",
                   "attempts": attempts}, version=version, round_no=round_no)
    await rt.emit(run_id, "generator", "node_end",
                  {"summary": f"生成 SQL（{v.action} · 风险 {v.level}）",
                   "suggests": out.handoff_to}, version=version, round_no=round_no)

    return {"draft": draft, "verdict": verdict, "checks": None, "result": None,
            "confirmed": False, "status": "generating", "version": new_version,
            "last_agent": "generator", "next_agent": out.handoff_to or "validator"}


# ---------------------------------------------------------------- 验证智能体

async def validator(state, rt) -> dict:
    run_id = state["global_task_id"]
    version = state.get("version", 0)
    round_no = state.get("round", 0)
    draft = state.get("draft") or {}
    sql = draft.get("sql", "")
    sub_task_id = f"validator-r{round_no}-c{state.get('cursor', 0)}"
    intent = _current_intent(state)

    await rt.emit(run_id, "validator", "node_start",
                  {"role": "validator", "sub_task_id": sub_task_id}, version=version, round_no=round_no)
    await _claim(rt, state, "validator", "validate")

    # ① 运行时确定性检查：静态防线
    v = sql_guard.analyze(sql, rt.allowed_tables)
    runtime_checks = [{
        "check_id": "rt-static", "item": "静态防线（危险语句 / 多语句 / 表名授权）",
        "result": "pass" if v.ok else "fail",
        "detail": ("；".join(v.reasons) if v.reasons else
                   f"{v.action} · 风险 {v.level}" + ("；" + "；".join(v.notes) if v.notes else "")),
    }]

    # ② 运行时确定性检查：EXPLAIN（不执行）—— PostgreSQL 会做完整语法与语义分析
    explain: dict = {}
    if v.ok:
        explain = await rt.db.explain(sql)
        if explain.get("ok"):
            node_type = explain.get("node_type") or "?"
            est = explain.get("est_rows")
            runtime_checks.append({
                "check_id": "rt-explain", "item": "语法与语义（表/字段是否存在、类型是否匹配）",
                "result": "pass",
                "detail": f"执行计划节点 {node_type}，预估影响 {est} 行",
            })
        else:
            runtime_checks.append({
                "check_id": "rt-explain", "item": "语法与语义（表/字段是否存在、类型是否匹配）",
                "result": "fail", "detail": explain.get("error", "EXPLAIN 失败"),
            })
    else:
        runtime_checks.append({
            "check_id": "rt-explain", "item": "语法与语义", "result": "fail",
            "detail": "静态防线未通过，跳过 EXPLAIN",
        })

    runtime_passed = all(c["result"] == "pass" for c in runtime_checks)

    # ③ 模型语义校验：这条 SQL 是否真的实现了意图
    system = (
        "你是 SQL 验证智能体。判断这条 SQL 是否能正确、安全地完成指定意图。\n"
        "硬性要求：\n"
        "1. 逐条检查：表/字段是否正确、JOIN 条件是否合理、聚合与 GROUP BY 是否配套、"
        "WHERE 条件是否覆盖了意图里的限定；\n"
        "2. **只要运行时检查里有任何一条 fail，你必须判 passed=false**；\n"
        "3. 你的判断只能比运行时更严格，不能更宽松；\n"
        "4. reason 说明结论理由。"
    )
    explain_brief = {"ok": explain.get("ok"), "error": explain.get("error"),
                     "node_type": explain.get("node_type"), "est_rows": explain.get("est_rows")}
    user = (
        f"用户的问题：{state.get('question', '')}\n"
        f"本次意图：{intent.get('intent')}（{intent.get('kind')}）\n\n"
        f"待验证 SQL：\n{sql}\n\n"
        f"表结构：\n{state.get('schema_text', '')}\n\n"
        f"运行时确定性检查结果（不是你主观判断，是系统实测）：\n{_dump(runtime_checks)}\n"
        f"EXPLAIN 结果：{_dump(explain_brief)}\n"
    )
    out, attempts, err = await _call(
        rt, ValidatorOutput, system, user,
        lambda: mock_llm.mock_validator(v, runtime_checks, intent))
    if err is not None or out is None:
        return await _degrade(rt, state, "validator", sub_task_id, version, err)

    attempt = await _settle_attempts(rt, state, "validator", "validate", attempts)

    # ④ 硬门禁：运行时没过，模型说通过也不算
    model_checks = [c.model_dump() for c in out.checks]
    passed = bool(out.passed) and runtime_passed
    forced = (not runtime_passed) and bool(out.passed)
    checks = {
        "passed": passed,
        "forced_by_runtime": forced,
        "reason": out.reason or ("运行时检查未通过" if not runtime_passed else "验证通过"),
        "checks": runtime_checks + model_checks,
    }

    await rt.ledger.finish_task(run_id, sub_task_id, attempt, "completed",
                                result_ref=f"check-{'pass' if passed else 'fail'}")
    await rt.ledger.audit_sql(
        run_id, round_no, draft.get("sub_task_id", ""),
        "validated" if passed else "rejected",
        sql, now_hash(sql), intent=draft.get("intent"),
        risk_level=v.level, action=v.action, tables=v.tables,
        est_rows=explain.get("est_rows"), need_confirm=v.needs_confirm)

    new_version = await rt.ledger.set_run_status(run_id, "validating", round_no) or version
    for c in checks["checks"]:
        await rt.emit(run_id, "validator", "check", c, version=version, round_no=round_no)
    await rt.emit(run_id, "validator", "verdict",
                  {"passed": passed, "reason": checks["reason"],
                   "forced_by_runtime": forced, "static": checks["checks"][0].get("detail")},
                  version=version, round_no=round_no)
    await rt.emit(run_id, "validator", "node_end",
                  {"summary": "验证通过" if passed else "验证不通过",
                   "suggests": out.handoff_to}, version=version, round_no=round_no)

    # 记录本轮验证结论（同一个 sub_task_id 增量合并，不覆盖历史）
    return {"checks": checks, "explain": explain, "status": "validating",
            "version": new_version,
            "history": _upsert_history(state, validated=True, validated_passed=passed),
            "last_agent": "validator",
            "next_agent": out.handoff_to or ("executor" if passed else "fixer")}


# ---------------------------------------------------------------- 执行智能体

async def executor(state, rt) -> dict:
    run_id = state["global_task_id"]
    version = state.get("version", 0)
    round_no = state.get("round", 0)
    draft = state.get("draft") or {}
    sql = draft.get("sql", "")
    verdict = state.get("verdict") or {}

    # ============ 人工协作：需确认的操作，先暂停 ============
    # interrupt 之前的代码在恢复后**会重跑**，所以这里刻意不做任何副作用 ——
    # 「等待确认」的事件与审计由编排器在捕获 __interrupt__ 时统一落账。
    if (bool(verdict.get("needs_confirm")) or verdict.get("level") == "需确认") \
            and not state.get("confirmed"):
        decision = interrupt({
            "type": "confirm_sql",
            "sub_task_id": draft.get("sub_task_id"),
            "intent": draft.get("intent"),
            "sql": sql,
            "risk_level": verdict.get("level"),
            "action": verdict.get("action"),
            "tables": verdict.get("tables"),
            "est_rows": (state.get("explain") or {}).get("est_rows"),
            "notes": verdict.get("notes") or [],
            "cascade": schema_info.cascade_note(rt.schema, verdict.get("tables") or []),
            "question": "这条会删除/修改数据，需要你确认后才执行。",
        })
        approved = (bool(decision.get("approve")) if isinstance(decision, dict)
                    else decision == "approve")
        approved_by = (decision or {}).get("by") if isinstance(decision, dict) else None
        if not approved:
            intents = list(state.get("intents") or [])
            cursor = int(state.get("cursor", 0))
            return {"result": {"ok": True, "kind": "cancelled", "rowcount": 0,
                               "summary": "用户取消了执行"},
                    "confirmed": False,
                    "cursor": min(cursor + 1, len(intents)),
                    "history": _upsert_history(state, executed=True, exec_ok=True,
                                               kind="cancelled", affected_rows=0,
                                               confirmed_by=approved_by,
                                               note="用户取消了执行"),
                    "status": "executing", "last_agent": "executor",
                    "next_agent": "reviewer"}
    else:
        approved_by = None

    # ============ 正常执行 ============
    sub_task_id = f"executor-r{round_no}-c{state.get('cursor', 0)}"
    await rt.emit(run_id, "executor", "node_start",
                  {"role": "executor", "sub_task_id": sub_task_id,
                   "risk_level": verdict.get("level")}, version=version, round_no=round_no)
    await _claim(rt, state, "executor", "execute")

    exec_result = await rt.db.execute(sql, max_rows=rt.settings.max_rows)
    attempts = 1
    await rt.ledger.finish_task(
        run_id, sub_task_id, attempts, "completed" if exec_result["ok"] else "failed",
        result_ref=f"exec-{'ok' if exec_result['ok'] else 'err'}",
        error=None if exec_result["ok"] else str(exec_result.get("error"))[:500])

    await rt.ledger.audit_sql(
        run_id, round_no, draft.get("sub_task_id", ""),
        "executed" if exec_result["ok"] else "failed",
        sql, now_hash(sql), intent=draft.get("intent"),
        risk_level=verdict.get("level"), action=verdict.get("action"),
        tables=verdict.get("tables"), est_rows=(state.get("explain") or {}).get("est_rows"),
        affected_rows=exec_result.get("rowcount"),
        need_confirm=bool(verdict.get("needs_confirm")),
        confirmed_by=approved_by or ("用户已确认" if state.get("confirmed") else None),
        error=exec_result.get("error"), duration_ms=exec_result.get("duration_ms"))

    new_version = await rt.ledger.set_run_status(run_id, "executing", round_no) or version
    await rt.emit(run_id, "executor", "result", exec_result,
                  version=version, round_no=round_no)

    # ⑤ 让模型点评一下执行结果（是否真的完成了意图）
    system = (
        "你是 SQL 执行智能体。执行已经完成，请判断结果是否达成了本次意图。\n"
        "硬性要求：\n"
        "1. 查询返回 0 行不等于失败，可能是数据本来就没有；\n"
        "2. 影响行数为 0 的 UPDATE/DELETE 要提醒用户「条件可能没匹配到数据」；\n"
        "3. 失败时在 error_hint 里给出你判断的原因（越具体，修正智能体越好改）。"
    )
    user = (
        f"意图：{draft.get('intent')}\nSQL：{sql}\n\n"
        f"执行结果：{_dump({k: val for k, val in exec_result.items() if k != 'rows'})}\n"
        f"返回行数：{exec_result.get('rowcount')}\n"
        f"前几行样例：{_dump((exec_result.get('rows') or [])[:3])}\n"
    )
    out, _, err = await _call(rt, ExecutorOutput, system, user,
                              lambda: mock_llm.mock_executor(exec_result))
    if out is None:
        out = ExecutorOutput(ok=exec_result["ok"],
                             summary="执行完成" if exec_result["ok"] else "执行失败")

    entry_lifecycle = dict(
        executed=True, exec_ok=exec_result["ok"],
        confirmed_by=approved_by or ("用户已确认" if state.get("confirmed") else None),
        affected_rows=exec_result.get("rowcount"),
        duration_ms=exec_result.get("duration_ms"),
        error=exec_result.get("error"), kind=exec_result.get("kind"),
    )
    await rt.emit(run_id, "executor", "node_end",
                  {"summary": out.summary or ("执行成功" if exec_result["ok"] else "执行失败"),
                   "suggests": out.handoff_to}, version=version, round_no=round_no)

    # ⚠ 关键：执行成功后**推进游标**，否则路由会一直回到生成智能体，图就死循环了
    intents = list(state.get("intents") or [])
    cursor_now = int(state.get("cursor", 0))
    next_cursor = min(cursor_now + 1, len(intents)) if exec_result["ok"] else cursor_now

    return {"result": {**exec_result, "model_summary": out.summary,
                       "error_hint": out.error_hint},
            "confirmed": bool(state.get("confirmed")),
            "cursor": next_cursor,
            "history": _upsert_history(state, **entry_lifecycle),
            "status": "executing", "version": new_version, "last_agent": "executor",
            "next_agent": out.handoff_to or ("reviewer" if exec_result["ok"] else "fixer")}


# ---------------------------------------------------------------- 修正智能体

async def fixer(state, rt) -> dict:
    run_id = state["global_task_id"]
    version = state.get("version", 0)
    round_no = int(state.get("round", 0)) + 1
    draft = state.get("draft") or {}
    checks = state.get("checks") or {}
    result = state.get("result") or {}
    intent = _current_intent(state)
    sub_task_id = f"fixer-r{round_no}-c{state.get('cursor', 0)}"

    await rt.emit(run_id, "fixer", "node_start",
                  {"role": "fixer", "sub_task_id": sub_task_id,
                   "from_round": round_no - 1}, version=version, round_no=round_no)
    await _claim(rt, state, "fixer", "fix")

    failed = [c for c in (checks.get("checks") or []) if c.get("result") == "fail"]
    system = (
        "你是 SQL 修正智能体。上一条 SQL 没通过验证或执行失败，请修正它。\n"
        "硬性要求：\n"
        "1. 认真读失败原因，**针对原因**改，不要盲改；\n"
        "2. 只能在给定表结构内选表和字段；\n"
        "3. 仍然只输出一条 SQL，不要分号结尾，不要多条；\n"
        "4. 禁止任何 DDL；\n"
        "5. what_changed 里写清这次改了什么、为什么这样改能解决刚才的问题。"
    )
    user = (
        f"用户的问题：{state.get('question', '')}\n"
        f"本次意图：{intent.get('intent')}（{intent.get('kind')}）\n\n"
        f"上一版 SQL：\n{draft.get('sql', '')}\n\n"
        f"验证失败项：{_dump(failed) if failed else '（无）'}\n"
        f"验证结论：{checks.get('reason', '')}\n"
        f"执行错误：{result.get('error') or '（无）'}\n"
        f"执行侧判断：{result.get('error_hint') or '（无）'}\n\n"
        f"表结构：\n{state.get('schema_text', '')}\n\n"
        "请给出修正后的 SQL。"
    )
    out, attempts, err = await _call(
        rt, FixerOutput, system, user,
        lambda: mock_llm.mock_fixer(draft, checks, result, rt.schema, rt.allowed_tables))
    if err is not None or out is None:
        return await _degrade(rt, state, "fixer", sub_task_id, version, err)

    attempt = await _settle_attempts(rt, state, "fixer", "fix", attempts)
    new_draft = out.draft.model_dump()
    new_draft["round"] = round_no
    v = sql_guard.analyze(new_draft["sql"], rt.allowed_tables)
    verdict = {"level": v.level, "action": v.action, "tables": v.tables,
               "reasons": v.reasons, "notes": v.notes, "statements": v.statements,
               "needs_confirm": v.needs_confirm}

    await rt.ledger.finish_task(run_id, sub_task_id, attempt, "completed",
                                result_ref=f"fixed-{new_draft.get('sub_task_id')}")
    await rt.ledger.audit_sql(
        run_id, round_no, new_draft.get("sub_task_id", ""), "generated",
        new_draft["sql"], now_hash(new_draft["sql"]), intent=new_draft.get("intent"),
        risk_level=v.level, action=v.action, tables=v.tables, need_confirm=v.needs_confirm)

    new_version = await rt.ledger.set_run_status(run_id, "fixing", round_no) or version
    await rt.emit(run_id, "fixer", "sql",
                  {"draft": new_draft, "verdict": verdict, "stage": "fixed",
                   "what_changed": out.what_changed, "attempts": attempts},
                  version=version, round_no=round_no)
    await rt.emit(run_id, "fixer", "node_end",
                  {"summary": f"修正 SQL：{one_line(out.what_changed)[:60]}",
                   "suggests": out.handoff_to}, version=version, round_no=round_no)

    return {"draft": new_draft, "verdict": verdict, "checks": None, "result": None,
            "confirmed": False, "round": round_no, "status": "fixing",
            "version": new_version, "last_agent": "fixer",
            "next_agent": out.handoff_to or "validator"}


# ---------------------------------------------------------------- 汇总审查器

async def reviewer(state, rt) -> dict:
    run_id = state["global_task_id"]
    version = state.get("version", 0)
    round_no = state.get("round", 0)
    sub_task_id = f"reviewer-r{round_no}-c{state.get('cursor', 0)}"
    tasks = await rt.ledger.list_tasks(run_id)

    await rt.emit(run_id, "reviewer", "node_start",
                  {"role": "reviewer", "sub_task_id": sub_task_id}, version=version, round_no=round_no)
    await _claim(rt, state, "reviewer", "review")

    # ① 运行时先给出确定性结论
    report = validators.validate(
        allowed_tables=rt.allowed_tables,
        intents=list(state.get("intents") or []),
        history=list(state.get("history") or []),
        tasks=tasks, version=version, round_no=round_no,
        refusal=state.get("refusal") or "",
    )
    await rt.emit(run_id, "reviewer", "consistency", report.model_dump(),
                  version=version, round_no=round_no)

    history = list(state.get("history") or [])
    # ② 交给模型汇总
    system = (
        "你是汇总审查器。前面的智能体已经完成了数据操作，请给出给用户的最终答复。\n"
        "硬性要求：\n"
        "1. 只要『运行时一致性校验报告』里有任何一条 fail，decision 必须是 veto；\n"
        "2. final_answer 用用户能直接看懂的话：做了什么、结果如何、影响多少行；\n"
        "3. 有被拒绝、被取消、没找到数据的情况，必须如实说明，不要粉饰；\n"
        "4. 不要编造执行结果里没有的数据。"
    )
    plan_brief = {"understanding": state.get("understanding"),
                  "refusal": state.get("refusal"),
                  "intents": state.get("intents") or []}
    user = (
        f"用户的问题：{state.get('question', '')}\n\n"
        f"规划结论：{_dump(plan_brief)}\n\n"
        f"期间发生的错误：{state.get('error') or '（无）'}\n\n"
        f"每个子任务的落地情况：{_dump(history)}\n\n"
        f"执行结果明细：{_dump((state.get('result') or {}))}\n\n"
        f"运行时一致性校验报告（确定性证据）：\n{_dump(report.model_dump())}"
    )
    out, attempts, err = await _call(
        rt, ReviewerOutput, system, user,
        lambda: mock_llm.mock_reviewer(report, history, state.get("question", "")))
    if err is not None or out is None:
        return await _degrade(rt, state, "reviewer", sub_task_id, version, err)

    attempt = await _settle_attempts(rt, state, "reviewer", "review", attempts)

    # ③ 硬门禁
    forced = False
    if not report.passed and out.decision == "approve":
        forced = True
        out = ReviewerOutput(
            decision="veto",
            final_answer=out.final_answer,
            checks=list(out.checks),
            reason="运行时一致性校验未通过，已强制否决（模型原本给出 approve）。",
        )

    status = "done" if out.decision == "approve" else "failed"
    await rt.ledger.finish_task(run_id, sub_task_id, attempt, "completed",
                                result_ref=f"review:{out.decision}")
    new_version = await rt.ledger.set_run_status(run_id, status) or version

    await rt.emit(run_id, "reviewer", "review",
                  {"decision": out.decision, "forced_by_runtime": forced,
                   "reason": out.reason, "final_answer": out.final_answer,
                   "checks": [c.model_dump() for c in out.checks]},
                  version=new_version, round_no=round_no)
    await rt.emit(run_id, "reviewer", "node_end",
                  {"summary": "通过" if out.decision == "approve" else "否决"},
                  version=new_version, round_no=round_no)

    return {"final_answer": out.final_answer, "status": status,
            "version": new_version, "last_agent": "reviewer", "next_agent": "done"}
