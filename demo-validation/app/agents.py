"""四个智能体节点：规划器 / 研究员 / 执行器 / 审查器（通用版，无快照）。

共同骨架：
    node_start → 认领子任务（租约 + 幂等键）→ 调模型（失败重试 1 次）
    → 结果落账 → node_end

通用版的要点：
  · 用户只给「问题 + 资料」，智能体自己去读资料；
  · 每条结论 / 答案都必须给 `citations`（哪段资料 + 原文摘录）；
  · 摘录必须是**复制粘贴**，不许改写 —— 运行时逐字校验。
"""

from __future__ import annotations

import json

from . import mock_llm, validators
from .state import (
    ExecutorOutput,
    PlannerOutput,
    ResearcherOutput,
    ReviewCheck,
    ReviewerOutput,
)


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- 公共骨架

_CONTRACT_HINT = (
    "\n输出要求：嵌套字段必须是对象/数组本身，不要序列化成 JSON 字符串；"
    "列表字段请输出真正的数组。\n"
    "引用要求：citations 里的 quote 必须是从资料里**逐字复制**的原文片段，"
    "不要改写、不要润色、不要加省略号，且必须完整落在同一段资料内。"
)


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
    sub_task_id = f"{role}-r{round_no}"
    chunks = [c["chunk_id"] for c in await rt.ledger.list_chunks(run_id)]
    await rt.ledger.open_task(
        run_id=run_id, sub_task_id=sub_task_id, attempt=attempt, role=role, agent_id=role,
        context={"question": state.get("question", ""), "visible_chunks": chunks,
                 "sub_tasks": [t.get("sub_task_id") for t in (state.get("sub_tasks") or [])]},
        idempotency_key=f"{run_id}:r{round_no}:{role}:{action}:a{attempt}",
        lease_seconds=rt.settings.lease_seconds,
    )
    return sub_task_id


async def _settle_attempts(rt, state, role: str, action: str, attempts: int) -> int:
    if attempts <= 1:
        return 1
    run_id = state["global_task_id"]
    round_no = state.get("round", 0)
    sub_task_id = f"{role}-r{round_no}"
    await rt.ledger.finish_task(run_id, sub_task_id, 1, "failed",
                                error="结构化输出未通过契约校验，已重试")
    await _claim(rt, state, role, action, attempt=2)
    return 2


async def _fail(rt, state, role: str, sub_task_id: str, round_no: int, version: int, err) -> None:
    run_id = state["global_task_id"]
    message = str(err)[:500]
    await rt.ledger.finish_task(run_id, sub_task_id, 1, "failed", error=message)
    await rt.emit(run_id, role, "error", {"where": role, "message": message},
                  version=version, round_no=round_no)


def _material_text(chunks: list[dict], limit: int = 24000) -> str:
    """把资料渲染成给模型看的文本（带片段 ID）。"""
    lines: list[str] = []
    total = 0
    for c in chunks:
        block = f"【{c['chunk_id']}】{c['content']}"
        if total + len(block) > limit:
            lines.append(f"……（资料过长，此处截断，共 {len(chunks)} 段）")
            break
        lines.append(block)
        total += len(block)
    return "\n\n".join(lines)


# ---------------------------------------------------------------- Planner

async def planner(state, rt) -> dict:
    run_id = state["global_task_id"]
    round_no = state.get("round", 0)
    version = state.get("version", 0)
    sub_task_id = f"planner-r{round_no}"
    chunks = await rt.ledger.list_chunks(run_id)

    await rt.emit(run_id, "planner", "node_start",
                  {"role": "planner", "round": round_no, "sub_task_id": sub_task_id,
                   "chunk_count": len(chunks)},
                  version=version, round_no=round_no)
    await _claim(rt, state, "planner", "plan")

    system = (
        "你是规划器。用户会给你一个问题和他自己提供的资料。\n"
        "你的任务：把这个问题拆成 2-5 个子问题，每个子问题都能靠资料里的一部分回答。\n"
        "硬性要求：\n"
        "1. 每个子问题有稳定唯一的 sub_task_id；\n"
        "2. why 要写清：这个子问题需要资料里的哪部分才能回答；\n"
        "3. depends_on 写真实依赖，能并行的留空；\n"
        "4. 拆解要覆盖用户问题的全部方面，不要漏。"
    )
    user = (
        f"用户的问题：{state.get('question', '')}\n\n"
        f"用户提供的资料共 {len(chunks)} 段，片段清单：\n"
        f"{_dump([{'id': c['chunk_id'], 'length': c['char_len'], 'head': c['content'][:40]} for c in chunks])}\n\n"
        "请拆解这个问题。"
    )

    out, attempts, err = await _call(rt, PlannerOutput, system, user,
                                     lambda: mock_llm.mock_planner(round_no, state.get("question", ""), chunks))
    if err is not None or out is None:
        await _fail(rt, state, "planner", sub_task_id, round_no, version, err)
        raise RuntimeError(f"planner 失败：{err}")

    attempt = await _settle_attempts(rt, state, "planner", "plan", attempts)
    sub_tasks = [t.model_dump() for t in out.sub_tasks]
    await rt.ledger.finish_task(run_id, sub_task_id, attempt, "completed",
                                result_ref=f"plan-r{round_no}", citations=[])
    new_version = await rt.ledger.set_run_status(run_id, "planning", round_no) or version

    await rt.emit(run_id, "planner", "handoff",
                  {"from": "planner", "to": "researcher",
                   "label": f"交接 {len(sub_tasks)} 个子问题",
                   "handoff": {"sub_tasks": sub_tasks}},
                  version=version, round_no=round_no)
    await rt.emit(run_id, "planner", "node_end",
                  {"summary": f"拆出 {len(sub_tasks)} 个子问题",
                   "plan_reason": out.plan_reason, "sub_tasks": sub_tasks,
                   "attempts": attempts},
                  version=version, round_no=round_no)

    return {"sub_tasks": sub_tasks, "status": "planning", "version": new_version}


# ---------------------------------------------------------------- Researcher

async def researcher(state, rt) -> dict:
    run_id = state["global_task_id"]
    round_no = state.get("round", 0)
    version = state.get("version", 0)
    sub_task_id = f"researcher-r{round_no}"
    chunks = await rt.ledger.list_chunks(run_id)
    chunk_ids = [c["chunk_id"] for c in chunks]

    await rt.emit(run_id, "researcher", "node_start",
                  {"role": "researcher", "round": round_no, "sub_task_id": sub_task_id,
                   "chunk_ids": chunk_ids},
                  version=version, round_no=round_no)
    await _claim(rt, state, "researcher", "research")

    system = (
        "你是研究员。只依据用户提供的资料回答，不许引入资料之外的知识。\n"
        "硬性要求：\n"
        "2. 每个子问题都要给出结论，sub_task_id 必须用规划器给出的 ID；\n"
        "3. 每条结论的 citations 至少一条，quote 必须是资料原文的**逐字复制**；\n"
        "4. source_id 只能是资料片段清单里出现过的 ID，禁止编造；\n"
        "5. 结论状态：候选 / 已验证 / 冲突 / 已否决；\n"
        "6. 如果资料里确实没有与某个子问题相关的内容，把该结论的 insufficient 置为 true，"
        "claim 明确写成「资料中没有相关内容」—— 不要编造依据；\n"
        "7. 资料没写的推测写进 assumptions，不要伪装成结论。"
    )
    user = (
        f"用户的问题：{state.get('question', '')}\n\n"
        f"要回答的子问题：\n{_dump(state.get('sub_tasks') or [])}\n\n"
        f"用户提供的资料：\n{_material_text(chunks)}\n\n"
        "请逐个子问题给出结论。"
    )

    out, attempts, err = await _call(
        rt, ResearcherOutput, system, user,
        lambda: mock_llm.mock_researcher(round_no, state.get("sub_tasks") or [], chunks),
    )
    if err is not None or out is None:
        await _fail(rt, state, "researcher", sub_task_id, round_no, version, err)
        raise RuntimeError(f"researcher 失败：{err}")

    attempt = await _settle_attempts(rt, state, "researcher", "research", attempts)

    new_findings: list[dict] = []
    for idx, f in enumerate(out.findings):
        fid = f"{run_id}-f{round_no}-{idx + 1}"
        new_findings.append({
            "finding_id": fid, "round": round_no,
            "sub_task_id": f.sub_task_id, "claim": f.claim, "status": f.status,
            "citations": [c.model_dump() for c in f.citations],
            "insufficient": bool(f.insufficient),
            "assumptions": list(f.assumptions),
        })

    covered = {f["sub_task_id"] for f in new_findings}
    all_cites = [c for f in new_findings for c in f["citations"]]
    all_assumptions = [a for f in new_findings for a in f["assumptions"]]

    for item in new_findings:
        await rt.ledger.insert_finding(
            item["finding_id"], run_id, item["sub_task_id"], round_no,
            item["claim"], item["status"], item["citations"], item["assumptions"],
            insufficient=item["insufficient"],
        )
        await rt.emit(run_id, "researcher", "finding", item,
                      version=version, round_no=round_no, citations=item["citations"])

    await rt.ledger.finish_task(run_id, sub_task_id, attempt, "completed",
                                result_ref=f"findings-r{round_no}", citations=all_cites)
    await rt.emit(run_id, "researcher", "handoff",
                  {"from": "researcher", "to": "executor",
                   "label": (f"交接 {len(new_findings)} 条结论 · 覆盖 {len(covered)} 个子问题"
                             f" · 引用 {len(all_cites)} 段原文"),
                   "handoff": {"findings": new_findings, "assumptions": all_assumptions}},
                  version=version, round_no=round_no, citations=all_cites)

    new_version = await rt.ledger.set_run_status(run_id, "researching", round_no) or version
    await rt.emit(run_id, "researcher", "node_end",
                  {"summary": f"产出 {len(new_findings)} 条结论，覆盖 {len(covered)} 个子问题",
                   "attempts": attempts},
                  version=version, round_no=round_no)

    return {"findings": list(state.get("findings") or []) + new_findings,
            "status": "researching", "version": new_version}


# ---------------------------------------------------------------- Executor

async def executor(state, rt) -> dict:
    run_id = state["global_task_id"]
    round_no = state.get("round", 0)
    version = state.get("version", 0)
    sub_task_id = f"executor-r{round_no}"
    findings = list(state.get("findings") or [])
    chunks = await rt.ledger.list_chunks(run_id)

    await rt.emit(run_id, "executor", "node_start",
                  {"role": "executor", "round": round_no, "sub_task_id": sub_task_id},
                  version=version, round_no=round_no)
    await _claim(rt, state, "executor", "answer")

    system = (
        "你是执行器。把研究结论汇总成**直接回答用户问题**的答案。\n"
        "硬性要求：\n"
        "1. summary 一句话直接回答问题；\n"
        "2. points 必须覆盖规划器拆出的**每一个**子问题（sub_task_id 一一对应），一个都不能漏；\n"
        "3. 每个 point 的 citations 只能引用研究结论里出现过的资料片段，quote 逐字复制；\n"
        "4. 若研究结论说明资料里没有相关内容，该分点仍要保留，但把 insufficient 置为 true，"
        "statement 明确写「资料中没有相关内容」—— 不许编；\n"
        "5. acceptance 逐条写清「怎样算回答到位」，每条都要有原文依据；\n"
        "6. 资料不足或有争议的地方，诚实写进 caveats。"
    )
    user = (
        f"用户的问题：{state.get('question', '')}\n\n"
        f"规划器拆出的子问题：\n{_dump(state.get('sub_tasks') or [])}\n\n"
        f"研究结论：\n{_dump(findings)}\n\n"
        f"资料片段清单：{_dump([c['chunk_id'] for c in chunks])}\n\n"
        "请输出答案。"
    )

    out, attempts, err = await _call(
        rt, ExecutorOutput, system, user,
        lambda: mock_llm.mock_executor(round_no, state.get("sub_tasks") or [], findings, chunks),
    )
    if err is not None or out is None:
        await _fail(rt, state, "executor", sub_task_id, round_no, version, err)
        raise RuntimeError(f"executor 失败：{err}")

    attempt = await _settle_attempts(rt, state, "executor", "answer", attempts)
    answer = out.answer.model_dump()
    answer["round"] = round_no
    cites = [c for p in answer.get("points", []) for c in (p.get("citations") or [])]

    await rt.ledger.finish_task(run_id, sub_task_id, attempt, "completed",
                                result_ref=answer.get("answer_id"), citations=cites)
    await rt.emit(run_id, "executor", "answer", answer,
                  version=version, round_no=round_no, citations=cites)
    await rt.emit(run_id, "executor", "handoff",
                  {"from": "executor", "to": "reviewer",
                   "label": (f"交接答案 {answer.get('answer_id')}"
                             f" · 覆盖 {len(answer.get('points') or [])} 个子问题"
                             f" · 验收 {len(answer.get('acceptance') or [])} 项"),
                   "handoff": {"answer_id": answer.get("answer_id"),
                               "acceptance": answer.get("acceptance") or []}},
                  version=version, round_no=round_no, citations=cites)

    new_version = await rt.ledger.set_run_status(run_id, "answering", round_no) or version
    await rt.emit(run_id, "executor", "node_end",
                  {"summary": f"产出答案，覆盖 {len(answer.get('points') or [])} 个子问题",
                   "attempts": attempts},
                  version=version, round_no=round_no)

    return {"answers": list(state.get("answers") or []) + [answer],
            "status": "answering", "version": new_version}


# ---------------------------------------------------------------- Reviewer

async def reviewer(state, rt) -> dict:
    run_id = state["global_task_id"]
    round_no = state.get("round", 0)
    version = state.get("version", 0)
    sub_task_id = f"reviewer-r{round_no}"
    findings = list(state.get("findings") or [])
    answers = list(state.get("answers") or [])
    answer = answers[-1] if answers else None
    chunks = await rt.ledger.chunk_map(run_id)
    tasks = await rt.ledger.list_tasks(run_id)

    await rt.emit(run_id, "reviewer", "node_start",
                  {"role": "reviewer", "round": round_no, "sub_task_id": sub_task_id},
                  version=version, round_no=round_no)
    await _claim(rt, state, "reviewer", "review")

    # ① 运行时先给出确定性结论
    report = validators.validate(
        question=state.get("question", ""), chunks=chunks,
        sub_tasks=state.get("sub_tasks") or [], findings=findings, answers=answers,
        tasks=tasks, version=version, round_no=round_no,
    )
    await rt.emit(run_id, "reviewer", "consistency", report.model_dump(),
                  version=version, round_no=round_no)

    # ② 把报告当成「运行时证明」交给模型
    system = (
        "你是审查器。你要决定这份答案能不能交给用户。\n"
        "硬性要求：\n"
        "1. 逐条核对：每个分点是否真的被引用的原文支持（回到原文看，不要只看格式）；\n"
        "2. 只要『运行时一致性校验报告』里有任何一条 fail，就必须 veto；\n"
        "3. veto 时给出 blockers，并指定 rework_target（researcher 或 executor）；\n"
        "4. 你的结论必须改变后续动作，否则只是评论。"
    )
    user = (
        f"用户的问题：{state.get('question', '')}\n\n"
        f"研究结论：{_dump(findings)}\n\n"
        f"待审答案：{_dump(answer)}\n\n"
        f"运行时一致性校验报告（系统给出的确定性证据，不是你的主观判断）：\n{_dump(report.model_dump())}\n\n"
        "请给出评审结论。"
    )

    out, attempts, err = await _call(rt, ReviewerOutput, system, user,
                                     lambda: mock_llm.mock_reviewer(round_no, report.model_dump()))
    if err is not None or out is None:
        await _fail(rt, state, "reviewer", sub_task_id, round_no, version, err)
        raise RuntimeError(f"reviewer 失败：{err}")

    attempt = await _settle_attempts(rt, state, "reviewer", "review", attempts)

    # ③ 硬门禁：校验器 fail 时，模型的 approve 一律无效
    forced = False
    if not report.passed and out.decision == "approve":
        forced = True
        fails = [r for r in report.results if r.result == "fail"]
        out = ReviewerOutput(
            decision="veto",
            checks=list(out.checks) + [
                ReviewCheck(check_id=f"rt-{r.invariant_id}", target=r.invariant_id,
                            result="fail", source_id=None, note=f"运行时强制：{r.detail}")
                for r in fails
            ],
            blockers=list(out.blockers) + [f"{r.invariant_id}: {r.detail}" for r in fails],
            reason="运行时一致性校验未通过，已强制否决（模型原本给出 approve）。",
            rework_target=out.rework_target or "researcher",
        )

    veto = out.decision == "veto"
    if veto:
        next_round = round_no + 1
        status = "failed" if next_round >= rt.settings.max_rounds else "researching"
    else:
        next_round = round_no
        status = "done"

    await rt.ledger.finish_task(run_id, sub_task_id, attempt, "completed",
                                result_ref=f"review-r{round_no}:{'veto' if veto else 'approve'}")

    # 结论状态推进：通过后「候选」才升级为「已验证」
    if not veto:
        await rt.ledger.set_run_findings_status(run_id, "已验证")
        for f in findings:
            f["status"] = "已验证"

    new_version = await rt.ledger.set_run_status(run_id, status, next_round) or version

    await rt.emit(run_id, "reviewer", "review",
                  {"decision": out.decision, "forced_by_runtime": forced,
                   "checks": [c.model_dump() for c in out.checks],
                   "blockers": list(out.blockers), "reason": out.reason,
                   "rework_target": out.rework_target,
                   "consistency_passed": report.passed,
                   "version_before": version, "version_after": new_version,
                   "round_after": next_round},
                  version=new_version, round_no=round_no)
    await rt.emit(run_id, "reviewer", "node_end",
                  {"summary": "通过" if not veto else f"否决，退回 {out.rework_target or 'researcher'}",
                   "attempts": attempts},
                  version=new_version, round_no=round_no)

    return {"review": out.model_dump(), "consistency": report.model_dump(),
            "findings": findings, "status": status, "version": new_version, "round": next_round}
