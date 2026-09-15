"""状态契约：四个智能体的结构化输出 + LangGraph 共享状态。

通用版（无快照）：
  · 没有「哪一版代码」这种东西 —— 「同一份事实」由 **引用摘录** 保证；
  · 每个角色产出的都是一份**契约**，不是自然语言；
  · 每条结论 / 答案都必须带 `citations`（哪份资料 + 原文摘录），才可被运行时校验。

关于 `mode="before"` 校验器（接入层适配器）
------------------------------------------------
实测大模型走 function calling 时会把嵌套对象序列化成 JSON 字符串、
把 list[str] 写成换行/逗号拼接的长文本。所以进入契约之前统一归一化一次。
契约本身不变，变的只是接入层。
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal, TypedDict

from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------- 枚举

ResultStatus = Literal["候选", "已验证", "冲突", "已否决"]
RunStatus = Literal[
    "created", "planning", "researching", "answering",
    "reviewing", "done", "failed",
]
TaskStatus = Literal["pending", "running", "completed", "failed", "unknown"]
RoleName = Literal["planner", "researcher", "executor", "reviewer"]


# ---------------------------------------------------------------- 接入层适配

def coerce_json(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if text[:1] in "{[":
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return value
    return value


def coerce_model(value: Any) -> Any:
    value = coerce_json(value)
    if isinstance(value, list) and value:
        return coerce_json(value[0])
    return value


def coerce_model_list(value: Any) -> Any:
    value = coerce_json(value)
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [coerce_json(v) for v in value if v is not None]
    return value


def coerce_str_list(value: Any) -> Any:
    value = coerce_json(value)
    if value is None:
        return []
    if isinstance(value, str):
        raw = value.replace("，", ",").replace("；", ";").replace("、", ",")
        chunks: list[str] = []
        for line in raw.replace(";", "\n").splitlines():
            chunks.extend(line.split(","))
        items = [c.strip().lstrip("-•·*").strip() for c in chunks]
        items = [i for i in items if i]
        if items:
            return items
        stripped = value.strip()
        return [stripped] if stripped else []
    if isinstance(value, (list, tuple, set)):
        return [v if isinstance(v, str) else json.dumps(v, ensure_ascii=False) for v in value]
    return value


_CITE_RE = re.compile(r"^\s*\[?([A-Za-z]+\d+(?:-\d+)?)\]?\s*[:：|\-—]?\s*(.+)$", re.S)


def coerce_citations(value: Any) -> Any:
    """引用兼容：dict / JSON 字符串 / "D1-2: 原文…" 这种纯文本。"""
    value = coerce_json(value)
    if value is None:
        return []
    if isinstance(value, (str, dict)):
        value = [value]
    if not isinstance(value, list):
        return value
    out: list[Any] = []
    for v in value:
        v = coerce_json(v)
        if isinstance(v, dict):
            out.append(v)
        elif isinstance(v, str):
            m = _CITE_RE.match(v)
            if m:
                out.append({"source_id": m.group(1), "quote": m.group(2).strip()})
            else:
                out.append({"source_id": "", "quote": v.strip()})
        elif v is not None:
            # 已经是 Citation 之类的对象（离线 mock 会直接传对象），原样保留
            out.append(v)
    return out


# ---------------------------------------------------------------- 引用

class Citation(BaseModel):
    """一条依据：哪份资料的哪句原文。`quote` 必须能在资料里逐字找到。"""

    source_id: str = Field(description="资料片段 ID，例如 D1-2（含义：第 1 份资料的第 2 段）")
    quote: str = Field(description="原文摘录，必须与资料逐字一致，直接复制，不要改写或加省略号")

    _v = field_validator("source_id", "quote", mode="before")(lambda v: v if isinstance(v, str) else ("" if v is None else str(v)))


# ---------------------------------------------------------------- Planner

class SubTask(BaseModel):
    sub_task_id: str = Field(description="稳定唯一的子问题 ID，如 st-1")
    question: str = Field(description="把用户问题拆出的这个子问题，一句话")
    why: str = Field(description="为什么要拆出它：它需要哪部分资料才能回答")
    depends_on: list[str] = Field(default_factory=list, description="依赖的子问题 ID，无依赖留空")

    _v_dep = field_validator("depends_on", mode="before")(coerce_str_list)


class PlannerOutput(BaseModel):
    plan_reason: str = Field(description="怎么拆的：说明拆解依据与各子问题之间的关系")
    sub_tasks: list[SubTask] = Field(description="拆出的子问题，2-5 个")

    _v = field_validator("sub_tasks", mode="before")(coerce_model_list)


# ---------------------------------------------------------------- Researcher

class Finding(BaseModel):
    sub_task_id: str = Field(description="这条结论回答了哪个子问题（必须是规划器给出的 ID）")
    claim: str = Field(description="一句可核对的主张")
    status: ResultStatus = Field(default="候选", description="候选 / 已验证 / 冲突 / 已否决")
    citations: list[Citation] = Field(description="支撑这条结论的原文摘录，至少一条")
    insufficient: bool = Field(
        default=False,
        description="资料里确实没有与这个子问题相关的内容时置为 true；此时 citations 可以为空，"
                    "但 claim 必须明确说明资料不足，不许编造",
    )
    assumptions: list[str] = Field(default_factory=list, description="资料没写、只能推测的部分")

    _v = field_validator("citations", mode="before")(coerce_citations)
    _va = field_validator("assumptions", mode="before")(coerce_str_list)


class ResearcherOutput(BaseModel):
    findings: list[Finding] = Field(description="得到的结论，每个子问题至少一条")

    _v = field_validator("findings", mode="before")(coerce_model_list)


# ---------------------------------------------------------------- Executor

class AnswerPoint(BaseModel):
    sub_task_id: str = Field(description="对应规划器拆出的哪个子问题")
    statement: str = Field(description="这个子问题的结论，直接可读")
    citations: list[Citation] = Field(description="支撑它的原文摘录")
    insufficient: bool = Field(
        default=False,
        description="资料里没有相关内容时置为 true；此时 citations 可以为空，"
                    "但 statement 必须明确说明资料不足，不许编造",
    )

    _v = field_validator("citations", mode="before")(coerce_citations)


class AcceptanceItem(BaseModel):
    item_id: str = Field(description="验收项 ID，如 ac-1")
    statement: str = Field(description="怎样算回答到位，一句可核对的话")
    citations: list[Citation] = Field(description="该验收所依据的原文摘录")
    insufficient: bool = Field(
        default=False,
        description="资料不足、没有可验证的内容时置为 true；此时 citations 可以为空",
    )

    _v = field_validator("citations", mode="before")(coerce_citations)


class Answer(BaseModel):
    answer_id: str = Field(description="答案 ID，如 ans-1")
    summary: str = Field(description="一句话直接回答用户的问题")
    points: list[AnswerPoint] = Field(description="分点论述，必须覆盖每一个子问题")
    recommendations: list[str] = Field(default_factory=list, description="给用户的建议")
    acceptance: list[AcceptanceItem] = Field(description="验收条件，逐条带原文依据")
    caveats: list[str] = Field(default_factory=list, description="资料不足 / 结论有争议的地方，必须诚实列出")

    _v1 = field_validator("points", mode="before")(coerce_model_list)
    _v2 = field_validator("acceptance", mode="before")(coerce_model_list)
    _v3 = field_validator("recommendations", "caveats", mode="before")(coerce_str_list)


class ExecutorOutput(BaseModel):
    answer: Answer

    _v = field_validator("answer", mode="before")(coerce_model)


# ---------------------------------------------------------------- Reviewer

class ReviewCheck(BaseModel):
    check_id: str
    target: str
    result: Literal["pass", "fail"]
    source_id: str | None = None
    note: str = ""

    @field_validator("source_id", mode="before")
    @classmethod
    def _v_src(cls, v: Any) -> Any:
        if isinstance(v, (list, tuple)):
            return v[0] if v else None
        return v

    @field_validator("note", "target", mode="before")
    @classmethod
    def _v_txt(cls, v: Any) -> Any:
        if isinstance(v, (list, tuple)):
            return "；".join(str(x) for x in v)
        return v


class ReviewerOutput(BaseModel):
    decision: Literal["approve", "veto"]
    checks: list[ReviewCheck]
    blockers: list[str] = Field(default_factory=list, description="否决时必须逐条列出阻塞点")
    reason: str = Field(description="给出该结论的理由")
    rework_target: Literal["researcher", "executor"] | None = Field(
        default=None, description="需要返工时退回给谁"
    )

    _v = field_validator("checks", mode="before")(coerce_model_list)
    _vb = field_validator("blockers", mode="before")(coerce_str_list)

    @field_validator("decision", mode="before")
    @classmethod
    def _v_decision(cls, v: Any) -> Any:
        if isinstance(v, str):
            low = v.strip().lower()
            if low.startswith("approve") or low in {"通过", "同意", "pass"}:
                return "approve"
            if low.startswith("veto") or low in {"否决", "驳回", "reject"}:
                return "veto"
        return v


# ---------------------------------------------------------------- 一致性报告

class InvariantResult(BaseModel):
    invariant_id: str
    title: str
    result: Literal["pass", "fail"]
    detail: str = ""
    offenders: list[str] = Field(default_factory=list)


class ConsistencyReport(BaseModel):
    passed: bool
    results: list[InvariantResult]


# ---------------------------------------------------------------- LangGraph 状态

class TaskState(TypedDict, total=False):
    """共享状态：唯一的交接载体。没有快照，只有「问题 + 资料」。"""

    global_task_id: str
    question: str
    status: str
    version: int
    round: int
    sub_tasks: list[dict]
    findings: list[dict]
    answers: list[dict]
    consistency: dict
    review: dict
    error: str
