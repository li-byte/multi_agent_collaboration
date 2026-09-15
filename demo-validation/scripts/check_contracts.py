"""契约适配层 + 资料引用校验 自检。

用法：
    python scripts/check_contracts.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.sources import build_chunks, locate, quote_matches, split_material  # noqa: E402
from app.state import (  # noqa: E402
    ExecutorOutput,
    PlannerOutput,
    ResearcherOutput,
    ReviewerOutput,
)

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, fn) -> None:
    try:
        fn()
        PASS.append(name)
        print(f"  ✓ {name}")
    except Exception as exc:  # noqa: BLE001
        FAIL.append(name)
        print(f"  ✕ {name}\n      {type(exc).__name__}: {str(exc)[:200]}")


# ---------------------------------------------------------------- 契约适配

def case_answer_as_json_string() -> None:
    answer = {
        "answer_id": "ans-1",
        "summary": "结论",
        "points": [{"sub_task_id": "st-1", "statement": "x",
                    "citations": [{"source_id": "D1-1", "quote": "原文"}]}],
        "recommendations": [],
        "acceptance": [{"item_id": "ac-1", "statement": "y",
                        "citations": [{"source_id": "D1-1", "quote": "原文"}]}],
        "caveats": [],
    }
    out = ExecutorOutput.model_validate({"answer": json.dumps(answer, ensure_ascii=False)})
    assert out.answer.points[0].citations[0].source_id == "D1-1"


def case_citations_as_text() -> None:
    out = ExecutorOutput.model_validate({
        "answer": {
            "answer_id": "a", "summary": "s",
            "points": [{"sub_task_id": "st-1", "statement": "x",
                        "citations": ["D1-2: 生活方式干预是所有高血压患者的基础治疗"]}],
            "acceptance": [{"item_id": "ac-1", "statement": "y",
                            "citations": "D2-1|每周进行 150 分钟以上的中等强度有氧运动"}],
            "recommendations": "建议一\n建议二",
            "caveats": "",
        }
    })
    assert out.answer.points[0].citations[0].source_id == "D1-2"
    assert out.answer.points[0].citations[0].quote.startswith("生活方式干预")
    assert out.answer.acceptance[0].citations[0].source_id == "D2-1"
    assert out.answer.recommendations == ["建议一", "建议二"]
    assert out.answer.caveats == []


def case_acceptance_as_single_object() -> None:
    out = ExecutorOutput.model_validate({
        "answer": {"answer_id": "a", "summary": "s", "points": [],
                   "acceptance": {"item_id": "ac-1", "statement": "y", "citations": []},
                   "caveats": []}
    })
    assert len(out.answer.acceptance) == 1


def case_planner_subtasks_as_string() -> None:
    payload = {"plan_reason": "r", "sub_tasks": json.dumps(
        [{"sub_task_id": "st-1", "question": "q", "why": "w", "depends_on": ""}],
        ensure_ascii=False)}
    out = PlannerOutput.model_validate(payload)
    assert out.sub_tasks[0].depends_on == []


def case_researcher_citations() -> None:
    out = ResearcherOutput.model_validate({
        "findings": [{"sub_task_id": "st-1", "claim": "c",
                      "citations": [{"source_id": "D1-1", "quote": "q"}],
                      "assumptions": "猜测一; 猜测二"}]
    })
    assert out.findings[0].status == "候选"
    assert out.findings[0].assumptions == ["猜测一", "猜测二"]


def case_reviewer_loose() -> None:
    out = ReviewerOutput.model_validate({
        "decision": "Approve — 看起来没问题", "checks": [], "blockers": "", "reason": "ok"})
    assert out.decision == "approve" and out.blockers == []


def case_reviewer_chinese() -> None:
    out = ReviewerOutput.model_validate({
        "decision": "否决", "checks": [], "blockers": "覆盖不全; 引用不支持结论", "reason": "x"})
    assert out.decision == "veto"
    assert out.blockers == ["覆盖不全", "引用不支持结论"]


# ---------------------------------------------------------------- 资料与引用

def case_split_material() -> None:
    text = "第一段内容够长了。\n\n第二段内容也够长了。\n\n\n第三段。"
    parts = split_material(text)
    assert len(parts) == 3, parts


def case_build_chunks() -> None:
    chunks = build_chunks(["第一段甲甲甲甲甲甲。\n\n第一段乙乙乙乙乙乙。", "第二份甲甲甲甲甲甲。"])
    ids = [c["chunk_id"] for c in chunks]
    assert ids == ["D1-1", "D1-2", "D2-1"], ids


def case_quote_verbatim() -> None:
    content = "生活方式干预：限盐（每日 <5g）、规律运动（每周 ≥150 分钟中等强度）。"
    assert quote_matches("限盐（每日 <5g）", content)
    assert quote_matches("限盐（每日\n<5g）", content), "换行差异应当容忍"
    assert not quote_matches("限盐(每日<5g)", content), "全角半角不同必须判否"
    assert not quote_matches("每日食盐不超过 3 克", content), "编造内容必须判否"
    assert not quote_matches("的", content), "过短摘录不算有效引用"


def case_locate() -> None:
    content = "甲乙丙丁戊己庚辛"
    pos = locate("丙丁", content)
    assert pos == (2, 4), pos
    assert locate("不存在", content) is None


def case_citations_as_objects() -> None:
    """离线 mock 会直接传 Citation 对象，适配层不能把它们丢掉。"""
    from app.state import Citation

    out = ResearcherOutput.model_validate({
        "findings": [{"sub_task_id": "st-1", "claim": "c",
                      "citations": [Citation(source_id="D1-1", quote="原文摘录内容")]}]
    })
    assert len(out.findings[0].citations) == 1
    assert out.findings[0].citations[0].source_id == "D1-1"
    assert out.findings[0].citations[0].quote == "原文摘录内容"


def case_citation_unknown_source() -> None:
    out = ExecutorOutput.model_validate({
        "answer": {"answer_id": "a", "summary": "s",
                   "points": [{"sub_task_id": "st-1", "statement": "x",
                               "citations": [{"source_id": "D9-9", "quote": "随便写"}]}],
                   "acceptance": [], "caveats": []}
    })
    # 契约层允许通过（结构合法），由校验器 V2 判定「资料不存在」
    assert out.answer.points[0].citations[0].source_id == "D9-9"


def main() -> int:
    print("契约适配 + 引用校验 自检：")
    for name, fn in [
        ("Executor: answer 是 JSON 字符串", case_answer_as_json_string),
        ("Executor: citations 是 “D1-2: 原文” 文本", case_citations_as_text),
        ("Executor: acceptance 是单个对象", case_acceptance_as_single_object),
        ("Planner : sub_tasks 是 JSON 字符串", case_planner_subtasks_as_string),
        ("Researcher: citations/assumptions 归一化", case_researcher_citations),
        ("Reviewer: decision 带解释文字", case_reviewer_loose),
        ("Reviewer: 中文 decision + 分号 blockers", case_reviewer_chinese),
        ("资料：按空行切段", case_split_material),
        ("资料：多份资料编号 D1-1/D2-1", case_build_chunks),
        ("引用：逐字命中（宽容空白、拒绝改写）", case_quote_verbatim),
        ("引用：定位原文位置", case_locate),
        ("引用：直接传 Citation 对象不被丢", case_citations_as_objects),
        ("引用：未知来源由校验器判否", case_citation_unknown_source),
    ]:
        check(name, fn)
    print(f"\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项。")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
