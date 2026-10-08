"""六个智能体节点。

    planner    规划器     —— **只拆任务**，不碰表信息
    generator  生成智能体 —— 两层取表（简述+关系过滤 → 详情）+ 生成 SQL
    validator  校验智能体 —— 静态防线 + 模型语义校验，**零数据库操作**
    executor   执行智能体 —— **唯一执行 SQL 的智能体**：EXPLAIN + 执行；需确认时 interrupt
    fixer      修正智能体 —— 拿执行器给出的真实报错重写 SQL
    reviewer   汇总审查器 —— 汇总结果 + 硬门禁 + 给用户的答复

两条硬边界：

1. **校验阶段不许有任何数据库操作。** 校验智能体不 EXPLAIN、不查 information_schema、
   也不判断表是否存在 —— 那些都是"偷看现实"。SQL 能不能跑，由执行器在执行时给答案。
   这样"表找不到"这类错误才会真实地抛出来，交给修正智能体处理（这正是要测的东西）。

2. **只有执行器执行 SQL。** 其他任何地方都不对业务库发语句。

表结构来自外部表目录文件（`config/tables.json`），不读数据库。

协作链路**不固定**：每个节点只负责"说话"和"建议下一步交给谁"（`next_agent`），
真正走哪条路由 `router` 节点用运行时护栏决定。

每次大模型调用都会带着「谁 + 在干什么」落进 `llm_call` 表（消耗清单），
用量取自响应里的真数，不按字符数估算。
"""

from __future__ import annotations

import json
import time

from langgraph.types import interrupt

from . import catalog, catalog_check, mock_llm, prompts, runtime, sql_guard, validators
from .state import (
    ExecutorOutput,
    FixerOutput,
    GeneratorOutput,
    PlannerOutput,
    ReviewerOutput,
    TableFilterOutput,
    ValidatorOutput,
    now_hash,
    one_line,
)


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- 公共骨架

async def _call(rt, schema, system: str, user: str, mock_fn, ctx: dict | None = None):
    """结构化调用，失败重试 1 次。返回 (output, attempts, error)。

    `ctx` 说明这次调用「是谁、在干什么」，用量的账就记在它下面 ——
    没有它，消耗清单只能按角色粗分，看不出钱花在哪一步。
    """
    if rt.llm is None:
        return mock_fn(), 1, None
    bound = rt.structured(schema)
    messages = [("system", system + prompts.CONTRACT_HINT), ("human", user)]
    last_err: Exception | None = None
    for attempt in (1, 2):
        started = time.monotonic()
        try:
            res = await bound.ainvoke(messages)
            raw, out = runtime.split_raw(res)
            await rt.record_llm(ctx, usage=runtime.usage_of(raw),
                                duration_ms=int((time.monotonic() - started) * 1000),
                                attempt=attempt)
            if out is not None:
                return out, attempt, None
            last_err = RuntimeError("模型返回空结果")
        except Exception as exc:  # noqa: BLE001
            # 失败的调用一样花了输入 token，也要落账 —— 否则"账单"比真实花费少
            await rt.record_llm(ctx, usage={}, duration_ms=int((time.monotonic() - started) * 1000),
                                ok=False, error=str(exc), attempt=attempt)
            last_err = exc
    return None, 2, last_err


def _ctx(state, role: str, stage: str) -> dict:
    """一次调用的上下文：谁、在干什么、第几轮、第几个子任务。"""
    return {
        "run_id": state.get("global_task_id"),
        "role": role, "stage": stage,
        "round": int(state.get("round", 0) or 0),
        "cursor": int(state.get("cursor", 0) or 0),
        "version": int(state.get("version", 0) or 0),
    }


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


# ---------------------------------------------------------------- 交接（共享记忆）

def _result_desc(mem: dict) -> str:
    action = (mem.get("action") or "").upper()
    n = mem.get("rowcount")
    if action == "SELECT":
        return f"返回 {n if n is not None else '?'} 行"
    return f"影响 {n if n is not None else '?'} 行"


def _entities_from(exec_result: dict, tables: list[str]) -> dict:
    """从查询结果里抽出「实体」(表 + 键 + 值)，让下游能直接引用具体值。

    例如 st-1 查价格 > 500 的商品 → entities = {table: products, key: id,
    values: [3,6,8,10]}。下游就能写 `WHERE product_id IN (3,6,8,10)` ——
    这正是「后面的计划要基于前面的实际结果」在数据上的样子。

    找键的顺序：
      ① 这张表的主键列名（`products.id`）；
      ② 指向这张表的外键列名（`order_id` / `orders_id`）——
         查询为了展示常常只 SELECT 外键而不 SELECT 主键，
         这时外键值一样能当"范围"用。

    写操作（UPDATE/DELETE）拿不到具体行（只返回影响行数），这时返回空 ——
    **不编**。下游要判断范围就读来源 SQL 里的 WHERE。
    """
    cols = exec_result.get("columns") or []
    rows = exec_result.get("rows") or []
    if not cols or not rows:
        return {}
    lower = {str(c).lower(): i for i, c in enumerate(cols)}   # 别名大小写不定，按小写对
    for t in tables or []:
        tb = catalog.get(t)
        pk = (tb.pk if tb else None) or "id"
        # ①② 的候选键名：主键，以及常见的「指向本表的外键」写法
        cands = [pk, f"{t[:-1]}_id", f"{t}_id"]
        key = next((c for c in cands if c and c.lower() in lower), None)
        if not key:
            continue
        i = lower[key.lower()]
        # 去重保序：同一个商品出现在 3 张订单里，下游要的是「那批商品」而不是 3 个重复值
        vals: list = []
        seen: set = set()
        for r in rows:
            if i >= len(r):
                continue
            v = r[i]
            try:
                if v in seen:
                    continue
                seen.add(v)
            except TypeError:   # 不可哈希的值（理论上不会出现）就不去重
                pass
            vals.append(v)
        if vals:
            return {"table": t, "key": key, "values": vals}
    return {}


def _fact_claim(mem: dict) -> str:
    """读取边界重新构造证据说明；旧 DB/checkpoint 的 claim 也不能冒充事实。"""
    if mem.get("stage") != "executed" or not mem.get("verified") or not mem.get("sql_id"):
        return "缺少可核对的成功执行来源，不作为已确认事实"
    return f"数据库执行成功，返回/影响 {mem.get('rowcount', '未知')} 行"


def _render_fact(mem: dict) -> str:
    """把一条共享记忆渲染成「带来源的事实」。

    来源必须写出来 —— 文档讲「共享记忆记录引用和状态，不能取代权威数据源」，
    所以这里把 sql_id 与原文一并给出，要核对随时能回去看。
    """
    ent = mem.get("entities") or {}
    body = ""
    if mem.get("truncated", True) or mem.get("sampled", True):
        body += "      完整性：结果被截断或仅保存样本，不能当作全部数据。\n"
    values = ent.get("values") or []
    if ent.get("key") and values:
        shown = "、".join(str(v) for v in values[:50])
        tail = "" if len(values) <= 50 else f" …（共 {len(values)} 个）"
        body += f"      {ent.get('table', '?')}.{ent['key']} = {shown}{tail}\n"
    cols, rows = mem.get("columns") or [], mem.get("rows") or []
    if cols and rows:
        body += "      列：" + " | ".join(str(c) for c in cols) + "\n"
        body += "      前几行：\n"
        for r in rows[:5]:
            body += "        " + " | ".join("" if v is None else str(v) for v in r) + "\n"
    sql = (mem.get("sql_text") or "").replace("\n", " ")
    return prompts.FACT_ITEM.format(
        sub_task_id=mem.get("sub_task_id", "?"),
        claim=_fact_claim(mem),
        sql=sql[:160] + ("…" if len(sql) > 160 else ""),
        result_desc=_result_desc(mem),
        entity_lines=body,
    )


def _render_memory(mems: list[dict]) -> str:
    """把共享记忆整段渲染出来（没有就给一句「没有上游事实」）。"""
    return "".join(_render_fact(m) for m in mems) if mems else prompts.FACTS_EMPTY


def _render_plan(intents: list[dict], cursor: int) -> str:
    """把当前计划渲染成「哪几步做完了、哪几步还没做」，供规划器复查时对照。"""
    lines = []
    for i, it in enumerate(intents):
        mark = "已完成" if i < cursor else "待办"
        dep = it.get("depends_on") or []
        lines.append(f"  [{mark}] {it.get('sub_task_id')}（{it.get('kind')}）"
                     f"{it.get('intent')}"
                     + (f" —— 依赖 {'、'.join(dep)}" if dep else ""))
    return "\n".join(lines) or "  （空）"


def _handoff(state, rt, intent: dict) -> str:
    """按原始文档的**六类结构**给下游交出「拿来就能继续工作的状态」。

    这里最关键的是【已确认的事实】：它只来自 `state["memory"]` ——
    也就是**执行器真正执行过**、带 sql_id 可核对的那些条目。
    模型自己说的话进不来。

    取哪些记忆：优先按 `depends_on`；它没声明时退化为「取全部已完成的」，
    宁可多给一点，也不能把连续性丢了。
    """
    sid = intent.get("sub_task_id") or "?"
    deps = [d for d in (intent.get("depends_on") or []) if d]
    done_ids = [h.get("sub_task_id") for h in (state.get("history") or [])
                if h.get("executed") and h.get("exec_ok") is True
                and h.get("kind") != "cancelled" and h.get("sub_task_id") != sid]
    want = deps or done_ids

    mems = [m for m in (state.get("memory") or []) if m.get("sub_task_id") in want]
    if mems:
        facts = _render_memory(mems)
    elif deps:
        facts = prompts.FACTS_MISSING.format(deps="、".join(deps))
    else:
        facts = prompts.FACTS_EMPTY

    tables = "、".join(sorted(rt.allowed_tables))
    return prompts.HANDOFF_BLOCK.format(
        sub_task_id=sid,
        intent=intent.get("intent") or "",
        kind=intent.get("kind") or "",
        depends=("、".join(deps) if deps else
                 ("（未声明，但已有上游结果：" + "、".join(done_ids) + "）" if done_ids
                  else "（无，这是第一步）")),
        facts=facts,
        assumptions=("任务描述之外的细节（字段选择、聚合口径、边界条件）由你判断；"
                     "但**上游事实里给出的值必须原样使用，不许自己造**。"),
        allowed=f"只能访问这些表：{tables}；禁止任何 DDL。",
        artifacts="本步要产出一条 SQL（会记进 sql_audit，带 sql_id 可追溯）。",
        acceptance=intent.get("acceptance") or "产出一条能正确完成本子任务的 SQL。",
    )


# ---------------------------------------------------------------- 规划器

async def planner(state, rt) -> dict:
    run_id = state["global_task_id"]
    version = state.get("version", 0)
    sub_task_id = await _claim(rt, state, "planner", "plan")

    await rt.emit(run_id, "planner", "node_start",
                  {"role": "planner", "sub_task_id": sub_task_id}, version=version)

    system = prompts.PLANNER_SYSTEM

    # 这一次是「首次拆解」还是「拿到实际结果后的复查」？
    # 判据只有一条：**已经有计划了**就是复查 ——
    # 首次拆解时 intents 必然是空的；而规划器只会在三个位置被叫到：
    # 首次、每个子任务跑完之后的复查点、某一步失败后（校验/执行把决定权交给它），
    # 后两种都已经有计划了。
    is_replan = bool(state.get("intents"))
    intents = list(state.get("intents") or [])
    cursor = int(state.get("cursor", 0))

    if is_replan:
        system = prompts.PLANNER_REPLAN_SYSTEM
        user = prompts.PLANNER_REPLAN_USER.format(
            question=state.get("question", ""),
            total=len(intents), done=min(cursor, len(intents)),
            plan=_render_plan(intents, cursor),
            facts=_render_memory(list(state.get("memory") or [])))
    else:
        prior = list(state.get("prior_turns") or [])
        prior_text = prompts.PLANNER_PRIOR.format(turns=_dump(prior[-5:])) if prior else ""
        user = prompts.PLANNER_USER.format(prior=prior_text,
                                           question=state.get("question", ""))

    out, attempts, err = await _call(
        rt, PlannerOutput, system, user,
        (lambda: mock_llm.mock_replan(state, intents)) if is_replan
        else (lambda: mock_llm.mock_planner(state.get("question", ""), {})),
        ctx=_ctx(state, "planner", "复查剩余计划" if is_replan else "理解问题并拆任务"))

    if err is not None or out is None:
        return await _degrade(rt, state, "planner", sub_task_id, version, err)

    # ---- 运行时硬拦①：用户只能通过「提问」，不能直接给 SQL ----
    #
    # 用户的权限只体现在**对话**里，不体现在"递过来一条能跑的语句"上。
    # 输入里一旦夹带 SQL，就按危险操作处理。
    # 放在运行时（确定性）而不是提示词里 —— 这是规则，不是建议。
    # 好处还有一个：入库的每一条 SQL 都必然出自生成器，不留旁门。
    #
    # 只在**首次拆解**时判：这是输入层面的检查，同一个问题复查时不必再判一遍
    #（真夹带了 SQL，首次就已经拒绝，根本走不到复查）。
    user_sql = sql_guard.find_user_sql(state.get("question", "")) if not is_replan else None
    if user_sql and not out.refusal:
        out = PlannerOutput(
            understanding="用户消息里夹带了 SQL",
            mode="execute",
            safety=f"越界：输入里夹带了 SQL（{user_sql[:60]}…）",
            plan_reason="用户只能通过提问引导；不接受直接下达 SQL",
            intents=[],
            refusal=("我不能执行你给的 SQL —— 你只能通过**提问**告诉我你想查什么，"
                     "语句由我根据你的问题生成。\n\n"
                     "把需求说成一句话就行，比如「查一下 name 是张三的客户」。"),
            handoff_to="reviewer",
        )

    # ---- 运行时硬拦②：用户提到的表，必须都在系统目录里 ----
    #
    # 这一步**不交给模型判断**。模型看到目录里没有 users，会"聪明地"换成 customers 顶上，
    # 于是用户问的是 A、查的是 B，一路校验通过、执行成功、还返回 0 行 ——
    # 全链路看起来都对，就是答错了问题。
    #
    # 换一张表顶上 = 换了问题，所以必须在链路开始前掐掉，直接说明这张表不可用。
    # 同样只在首次拆解时判（输入层面的检查）。
    unknown = (catalog_check.unknown_tables(state.get("question", ""))
               if not is_replan else [])
    if unknown and not out.refusal:
        names = "、".join(unknown)
        avail = "、".join(catalog.names())
        out = PlannerOutput(
            understanding=f"用户要操作的表 {names} 不在系统目录里",
            mode="execute",
            safety=f"越界：表 {names} 不在系统目录里",
            plan_reason="换一张表顶上等于换了问题，所以不换、直接说明不可用",
            intents=[],
            refusal=(f"你提到的表 **{names}** 不在我能访问的范围内。"
                     f"我**不会**拿另一张表顶替它 —— 那回答的就不是你的问题了。\n\n"
                     f"系统目录里可用的表是：**{avail}**。换成其中之一，我就帮你查。"),
            handoff_to="reviewer",
        )

    prior_turns = list(state.get("prior_turns") or [])
    round_no = int(state.get("round", 0))
    attempt = await _settle_attempts(rt, state, "planner", "plan", attempts)
    await rt.ledger.finish_task(run_id, sub_task_id, attempt, "completed",
                                result_ref=f"plan-{len(out.intents)}")
    new_version = (await rt.ledger.set_run_status(
        run_id, "planning", round_no if is_replan else 0) or version)

    # ---------- 复查的产物：拿**实际结果**决定剩余计划动不动 ----------
    action = "initial"
    patch: dict = {}
    dropped: list[dict] = []
    if is_replan:
        # plan_action 是复查专有字段；模型若仍填 initial，按最保守的「继续」处理
        action = out.plan_action if out.plan_action != "initial" else "continue"
        if out.refusal:
            # 复查阶段也可能得出「不该继续做」的结论，一样交给汇总审查器去讲清楚
            action = "finish"
        if action == "revise" and out.intents:
            intents = [i.model_dump() for i in out.intents]
            # 游标**按「哪几个子任务真的执行过」重算**，不按模型给的列表形状。
            # （模型要是只把剩下的任务还回来，直接沿用旧游标就会错位。）
            done_ids = {h.get("sub_task_id") for h in (state.get("history") or [])
                        if h.get("executed") and h.get("exec_ok") is True
                        and h.get("kind") != "cancelled"}
            cursor = next((i for i, it in enumerate(intents)
                           if it.get("sub_task_id") not in done_ids), len(intents))
            # 计划变了，上一版 SQL 就对不上当前子任务了 —— 必须清掉，
            # 否则校验/执行会拿着旧 SQL 继续往下走（"改了计划却没改动作"）
            patch.update({"draft": None, "verdict": {}, "checks": None,
                          "result": None, "confirmed": False})
        elif action == "finish":
            # 「到此为止」= 剩下的任务不做了 —— **计划要跟着缩**。
            # 不缩的话一致性校验会判「有子任务没落地」从而否决，
            # 等于运行时把规划器刚做出的结论又推翻了一次。
            dropped = intents[cursor:]
            intents = intents[:cursor]
            cursor = len(intents)
        # 复查只在一个位置做一次 —— 记住做到哪了，避免原地绕圈
        patch["replanned_cursor"] = cursor
        # 游标必须**显式写回状态**：复查可能把计划换了、把游标按已执行过的子任务重算过，
        # 只改局部变量而不交出去，图状态里留的还是旧游标 —— 下一步就会跳过一个子任务。
        patch["cursor"] = cursor
        note = f"复查（已完成 {min(cursor, len(intents))} 个）：{out.review_note or action}"
        if dropped:
            note += "；判定不必再做：" + "、".join(
                d.get("sub_task_id", "?") for d in dropped)
        patch["plan_note"] = note
    else:
        intents = [i.model_dump() for i in out.intents]
        patch["cursor"] = 0
        patch["plan_note"] = ""

    await rt.emit(run_id, "planner", "plan",
                  {"understanding": out.understanding, "plan_reason": out.plan_reason,
                   "mode": out.mode, "safety": out.safety,
                   "intents": intents, "refusal": out.refusal,
                   "chat_reply": out.chat_reply, "attempts": attempts,
                   # 复查与首次拆解要能分得开，前端才画得出「回到规划器复查」这一步
                   "phase": action, "plan_action": out.plan_action,
                   "review_note": out.review_note, "cursor": cursor,
                   "dropped": [d.get("sub_task_id") for d in dropped],
                   # 把**实际喂进去的多轮上文**也落下来 —— 否则"多轮到底有没有生效"
                   # 只能靠猜（它是输入，不是返回值，状态增量里看不到）
                   "context_turns": prior_turns,
                   "turn": state.get("turn", 1)},
                  version=version, round_no=round_no)

    if out.refusal:
        summary = f"拒绝执行：{one_line(out.refusal)[:50]}"
    elif out.mode == "chat" and not is_replan:
        summary = "打招呼 / 闲聊，不需要动数据库"
    elif is_replan:
        summary = {"continue": "复查：剩余计划仍然成立，继续做",
                   "revise": f"复查：调整后续任务（现共 {len(intents)} 个）",
                   "finish": "复查：已有信息足够回答，收尾"}.get(action, "复查完成")
    else:
        summary = f"拆出 {len(intents)} 个数据操作任务"
    await rt.emit(run_id, "planner", "node_end",
                  {"summary": summary, "suggests": out.handoff_to},
                  version=version, round_no=round_no)

    # chat / 拒绝 都不需要下游动手
    if out.refusal or (out.mode == "chat" and not is_replan):
        nxt = "reviewer"
    else:
        nxt = out.handoff_to or "generator"

    return {**patch,
            "intents": intents, "refusal": out.refusal or "",
            # 复查不改 mode：模式属于「这一轮用户想干什么」，不属于复查
            "mode": state.get("mode") or out.mode,
            "understanding": out.understanding,
            "safety": out.safety or "",
            "chat_reply": state.get("chat_reply") or out.chat_reply or "",
            "status": "planning", "version": new_version, "last_agent": "planner",
            "next_agent": nxt}


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

    # ---------- SQL 只从这里产生：根据任务描述写一条 ----------
    # （没有「用户贴 SQL」这条路径 —— 问题里出现的 SQL 只是文字，
    #   由规划器拆成任务，再由这里从表结构生成。）
    picked = catalog.names()

    # ---------- 第一层：先用「表简述 + 表关系」粗筛出要碰哪几张表 ----------
    # 这一步**看不到字段**。先粗筛、再看细节，模型的注意力才落对地方；
    # 一次把全部字段灌进去，它反而容易在无关的表之间乱 JOIN。
    filter_system = prompts.GENERATOR_FILTER_SYSTEM
    filter_user = prompts.GENERATOR_FILTER_USER.format(
        question=state.get("question", ""),
        sub_task_id=intent.get("sub_task_id"), intent=intent.get("intent"),
        kind=intent.get("kind"), tier1=catalog.tier1_text())
    pick_reason = "目录较小，直接根据表结构生成，不增加选表模型调用"
    if len(picked) > getattr(rt.settings, "table_filter_threshold", 20):
        pick, _, pick_err = await _call(
            rt, TableFilterOutput, filter_system, filter_user,
            lambda: mock_llm.mock_table_filter(intent, rt.allowed_tables),
            ctx=_ctx(state, "generator", "第一层·选表"))
        if pick_err is not None or pick is None:
            pick_reason = f"选表步骤失败（{pick_err}），退化为使用全部表"
        else:
            picked = catalog.connected(pick.tables) or catalog.names()
            pick_reason = pick.reason

    detail = catalog.detail_text(picked)
    await rt.emit(run_id, "generator", "tables",
                  {"selected": picked, "reason": pick_reason,
                   "candidates": catalog.names()}, version=version, round_no=round_no)

    # ---------- 第二层：拿过滤后表的完整字段生成 SQL ----------
    system = prompts.GENERATOR_SYSTEM
    user = prompts.GENERATOR_USER.format(
        question=state.get("question", ""),
        handoff=_handoff(state, rt, intent),
        tables="、".join(picked), detail=detail)
    out, attempts, err = await _call(
        rt, GeneratorOutput, system, user,
        lambda: mock_llm.mock_generator(intent, picked, rt.allowed_tables),
        ctx=_ctx(state, "generator", "第二层·生成 SQL"))
    if err is not None or out is None:
        return await _degrade(rt, state, "generator", sub_task_id, version, err)

    attempt = await _settle_attempts(rt, state, "generator", "generate", attempts)
    draft = out.draft.model_dump()
    draft["round"] = round_no
    # **强制**打上当前子任务 ID：运行时知道现在在做哪个子任务，
    # 不能让模型填错这个字段 —— 它决定了路由与历史归属，填错会被当成「另一个子任务」
    draft["sub_task_id"] = intent.get("sub_task_id") or draft.get("sub_task_id")
    # **强制**打上本步筛出的表：修正智能体也继承这份清单，
    # 于是"把不存在的表换成另一张同构表"在运行时层就被挡住
    draft["tables"] = picked
    v = sql_guard.analyze(draft["sql"], rt.allowed_tables)
    if v.ok and v.normalized_sql:
        draft["sql"] = v.normalized_sql
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


# ---------------------------------------------------------------- 校验智能体

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

    # ① 运行时确定性检查：静态防线（纯字符串/语法层面，不碰数据库）
    v = sql_guard.analyze(sql, rt.allowed_tables)
    runtime_checks = [{
        "check_id": "rt-static", "item": "静态防线（危险语句 / 多语句 / 表名授权）",
        "result": "pass" if v.ok else "fail",
        "detail": ("；".join(v.reasons) if v.reasons else
                   f"{v.action} · 风险 {v.level}" + ("；" + "；".join(v.notes) if v.notes else "")),
    }]

    # ①b 运行时确定性检查：SQL 的表必须落在生成器筛出的表之内（防止「偷换表」）
    tables_ok, tables_detail = sql_guard.check_intent_tables(sql, draft.get("tables"))
    runtime_checks.append({
        "check_id": "rt-intent-tables", "item": "SQL 涉及的表必须在生成器选定的表之内",
        "result": "pass" if tables_ok else "fail", "detail": tables_detail,
    })

    # ①c 运行时确定性检查：**按系统目录的表信息**核对表和字段。
    #
    # 只挡「表在目录里、字段是编的 / 抄错的」这一类。一行 SQL 能不能进这个库执行，
    # 必须先过这一关 —— 执行器永远排在它后面。
    # 注意：读的是 app/catalog.py，**不是数据库**（校验阶段零数据库操作）。
    cat_ok, cat_detail, _ = catalog_check.check_sql(sql)
    # 保险丝：问题里提到的表若不在目录里，绝不允许拿别的表顶替着执行。
    # 规划器已经拦过一道，这里再拦一道 —— 「换表顶上」是这个系统最不能出的事。
    unknown = catalog_check.unknown_tables(state.get("question", ""))
    if unknown:
        cat_ok = False
        cat_detail = (f"问题里提到的表 {'、'.join(unknown)} 不在系统目录里，"
                      f"不能拿别的表顶替执行。可用的表：{'、'.join(catalog.names())}")
    runtime_checks.append({
        "check_id": "rt-catalog", "item": "表和字段必须能在系统目录里对上（按表信息核对）",
        "result": "pass" if cat_ok else "fail", "detail": cat_detail,
    })

    # ② 这里**刻意不做任何数据库操作**。
    #
    # 早先这里会跑 EXPLAIN，失败时还去查 information_schema 列出「可见的表」、
    # 算出「缺失的表」—— 那等于校验阶段自己去偷看现实：表被改名/删掉时，
    # 系统立刻自己发现并改口，修正智能体永远等不到真实的报错。
    #
    # 现在的分工：语法/语义/表是否存在，全部由**执行器**在执行时给答案；
    # 报错原样交给修正智能体。校验器只做两件不碰库的事：静态防线 + 语义判断。
    runtime_checks.append({
        "check_id": "rt-no-db", "item": "校验阶段不访问数据库",
        "result": "pass",
        "detail": "表是否存在、语法与语义由执行器在执行时判定，本阶段不发任何语句",
    })

    runtime_passed = all(c["result"] == "pass" for c in runtime_checks)

    # ③ 模型语义校验：这条 SQL 是否真的实现了任务
    system = prompts.VALIDATOR_SYSTEM
    user = prompts.VALIDATOR_USER.format(
        question=state.get("question", ""),
        intent=intent.get("intent"), kind=intent.get("kind"),
        tables="、".join(draft.get("tables") or []) or "（未声明）",
        sql=sql,
        detail=catalog.detail_text(draft.get("tables")),
        runtime_checks=_dump(runtime_checks))
    if runtime_passed and getattr(rt.settings, "semantic_review", False):
        out, attempts, err = await _call(
            rt, ValidatorOutput, system, user,
            lambda: mock_llm.mock_validator(v, runtime_checks, intent),
            ctx=_ctx(state, "validator", "可选语义复核"))
        if err is not None or out is None:
            return await _degrade(rt, state, "validator", sub_task_id, version, err)
    else:
        out = ValidatorOutput(passed=runtime_passed, checks=[],
                              reason="程序门禁通过；人工确认由执行器负责" if runtime_passed else "程序门禁未通过")
        attempts = 1

    attempt = await _settle_attempts(rt, state, "validator", "validate", attempts)

    # ④ 硬门禁：运行时没过，模型说通过也不算
    model_checks = [c.model_dump() for c in out.checks]
    passed = bool(out.passed) and runtime_passed
    forced = (not runtime_passed) and bool(out.passed)
    checks = {
        "passed": passed,
        "sql_hash": now_hash(sql),
        "forced_by_runtime": forced,
        "reason": out.reason or ("运行时检查未通过" if not runtime_passed else "校验通过"),
        "checks": runtime_checks + model_checks,
    }

    await rt.ledger.finish_task(run_id, sub_task_id, attempt, "completed",
                                result_ref=f"check-{'pass' if passed else 'fail'}")
    await rt.ledger.audit_sql(
        run_id, round_no, draft.get("sub_task_id", ""),
        "validated" if passed else "rejected",
        sql, now_hash(sql), intent=draft.get("intent"),
        risk_level=v.level, action=v.action, tables=v.tables,
        # 校验阶段不碰数据库，所以这里**拿不到预估行数** ——
        # 预估由执行器 EXPLAIN 后补写进审计（见 executor 的 audit_sql）
        est_rows=None, need_confirm=v.needs_confirm)

    new_version = await rt.ledger.set_run_status(run_id, "validating", round_no) or version
    for c in checks["checks"]:
        await rt.emit(run_id, "validator", "check", c, version=version, round_no=round_no)
    await rt.emit(run_id, "validator", "verdict",
                  {"passed": passed, "reason": checks["reason"],
                   "forced_by_runtime": forced, "static": checks["checks"][0].get("detail")},
                  version=version, round_no=round_no)
    await rt.emit(run_id, "validator", "node_end",
                  {"summary": "校验通过" if passed else "校验不通过",
                   "suggests": out.handoff_to}, version=version, round_no=round_no)

    # 记录本轮校验结论（同一个 sub_task_id 增量合并，不覆盖历史）
    # 同时记下「这次 SQL + 这次失败特征」，供路由护栏判断修正有没有进展
    error_sig = ""
    if not passed:
        fails = [c for c in checks["checks"] if c["result"] == "fail"]
        error_sig = now_hash(" | ".join(f"{c.get('item')}:{c.get('detail')}" for c in fails))
    attempts_log = list(state.get("attempts") or [])
    attempts_log.append({
        "round": round_no, "sql_hash": now_hash(sql), "sql": sql[:400], "ok": passed,
        "error_sig": error_sig,
        "why": "；".join(f"{c.get('item')}: {str(c.get('detail'))[:140]}"
                         for c in checks["checks"] if c["result"] == "fail") or "执行失败",
    })

    return {"checks": checks, "status": "validating",
            "version": new_version, "attempts": attempts_log,
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

    # 执行边界重新计算风险，防止旧 verdict/损坏状态绕过静态防线。
    fresh = sql_guard.analyze(sql, rt.allowed_tables)
    verdict = {"level": fresh.level, "action": fresh.action, "tables": fresh.tables,
               "needs_confirm": fresh.needs_confirm, "notes": fresh.notes, "reasons": fresh.reasons}
    checks = state.get("checks") or {}
    if (fresh.level == "禁止" or fresh.normalized_sql != sql.strip()
            or not checks.get("passed")
            or checks.get("sql_hash") != now_hash(sql)):
        return {"verdict": verdict, "status": "failed", "last_agent": "executor",
                "next_agent": "reviewer", "result": {"ok": False, "kind": "blocked",
                "summary": "执行门禁拒绝：SQL 风险或校验未通过"}}

    # ============ 执行器是**唯一**访问数据库的智能体 ============
    #
    # 第 ① 步：EXPLAIN —— 不执行语句，只让 PostgreSQL 做完整的语法与语义分析。
    # 「表不存在 / 字段不存在 / 类型不匹配」在这里**第一次真实暴露**。
    # 报错原话直接交给修正智能体：不加工、不猜测、更不去找别的表替代。
    #
    # 这是纯读操作，没有副作用，所以在 interrupt 之前跑是安全的
    # （interrupt 一旦恢复，它之前的代码会重跑）。
    explain: dict = {}
    if sql.strip():
        explain = await rt.db.explain(sql)
    else:
        explain = {"ok": False, "error": "没有可执行的 SQL"}

    if not explain.get("ok"):
        err_text = str(explain.get("error") or "EXPLAIN 失败")
        fail_id = f"executor-r{round_no}-c{state.get('cursor', 0)}"
        await rt.emit(run_id, "executor", "node_start",
                      {"role": "executor", "sub_task_id": fail_id,
                       "risk_level": verdict.get("level")},
                      version=version, round_no=round_no)
        await _claim(rt, state, "executor", "execute")
        await rt.ledger.finish_task(run_id, fail_id, 1, "failed",
                                    result_ref="exec-explain-err", error=err_text[:500])
        await rt.ledger.audit_sql(
            run_id, round_no, draft.get("sub_task_id", ""), "failed",
            sql, now_hash(sql), intent=draft.get("intent"),
            risk_level=verdict.get("level"), action=verdict.get("action"),
            tables=verdict.get("tables"), need_confirm=bool(verdict.get("needs_confirm")),
            error=err_text, duration_ms=None)
        attempts_log = list(state.get("attempts") or [])
        attempts_log.append({
            "round": round_no, "sql_hash": now_hash(sql), "sql": sql[:400], "ok": False,
            "error_sig": now_hash(err_text),
            "why": "执行前语义分析失败：" + err_text[:180],
        })
        await rt.emit(run_id, "executor", "node_end",
                      {"summary": f"未执行：数据库不接受这条 SQL（{err_text[:60]}）",
                       "suggests": "fixer"}, version=version, round_no=round_no)
        return {"explain": explain,
                "result": {"ok": False, "kind": "explain_failed", "error": err_text,
                           "summary": "SQL 未通过数据库的语法/语义分析，**没有执行**"},
                "attempts": attempts_log,
                "status": "executing", "version": version,
                "last_agent": "executor", "next_agent": "fixer"}

    # ============ 人工协作：需确认的操作，先暂停 ============
    # interrupt 之前的代码在恢复后**会重跑**，所以这里刻意不做任何副作用 ——
    # 「等待确认」的事件与审计由编排器在捕获 __interrupt__ 时统一落账。
    if bool(verdict.get("needs_confirm")) or verdict.get("level") == "需确认":
        decision = interrupt({
            "type": "confirm_sql",
            "sub_task_id": draft.get("sub_task_id"),
            "intent": draft.get("intent"),
            "sql": sql,
            "sql_hash": now_hash(sql),
            "risk_level": verdict.get("level"),
            "action": verdict.get("action"),
            "tables": verdict.get("tables"),
            "est_rows": explain.get("est_rows"),
            "notes": verdict.get("notes") or [],
            "cascade": catalog.cascade_note(verdict.get("tables") or []),
            "question": "这条会删除/修改数据，需要你确认后才执行。",
        })
        approved = (bool(decision.get("approve")) if isinstance(decision, dict)
                    else decision == "approve")
        approved_by = (decision or {}).get("by") if isinstance(decision, dict) else None
        if approved and (not isinstance(decision, dict) or decision.get("sql_hash") != now_hash(sql)):
            raise RuntimeError("确认记录与当前 SQL 不匹配，必须重新确认")
        if not approved:
            intents = list(state.get("intents") or [])
            cursor = int(state.get("cursor", 0))
            return {"result": {"ok": False, "kind": "cancelled", "rowcount": 0,
                               "summary": "用户取消了执行"},
                    "confirmed": False,
                    "cursor": cursor,
                    "history": _upsert_history(state, executed=False, exec_ok=None,
                                               kind="cancelled", affected_rows=0,
                                               confirmed_by=approved_by,
                                               note="用户取消了执行"),
                    "status": "cancelled", "last_agent": "executor",
                    "next_agent": "reviewer"}
    else:
        approved_by = None

    # ============ 正常执行 ============
    sub_task_id = f"executor-r{round_no}-c{state.get('cursor', 0)}"
    await rt.emit(run_id, "executor", "node_start",
                  {"role": "executor", "sub_task_id": sub_task_id,
                   "risk_level": verdict.get("level")}, version=version, round_no=round_no)
    await _claim(rt, state, "executor", "execute")

    exec_result = await rt.db.execute(sql, max_rows=rt.settings.max_rows,
                                      read_only=verdict.get("level") == "只读")
    attempts = 1
    await rt.ledger.finish_task(
        run_id, sub_task_id, attempts, "completed" if exec_result["ok"] else "failed",
        result_ref=f"exec-{'ok' if exec_result['ok'] else 'err'}",
        error=None if exec_result["ok"] else str(exec_result.get("error"))[:500])

    exec_sql_id = await rt.ledger.audit_sql(
        run_id, round_no, draft.get("sub_task_id", ""),
        "executed" if exec_result["ok"] else "failed",
        sql, now_hash(sql), intent=draft.get("intent"),
        risk_level=verdict.get("level"), action=verdict.get("action"),
        tables=verdict.get("tables"), est_rows=explain.get("est_rows"),
        affected_rows=exec_result.get("rowcount"),
        need_confirm=bool(verdict.get("needs_confirm")),
        confirmed_by=approved_by or ("用户已确认" if state.get("confirmed") else None),
        error=exec_result.get("error"), duration_ms=exec_result.get("duration_ms"))

    new_version = await rt.ledger.set_run_status(run_id, "executing", round_no) or version
    await rt.emit(run_id, "executor", "result", exec_result,
                  version=version, round_no=round_no)

    # 执行结果由数据库给出，不再让模型点评每次成功/失败。
    out = ExecutorOutput(ok=exec_result["ok"],
                         summary=(f"数据库执行成功，返回/影响 {exec_result.get('rowcount')} 行"
                                  if exec_result["ok"] else "执行失败：" + str(exec_result.get("error") or "未知错误")),
                         error_hint=str(exec_result.get("error") or ""))

    entry_lifecycle = dict(
        executed=True, exec_ok=exec_result["ok"],
        confirmed_by=approved_by or ("用户已确认" if state.get("confirmed") else None),
        affected_rows=exec_result.get("rowcount"),
        duration_ms=exec_result.get("duration_ms"),
        error=exec_result.get("error"), kind=exec_result.get("kind"),
    )
    # 查询结果要跟着子任务一起留下：汇总审查器最后要给用户一张**看得见的表**，
    # 只告诉它"返回 8 行"等于什么也没说。这里把列与行带进 history。
    if exec_result.get("kind") == "rows":
        entry_lifecycle["columns"] = exec_result.get("columns") or []
        entry_lifecycle["rows"] = (exec_result.get("rows") or [])[:50]
        entry_lifecycle["truncated"] = bool(exec_result.get("truncated"))
        entry_lifecycle["sampled"] = len(exec_result.get("rows") or []) > 50

    # ⑥ 写**共享记忆** —— 这一步是「连续性」的落点。
    #
    # 只有**执行成功**才写，而且 verified 只在这里置位：
    # 模型说的不算，只有执行器真的把它跑过才算事实。
    # 来源（sql_id + 原文）一并存下 —— 文档要求「不能取代权威数据源」，要核对能回去。
    memory = list(state.get("memory") or [])
    if exec_result["ok"]:
        claim = f"数据库执行成功，返回/影响 {exec_result.get('rowcount')} 行"
        mem = {
            "sub_task_id": draft.get("sub_task_id") or "",
            "claim": f"{draft.get('intent') or '本步'} —— {claim}",
            "sql_id": exec_sql_id,
            "sql_text": sql,
            "action": verdict.get("action"),
            "stage": "executed",
            "verified": True,
            "entities": _entities_from(exec_result, verdict.get("tables") or []),
            "columns": exec_result.get("columns") or [],
            "rows": (exec_result.get("rows") or [])[:50],
            "rowcount": exec_result.get("rowcount"),
            "truncated": bool(exec_result.get("truncated")),
            "sampled": len(exec_result.get("rows") or []) > 50,
            "interpretation": out.summary or "",
            "interpretation_verified": False,
        }
        await rt.ledger.save_memory(
            run_id, mem["sub_task_id"], mem["claim"],
            sql_id=exec_sql_id, sql_text=sql, action=mem["action"],
            stage="executed", verified=True, entities=mem["entities"],
            columns=mem["columns"], rows=mem["rows"],
            rowcount=mem["rowcount"], round_no=round_no,
            truncated=mem["truncated"], sampled=mem["sampled"],
            interpretation=mem["interpretation"], interpretation_verified=False)
        # 读回最新的记忆列表，交给下游 —— 不额外查库，就这一条
        memory = await rt.ledger.list_memory(run_id)
        await rt.emit(run_id, "executor", "memory",
                      {"saved": mem["sub_task_id"], "claim": mem["claim"],
                       "sql_id": exec_sql_id,
                       "entities": mem["entities"], "rowcount": mem["rowcount"],
                       "verified": True},
                      version=version, round_no=round_no)

    # 执行失败也要进 attempts —— 否则「执行反复失败」检测不到
    attempts_log = list(state.get("attempts") or [])
    if not exec_result["ok"]:
        err_text = str(exec_result.get("error") or "")
        attempts_log.append({
            "round": round_no, "sql_hash": now_hash(sql), "sql": sql[:400], "ok": False,
            "error_sig": now_hash(err_text), "why": "执行失败：" + err_text[:180],
        })
    await rt.emit(run_id, "executor", "node_end",
                  {"summary": out.summary or ("执行成功" if exec_result["ok"] else "执行失败"),
                   "suggests": out.handoff_to}, version=version, round_no=round_no)

    # ⚠ 关键：执行成功后**推进游标**，否则路由会一直回到生成智能体，图就死循环了
    intents = list(state.get("intents") or [])
    cursor_now = int(state.get("cursor", 0))
    next_cursor = min(cursor_now + 1, len(intents)) if exec_result["ok"] else cursor_now

    return {"result": {**exec_result, "model_summary": out.summary,
                       "error_hint": out.error_hint},
            "explain": explain,
            "confirmed": bool(state.get("confirmed")),
            "cursor": next_cursor,
            "attempts": attempts_log,
            "memory": memory,
            "history": _upsert_history(state, **entry_lifecycle),
            "status": "executing", "version": new_version, "last_agent": "executor",
            "next_agent": out.handoff_to or ("reviewer" if exec_result["ok"] else "fixer")}


# ---------------------------------------------------------------- 修正智能体

async def fixer(state, rt) -> dict:
    # 本次修正的节点身份、重试和模型上下文统一使用新 round。
    state = {**state, "round": int(state.get("round", 0)) + 1}
    run_id = state["global_task_id"]
    version = state.get("version", 0)
    round_no = int(state.get("round", 0))
    draft = state.get("draft") or {}
    checks = state.get("checks") or {}
    result = state.get("result") or {}
    intent = _current_intent(state)
    sub_task_id = await _claim(rt, state, "fixer", "fix")

    await rt.emit(run_id, "fixer", "node_start",
                  {"role": "fixer", "sub_task_id": sub_task_id,
                   "from_round": round_no - 1}, version=version, round_no=round_no)

    failed = [c for c in (checks.get("checks") or []) if c.get("result") == "fail"]
    # 把**已经试过并失败的 SQL**一并给它 —— 否则它会一遍遍生成类似的东西，
    # 正是「修正没有进展却没人发现」的根源
    tried = [a for a in (state.get("attempts") or []) if not a.get("ok")]
    system = prompts.FIXER_SYSTEM
    user = prompts.FIXER_USER.format(
        question=state.get("question", ""),
        handoff=_handoff(state, rt, intent),
        tables="、".join(draft.get("tables") or []) or "（未声明）",
        sql=draft.get("sql", ""),
        failed=_dump(failed) if failed else "（无）",
        checks_reason=checks.get("reason", ""),
        db_error=(state.get("explain") or {}).get("error") or "（数据库没有报错）",
        exec_error=result.get("error") or "（无）",
        error_hint=result.get("error_hint") or "（无）",
        tried=(_dump([{"round": a.get("round"), "sql": a.get("sql"), "why": a.get("why")}
                      for a in tried]) if tried else "（这是第一次修正）"),
        allowed="、".join(sorted(rt.allowed_tables)),
        detail=catalog.detail_text(draft.get("tables")))
    out, attempts, err = await _call(
        rt, FixerOutput, system, user,
        lambda: mock_llm.mock_fixer(draft, checks, result,
                                    catalog.detail_text(draft.get("tables")),
                                    rt.allowed_tables),
        ctx=_ctx(state, "fixer", "修正 SQL"))
    if err is not None or out is None:
        return await _degrade(rt, state, "fixer", sub_task_id, version, err)

    # 修正智能体自己判断「修不了」—— 尊重它，别再空转
    if out.give_up:
        await rt.ledger.finish_task(run_id, sub_task_id, 1, "failed", error=out.give_up[:500])
        await rt.emit(run_id, "fixer", "give_up",
                      {"reason": out.give_up}, version=version, round_no=round_no)
        await rt.emit(run_id, "fixer", "node_end",
                      {"summary": f"判断无法修复：{one_line(out.give_up)[:50]}"},
                      version=version, round_no=round_no)
        return {"give_up": out.give_up, "status": "fixing", "round": round_no,
                "last_agent": "fixer", "next_agent": "reviewer"}

    attempt = await _settle_attempts(rt, state, "fixer", "fix", attempts)
    new_draft = out.draft.model_dump()
    new_draft["round"] = round_no
    # 同上：sub_task_id 必须由运行时钉死。之前这里没钉，
    # 模型没带对就会让护栏误判成「当前子任务还没有 SQL」→ 回生成器重造 →
    # **把修正器的成果整个覆盖掉**，看起来就像"修正没有进展"。
    new_draft["sub_task_id"] = intent.get("sub_task_id") or new_draft.get("sub_task_id")
    # 表清单**继承生成器筛出的那一份**，修正器无权扩大范围 ——
    # 于是"把不存在的表换成另一张同构表"在运行时层直接被判越权
    new_draft["tables"] = list(draft.get("tables") or [])
    v = sql_guard.analyze(new_draft["sql"], rt.allowed_tables)
    if v.ok and v.normalized_sql:
        new_draft["sql"] = v.normalized_sql
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
    verdict_now = state.get("verdict") or {}
    blocked = ""
    if verdict_now.get("level") == "禁止":
        blocked = "；".join(verdict_now.get("reasons") or []) or "静态防线判定禁止"
    report = validators.validate(
        allowed_tables=rt.allowed_tables,
        intents=list(state.get("intents") or []),
        history=list(state.get("history") or []),
        tasks=tasks, version=version, round_no=round_no,
        refusal=state.get("refusal") or "",
        mode=state.get("mode") or "execute",
        blocked=blocked,
    )
    await rt.emit(run_id, "reviewer", "consistency", report.model_dump(),
                  version=version, round_no=round_no)

    history = list(state.get("history") or [])
    mode = state.get("mode") or "execute"
    # ② 交给模型汇总 —— 答复的形状取决于**用户要什么**，不是千篇一律的工作总结
    mode_rule = prompts.REVIEWER_MODE_RULE.get(mode, "")
    blocked_rule = (prompts.REVIEWER_BLOCKED_RULE
                    if (state.get("verdict") or {}).get("level") == "禁止" else "")
    system = prompts.REVIEWER_SYSTEM.format(mode_rule=mode_rule, blocked_rule=blocked_rule)
    plan_brief = {"understanding": state.get("understanding"),
                  "refusal": state.get("refusal"),
                  "mode": mode,
                  "safety": state.get("safety"),
                  "chat_reply": state.get("chat_reply"),
                  "复查结论": state.get("plan_note") or None,
                  "intents": state.get("intents") or []}
    # 共享记忆：每条都带 sql_id 与来源原文 —— 答复要「基于谁的结果」时就有据可依
    memory_brief = [{"sub_task_id": m.get("sub_task_id"), "claim": _fact_claim(m),
                     "sql_id": m.get("sql_id"), "rowcount": m.get("rowcount"),
                     "truncated": m.get("truncated", True), "sampled": m.get("sampled", True),
                     "sql": m.get("sql_text")}
                    for m in (state.get("memory") or [])]
    user = prompts.REVIEWER_USER.format(
        question=state.get("question", ""),
        prior=_dump((state.get("prior_turns") or [])[-3:]),
        plan=_dump(plan_brief),
        error=state.get("error") or "（无）",
        history=_dump(history),
        memory=_dump(memory_brief) if memory_brief else "（没有：这一轮没有执行过任何语句）",
        result=_dump(state.get("result") or {}),
        report=_dump(report.model_dump()))
    out, attempts, err = await _call(
        rt, ReviewerOutput, system, user,
        lambda: mock_llm.mock_reviewer(report, history, state),
        ctx=_ctx(state, "reviewer", "汇总并写答复"))
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

    status = ("cancelled" if state.get("status") == "cancelled" else
              "done" if out.decision == "approve" else "failed")
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

    return {"status": status,
            "version": new_version, "last_agent": "reviewer", "next_agent": "done",
            "last_consistency": report.model_dump()}
