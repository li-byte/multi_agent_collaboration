"""离线兜底：LLM_MODE=mock 时用的预置输出。

行为与真实模式一致 —— 只能引用用户给的资料，
而且引用的 quote 都是从资料里**真实切出来的子串**，所以逐字校验一定通过。

注意：这里**刻意**让 mock 的审查器只做「结构自查」，只要答案齐整就 approve。
目的是演示硬门禁：模型的 approve 会被运行时的一致性校验推翻 ——
这正是全篇的主张「模型负责判断，运行时负责证明」。
"""

from __future__ import annotations

from .sources import normalize
from .state import (
    AcceptanceItem,
    Answer,
    AnswerPoint,
    Citation,
    ExecutorOutput,
    Finding,
    PlannerOutput,
    ResearcherOutput,
    ReviewCheck,
    ReviewerOutput,
    SubTask,
)


def _usable(chunks: list[dict]) -> list[dict]:
    return [c for c in chunks if len(normalize(c["content"])) >= 8]


def _quote(chunk: str, n: int = 42) -> str:
    """从资料里切一段原文当引用 —— 必然是逐字子串，校验必过。"""
    text = (chunk or "").strip()
    return text if len(text) <= n else text[:n]


# ---------------------------------------------------------------- Planner

def mock_planner(round_no: int, question: str, chunks: list[dict]) -> PlannerOutput:
    return PlannerOutput(
        plan_reason=(
            "按「资料说了什么 → 能据此得出什么」拆解："
            "先确认资料中与本问题直接相关的表述，再看资料给出的条件/限制，最后汇总成结论。"
        ),
        sub_tasks=[
            SubTask(sub_task_id="st-1", question="资料中与本问题直接相关的表述是什么？",
                    why="需要资料里明确的说法作为依据", depends_on=[]),
            SubTask(sub_task_id="st-2", question="资料给出了哪些条件或限制？",
                    why="需要资料里的限定条件，结论才稳妥", depends_on=["st-1"]),
            SubTask(sub_task_id="st-3", question="综合以上，可以给出什么结论或建议？",
                    why="需要前两个子问题的结论才能汇总", depends_on=["st-1", "st-2"]),
        ],
    )


# ---------------------------------------------------------------- Researcher

def mock_researcher(round_no: int, sub_tasks: list[dict], chunks: list[dict]) -> ResearcherOutput:
    usable = _usable(chunks)
    findings: list[Finding] = []
    for i, st in enumerate(sub_tasks):
        sid = st.get("sub_task_id", f"st-{i + 1}")
        if not usable:
            findings.append(Finding(
                sub_task_id=sid, claim="资料中没有与这个子问题相关的内容",
                status="候选", citations=[], insufficient=True,
                assumptions=["资料为空或过短"],
            ))
            continue
        c = usable[i % len(usable)]
        findings.append(Finding(
            sub_task_id=sid,
            claim=f"资料 {c['chunk_id']} 提到：{_quote(c['content'], 34)}",
            status="已验证",
            citations=[Citation(source_id=c["chunk_id"], quote=_quote(c["content"]))],
            assumptions=[],
        ))
    return ResearcherOutput(findings=findings)


# ---------------------------------------------------------------- Executor

def mock_executor(round_no: int, sub_tasks: list[dict], findings: list[dict],
                  chunks: list[dict]) -> ExecutorOutput:
    by_sub: dict[str, dict] = {}
    for f in findings:
        by_sub.setdefault(f.get("sub_task_id"), f)

    points: list[AnswerPoint] = []
    for st in sub_tasks:
        f = by_sub.get(st.get("sub_task_id"))
        if f and f.get("citations"):
            points.append(AnswerPoint(
                sub_task_id=st["sub_task_id"],
                statement=f.get("claim", ""),
                citations=[Citation(**c) for c in f["citations"]],
            ))
        else:
            # 每个子问题都要有分点：资料里没有就显式声明「资料不足」，不许漏答也不许编
            points.append(AnswerPoint(
                sub_task_id=st["sub_task_id"],
                statement="资料中没有与这个子问题相关的内容，无法作答。",
                citations=[], insufficient=True,
            ))

    grounded = [p for p in points if p.citations]
    acceptance = [
        AcceptanceItem(item_id=f"ac-{i + 1}",
                       statement=f"{p.sub_task_id} 的结论有原文依据支撑",
                       citations=p.citations)
        for i, p in enumerate(grounded)
    ]
    if not acceptance:
        acceptance = [AcceptanceItem(item_id="ac-1", statement="资料中没有可验证的内容",
                                     citations=[], insufficient=True)]

    if grounded:
        summary = f"根据你提供的资料：{grounded[0].statement}"
    else:
        summary = "你提供的资料里没有能回答这个问题的内容。"

    caveats = [f"{p.sub_task_id}：资料中没有相关内容" for p in points if p.insufficient]

    return ExecutorOutput(answer=Answer(
        answer_id=f"ans-{round_no + 1}",
        summary=summary,
        points=points,
        recommendations=[p.statement for p in points[1:]] or [],
        acceptance=acceptance,
        caveats=caveats,
    ))


# ---------------------------------------------------------------- Reviewer

def mock_reviewer(round_no: int, consistency: dict | None) -> ReviewerOutput:
    """离线兜底：只做「结构自查」，齐整就 approve。

    第 1 轮答案在模型眼里结构完整，但少了子问题覆盖 ——
    模型看不出来，运行时看得出来。这就是硬门禁要演的东西。
    """
    return ReviewerOutput(
        decision="approve",
        checks=[
            ReviewCheck(check_id="ck-1", target="answer.points", result="pass",
                        note="每个分点都带了原文引用，格式完整"),
            ReviewCheck(check_id="ck-2", target="answer.acceptance", result="pass",
                        note="验收条件逐条可核对"),
        ],
        blockers=[],
        reason="仅从答案本身看，论述与依据都成立，同意交付。",
        rework_target=None,
    )
