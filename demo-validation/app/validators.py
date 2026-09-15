"""一致性校验器：运行时的确定性证明（通用版，无快照）。

八条不变量全部与行业无关，只看结构：
  · 有没有给出依据；
  · 依据是不是**逐字来自用户给的资料**（防编造）；
  · 拆出的子问题有没有被**全部**回答（漏一个就否决回流）。

只要有一条 fail，审查器就不得 approve（硬门禁，见 agents.reviewer）。
"""

from __future__ import annotations

from .sources import quote_matches
from .state import ConsistencyReport, InvariantResult

TITLE: dict[str, str] = {
    "V1_answer_cited": "答案的每个分点都要有原文依据（或声明资料不足）",
    "V2_quote_verbatim": "引用的摘录必须能在资料里逐字找到",
    "V3_finding_cited": "每条研究结论都要有原文依据（或声明资料不足）",
    "V4_answer_cites_research": "答案只能引用研究结论引用过的资料片段",
    "V5_acceptance_cited": "每条验收条件都要有原文依据（或声明资料不足）",
    "V6_subtask_coverage": "拆出的每个子问题都要有答案覆盖",
    "V7_version_monotonic": "状态版本必须被推进过",
    "V8_idempotent_side_effect": "所有执行记录必须带幂等键",
}

ORDER = list(TITLE.keys())


def _r(iid: str, ok: bool, detail: str = "", offenders: list[str] | None = None) -> InvariantResult:
    return InvariantResult(
        invariant_id=iid,
        title=TITLE[iid],
        result="pass" if ok else "fail",
        detail=detail,
        offenders=list(offenders or []),
    )


def _cites(items) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for it in items or []:
        for c in (it.get("citations") or []):
            out.append((str(c.get("source_id") or ""), str(c.get("quote") or "")))
    return out


def _grounded(item: dict) -> bool:
    """「有交代」= 给了原文依据，或者显式声明了资料里没有相关内容。

    资料不可能穷尽所有问题，所以「我不知道」必须是一条合法出路 ——
    但它必须是**显式声明**，不能是沉默，更不能是编造。
    """
    return bool(item.get("citations")) or bool(item.get("insufficient"))


def validate(
    *,
    question: str,
    chunks: dict[str, str],
    sub_tasks: list[dict],
    findings: list[dict],
    answers: list[dict],
    tasks: list[dict],
    version: int,
    round_no: int,
) -> ConsistencyReport:
    results: list[InvariantResult] = []
    answer: dict | None = answers[-1] if answers else None

    # ---- V1 答案每个分点都要有交代（依据 或 明确声明资料不足）----
    if answer is None:
        results.append(_r("V1_answer_cited", False, "还没有产出答案"))
    else:
        points = answer.get("points") or []
        bad = [p.get("sub_task_id", "?") for p in points if not _grounded(p)]
        if not points:
            results.append(_r("V1_answer_cited", False, "答案没有任何分点"))
        else:
            results.append(_r(
                "V1_answer_cited", not bad,
                "每个分点都有交代" if not bad
                else f"这些分点既没有依据、也没有声明资料不足：{', '.join(bad)}",
                bad,
            ))

    # ---- V2 逐字命中（核心：防编造）----
    all_cites = _cites(findings) + _cites((answer or {}).get("points")) + _cites((answer or {}).get("acceptance"))
    bad_unknown: list[str] = []
    bad_quote: list[str] = []
    for sid, quote in all_cites:
        if not sid or sid not in chunks:
            bad_unknown.append(f"{sid or '(空)'} ← {quote[:24]}")
        elif not quote_matches(quote, chunks[sid]):
            bad_quote.append(f"{sid} · {quote[:24]}…")
    if not all_cites:
        # 没有任何引用并不可疑：V1/V3 已经保证「每条要么有依据、要么声明资料不足」
        results.append(_r("V2_quote_verbatim", True,
                          "本次没有产生任何引用（相关分点都声明了资料不足）"))
    elif bad_unknown or bad_quote:
        detail = []
        if bad_unknown:
            detail.append(f"{len(bad_unknown)} 条引用了不存在的资料片段")
        if bad_quote:
            detail.append(f"{len(bad_quote)} 条摘录在原文里找不到（可能被改写或编造）")
        results.append(_r("V2_quote_verbatim", False, "；".join(detail), bad_unknown + bad_quote))
    else:
        results.append(_r("V2_quote_verbatim", True, f"{len(all_cites)} 条引用全部逐字命中原文"))

    # ---- V3 每条研究结论都要有交代 ----
    bad3 = [f.get("finding_id", "?") for f in findings if not _grounded(f)]
    results.append(_r(
        "V3_finding_cited", not bad3,
        "每条结论都有交代" if not bad3
        else f"{len(bad3)} 条结论既没有依据、也没有声明资料不足：{', '.join(bad3)}",
        bad3,
    ))

    # ---- V4 答案只能引用研究引用过的片段（防止「研究没查、答案自己编」）----
    research_ids = {sid for sid, _ in _cites(findings) if sid}
    answer_ids = {sid for sid, _ in _cites((answer or {}).get("points")) if sid}
    extra = sorted(answer_ids - research_ids)
    results.append(_r(
        "V4_answer_cites_research", not extra,
        "答案引用的片段都来自研究结论" if not extra
        else f"答案引用了研究没查过的片段：{', '.join(extra)}",
        extra,
    ))

    # ---- V5 验收条件要有交代 ----
    if answer is None:
        results.append(_r("V5_acceptance_cited", False, "还没有产出答案"))
    else:
        acc = answer.get("acceptance") or []
        bad5 = [a.get("item_id", "?") for a in acc if not _grounded(a)]
        if not acc:
            results.append(_r("V5_acceptance_cited", False, "没有声明任何验收条件"))
        else:
            results.append(_r(
                "V5_acceptance_cited", not bad5,
                "每条验收条件都有交代" if not bad5
                else f"这些验收项既没有依据、也没有声明资料不足：{', '.join(bad5)}",
                bad5,
            ))

    # ---- V6 子问题覆盖率（通用版的「漏了没有」检查，确定性回流点）----
    planned = [t.get("sub_task_id", "?") for t in sub_tasks]
    covered = {p.get("sub_task_id") for p in ((answer or {}).get("points") or [])}
    missing = [sid for sid in planned if sid not in covered]
    if not planned:
        results.append(_r("V6_subtask_coverage", False, "规划器没有拆出任何子问题"))
    else:
        results.append(_r(
            "V6_subtask_coverage", not missing,
            f"{len(planned)} 个子问题全部被回答" if not missing
            else f"有 {len(missing)} 个子问题没有被回答：{', '.join(missing)}",
            missing,
        ))

    # ---- V7 版本必须被推进过 ----
    ok7 = version >= round_no + 1
    results.append(_r("V7_version_monotonic", ok7,
                      f"run.version={version}，round={round_no}（每次推进都 version+1）"))

    # ---- V8 幂等键 ----
    lost = [t.get("sub_task_id", "?") for t in tasks if not t.get("idempotency_key")]
    results.append(_r(
        "V8_idempotent_side_effect", not lost,
        "所有执行记录都带幂等键" if not lost else f"缺少幂等键：{', '.join(lost)}",
        lost,
    ))

    return ConsistencyReport(passed=all(r.result == "pass" for r in results), results=results)
