"""离线兜底：LLM_MODE=mock 时的预置输出。

行为与真实模式一致：只能从**系统内置目录**里选表和字段，
生成的 SQL 刻意**安全**（删除条件永远匹配不到数据），
但走的是完全相同的链路 —— 选表 → 生成 → 静态防线 → 执行器 EXPLAIN/执行 → 人工确认，
一个都不少。
"""

from __future__ import annotations

import re

from . import catalog

from .state import (
    ExecutorOutput,
    FixerOutput,
    GeneratorOutput,
    PlannerOutput,
    QueryIntent,
    ReviewerOutput,
    SqlCheck,
    SqlDraft,
    TableFilterOutput,
    ValidatorOutput,
)

# 离线模式的选表启发式：**从表目录现推**，不写死任何表名。
# 表目录是可动态更换的，所以这里也不能硬编码 —— 换一套表，选表逻辑跟着变。


def _table_blob(t) -> str:
    """把一张表的描述性文字拼成一个块，用来跟问题做词面匹配。"""
    parts = [t.name, t.summary, t.notes]
    for c in t.columns:
        parts += [c.name, c.desc, *c.enum]
    return " ".join(parts).lower()


def _pick_table(question: str, allowed: set[str]) -> str:
    q = (question or "").lower()
    pool = sorted(allowed) or catalog.names()
    if not pool:
        return ""
    best, best_score = pool[0], -1
    for name in pool:
        t = catalog.get(name)
        blob = _table_blob(t) if t else name.lower()
        # 用「问题里的二字片段命中表描述的次数」当分数 —— 不需要分词库
        score = sum(1 for i in range(len(q) - 1) if q[i:i + 2] in blob)
        if score > best_score:
            best, best_score = name, score
    return best


def _pick_kind(question: str) -> str:
    if any(w in question for w in ("删除", "删掉", "清理", "delete")):
        return "删除"
    if any(w in question for w in ("修改", "更新", "改成", "标记", "update", "insert", "新增")):
        return "修改"
    return "查询"


# ---------------------------------------------------------------- Planner

# 危险 / 越界关键词（离线兜底也必须有这条防线，否则 mock 模式会「什么都答应」）
DANGER_WORDS = ("drop", "truncate", "alter table", "create table", "create index",
                "grant ", "revoke ", "删表", "删除表", "删掉表", "清空表",
                "改表结构", "改结构", "建表", "建索引",
                "加字段", "加个字段", "加一个字段", "新增字段", "增加字段",
                "改字段", "删字段", "删除字段",
                "删库", "删掉库", "agent_run", "pg_shadow", "information_schema",
                "绕过权限", "不管权限")

CHAT_WORDS = ("你好", "您好", "hi", "hello", "hey", "在吗", "早上好", "晚上好",
              "谢谢", "多谢", "辛苦", "你是谁", "你能做什么", "介绍一下你")

# 子串匹配有个坑：「删掉表」匹配不到「删掉 orders 表」—— 中间隔了个表名。
# 所以「动词 + …… + 表」这类型式要用正则兜；动词在后的（「把 X 表删了」）也要一条。
# 末尾的 (?!里|中|内) 是在区分两件事：
#   「删掉 orders 表」     → 删的是**表**（DDL，必须拒）
#   「删掉 orders 表里的数据」→ 删的是**数据**（DML，正常走）
_TABLE_DANGER = re.compile(
    r"(?:删|删除|删掉|清空|清掉|干掉|去掉|drop|truncate|alter|create)\s*\w*?\s*表(?!\s*[里中内])"
    r"|表\s*(?:删|删除|删掉|清空|清掉|干掉|drop|truncate|alter|create)",
    re.I)


def _danger_hit(text: str) -> str:
    m = _TABLE_DANGER.search(text or "")
    if m:
        return m.group(0).strip()
    return next((w for w in DANGER_WORDS if w in (text or "").lower()), "")


def mock_planner(question: str, schema: dict | None = None) -> PlannerOutput:
    """规划器**只拆任务**：判「聊天还是动数据」+ 危险评估 + 拆任务。

    离线兜底也要把这三步走出来，否则 mock 模式什么都答应，
    演示时看不出这层判断到底存不存在。

    注意：**没有「用户贴 SQL」这条路径** —— 问题里出现的 SQL 只是文字，
    由这里拆成任务，再由生成器从表结构写出来。
    """
    q = (question or "").strip()
    low = q.lower()

    # ① 打招呼 / 闲聊
    if len(q) <= 20 and any(w in low for w in CHAT_WORDS):
        return PlannerOutput(
            understanding="用户在打招呼 / 闲聊，与数据库无关",
            mode="chat",
            safety="安全",
            plan_reason="不需要访问数据库，直接回复即可",
            intents=[],
            chat_reply="你好！我是 SQL 智能体，你用自然语言说想查 / 改 / 删什么，"
                       "我来写 SQL 并查给你看。",
            handoff_to="reviewer",
        )

    # ② 危险 / 越界
    hit = _danger_hit(q)
    if hit:
        return PlannerOutput(
            understanding=f"用户想执行一个超出授权范围的操作（命中「{hit}」）",
            mode="execute",
            safety=f"越界：请求包含「{hit}」，属于 DDL / 越权操作，执行角色没有任何 DDL 权限",
            plan_reason="这类请求不做，直接拒绝并给替代方案",
            intents=[],
            refusal=f"这个请求超出允许范围（涉及「{hit}」）。"
                    f"我能做的是**数据**层面的查 / 改 / 删，改表结构这类 DDL 一律不做。"
                    f"如果你的目标是清理数据，可以改成「删掉符合条件的行」。",
            handoff_to="reviewer",
        )

    # ③ 要动数据 —— 交给生成器按任务写 SQL
    kind = _pick_kind(q)
    return PlannerOutput(
        understanding=f"用户想完成一次「{kind}」数据操作",
        mode="execute",
        safety="安全",
        plan_reason="需要真的对数据做事，走完整链路；"
                    "SQL 由生成智能体根据任务与表结构写出来。",
        intents=[QueryIntent(sub_task_id="st-1", intent=q[:60] or f"{kind} 数据",
                             kind=kind, depends_on=[])],
        handoff_to="generator",
    )


def mock_replan(state: dict, intents: list[dict]) -> PlannerOutput:
    """规划器**复查**的离线兜底：剩余计划照原样继续。

    mock 不解析提示词，所以这里只能给出最保守但正确的结论 —— continue。
    「要不要根据实际结果修正剩余任务」是真实模型的判断，
    由 `prompts.PLANNER_REPLAN_SYSTEM` 约束（所以改提示词必须用真模型验一遍）。
    """
    done = min(int(state.get("cursor", 0)), len(intents))
    mems = list(state.get("memory") or [])
    return PlannerOutput(
        understanding=f"复查：已完成 {done}/{len(intents)} 个任务",
        mode="execute",
        plan_action="continue",
        review_note=(f"已完成 {done} 个，拿到 {len(mems)} 条已确认的事实；"
                     f"它们不影响剩余任务的前提，按原计划继续。"),
        safety="安全",
        plan_reason="复查不动计划",
        intents=[QueryIntent(**i) for i in intents],
        handoff_to="generator",
    )


# ---------------------------------------------------------------- 生成器的第一层：选表

def mock_table_filter(intent: dict, allowed: set[str]) -> TableFilterOutput:
    table = _pick_table(intent.get("intent", ""), set(allowed))
    return TableFilterOutput(tables=[table], reason=f"按关键词选中 {table}")


# ---------------------------------------------------------------- Generator

def _safe_sql(table: str, kind: str) -> tuple[str, str]:
    """按系统内置目录拼一条**安全**的 SQL（条件永远匹配不到数据）。"""
    t = catalog.get(table)
    pk = (t.pk if t else None) or "id"
    cols = [c.name for c in t.columns] if t else [pk]
    if kind == "删除":
        return (f"DELETE FROM {table} WHERE {pk} = -1",
                f"删除条件刻意用不存在的 {pk}，离线模式下不会真的删掉数据")
    if kind == "修改":
        target = next((c for c in cols if c != pk), pk)
        return (f"UPDATE {table} SET {target} = {target} WHERE {pk} = -1",
                f"用 {pk} = -1 限定范围，离线模式下不会真的改到数据")
    return (f"SELECT * FROM {table} LIMIT 20", "只读查询，限制 20 行")


def mock_generator(intent: dict, picked: list[str] | set[str],
                   allowed: set[str]) -> GeneratorOutput:
    """第二层：在**选表步骤给出的表**里挑一张，按任务写一条 SQL。"""
    pool = set(picked) or set(allowed)
    table = _pick_table(intent.get("intent", ""), pool)
    kind = intent.get("kind") or "查询"
    sql, note = _safe_sql(table, kind)
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
               detail: str, allowed: set[str]) -> FixerOutput:
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

def _checks(report) -> list[SqlCheck]:
    return [SqlCheck(check_id=f"r-{i + 1}", item=r.title,
                     result=r.result, detail=r.detail)
            for i, r in enumerate(report.results)]


def mock_reviewer(report, history: list[dict], state: dict) -> ReviewerOutput:
    """离线兜底也要产出**同样形状**的成品答复 —— 而且形状随 mode 变。

    否则 LLM_MODE=mock 时演示出来的答复和真实模式长得不一样，
    离线跑通了也说明不了线上没问题。
    """
    question = state.get("question", "")
    mode = state.get("mode") or "execute"

    if state.get("status") == "cancelled":
        return ReviewerOutput(decision="veto", final_answer="你取消了本次执行；这条 SQL 没有执行，后续任务也没有继续。",
                              checks=_checks(report), reason="用户取消，目标未全部完成", handoff_to="done")

    # ① 被静态防线拦下 —— **该交付的照给**（修好的 / 用户那条 SQL），只是不能执行。
    #    这一条必须放在最前面：一旦被拦，校验器根本没跑过，
    #    再往下走别的分支就会拿着一份不存在的校验结果硬报结论。
    verdict = state.get("verdict") or {}
    if verdict.get("level") == "禁止":
        draft = state.get("draft") or {}
        sql = draft.get("sql") or ""
        why = "；".join(verdict.get("reasons") or []) or "静态防线判定禁止"
        tables = "、".join(catalog.names())
        body = [
            "这条 SQL 我**没有执行** —— 它被静态防线拦下了。",
            "",
            "**SQL 本身**：",
            "",
            "```sql",
            sql,
            "```",
            "",
        ]
        if draft.get("note"):
            body += [f"_说明：{draft['note']}_", ""]
        body += [
            f"**为什么在这里跑不了**：{why}",
            "",
            f"系统目录里可用的表是：**{tables}**。"
            f"把表名换成其中之一，这条就能在这里执行（换完让我再跑一次即可）。",
        ]
        return ReviewerOutput(
            decision="veto" if not report.passed else "approve",
            final_answer="\n".join(body),
            checks=_checks(report),
            reason="静态防线拦下，未执行；但把用户要的 SQL 交付了",
            handoff_to="done",
        )

    # ② 打招呼 / 闲聊 —— 不报"已处理 0 个数据操作"这种废话
    if mode == "chat":
        return ReviewerOutput(
            decision="veto" if not report.passed else "approve",
            final_answer=state.get("chat_reply") or "你好！有什么可以帮你的？",
            checks=_checks(report),
            reason="闲聊，无需访问数据库",
            handoff_to="done",
        )

    # ③ 越界请求被拒绝 —— 答复要把拒绝理由原样讲清楚，别报"处理了 0 个操作"
    if state.get("refusal"):
        return ReviewerOutput(
            decision="veto" if not report.passed else "approve",
            final_answer=f"这个请求我没有执行。\n\n{state.get('refusal')}\n\n"
                         f"**没有产生任何数据操作。**",
            checks=_checks(report),
            reason="规划器判定请求超出允许范围",
            handoff_to="done",
        )

    # ④ 正常执行
    done = [h for h in history if h.get("executed")]
    lines = [f"针对「{question}」，共处理 {len(done)} 个数据操作。"]

    for h in done:
        sid = h.get("sub_task_id")
        intent = h.get("intent") or ""
        cols, rows = h.get("columns") or [], h.get("rows") or []

        if h.get("kind") == "cancelled":
            lines += ["", f"**{sid}** · {intent}", "", "你取消了执行，未做任何改动。"]
        elif h.get("exec_ok") is False:
            lines += ["", f"**{sid}** · {intent}", "",
                      f"执行失败：{h.get('error') or '未知错误'}"]
        elif cols:
            # 查询结果 —— 用 Markdown 表格原样列出
            lines += ["", f"**{sid}** · {intent}", ""]
            if rows:
                lines.append("| " + " | ".join(str(c) for c in cols) + " |")
                lines.append("| " + " | ".join("---" for _ in cols) + " |")
                for r in rows:
                    lines.append("| " + " | ".join("" if v is None else str(v) for v in r) + " |")
                total = h.get("affected_rows") or len(rows)
                if h.get("truncated") or len(rows) < total:
                    lines += ["", f"共 {total} 行，这里展示前 {len(rows)} 行。"]
            else:
                lines.append("没有查到符合条件的数据。")
        else:
            lines += ["", f"**{sid}** · {intent}", "",
                      f"影响 {h.get('affected_rows')} 行（{h.get('action') or ''}）。"]

    if not done:
        lines.append("没有实际执行任何数据操作。")

    return ReviewerOutput(
        decision="veto" if not report.passed else "approve",
        final_answer="\n".join(lines),
        checks=_checks(report),
        reason="一致性校验全部通过" if report.passed else "存在不变量未通过",
        handoff_to="done",
    )
