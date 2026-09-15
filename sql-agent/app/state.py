"""状态契约：智能体的结构化输出 + LangGraph 共享状态。

两个要点：

1. **每个智能体都要说「下一步我想交给谁」**（`handoff_to`）——
   这就是「链路不固定、智能体自己选择协作对象」的落点。
   但模型说的只是**建议**，最终由 runtime_guard 决定「能不能去」。

2. 接入层适配器（`mode="before"`）沿用冻结项目的做法：
   DeepSeek 会把嵌套对象序列化成 JSON 字符串、把 list 写成换行拼接的长文本，
   所以进契约前统一归一化，契约本身不变。
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal, TypedDict

from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------- 枚举

RiskLevel = Literal["只读", "写入", "需确认", "禁止"]
RunStatus = Literal[
    "created", "planning", "generating", "validating",
    "executing", "fixing", "reviewing", "done", "failed",
]
TaskStatus = Literal["pending", "running", "completed", "failed", "unknown"]
RoleName = Literal["planner", "generator", "validator", "executor", "fixer", "reviewer"]
# 智能体能建议的下一步（含结束）
NextAgent = Literal["planner", "generator", "validator", "executor", "fixer", "reviewer", "done"]


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


def coerce_next(value: Any) -> Any:
    """把模型给的下一步归一化到枚举内；认不出来就留空（由护栏兜底）。"""
    if not isinstance(value, str):
        return None
    low = value.strip().lower()
    for name in ("planner", "generator", "validator", "executor", "fixer", "reviewer"):
        if name in low:
            return name
    if low in {"done", "end", "结束", "完成"}:
        return "done"
    # 中文别名
    alias = {"规划": "planner", "生成": "generator", "验证": "validator",
             "执行": "executor", "修正": "fixer", "审查": "reviewer", "汇总": "reviewer"}
    for key, name in alias.items():
        if key in low:
            return name
    return None


class _NextMixin(BaseModel):
    handoff_to: NextAgent | None = Field(
        default=None,
        description="你建议下一步交给谁：planner/generator/validator/executor/fixer/reviewer/done。"
                    "这只是建议，运行时护栏会校验是否允许。",
    )

    @field_validator("handoff_to", mode="before")
    @classmethod
    def _v_next(cls, v: Any) -> Any:
        return coerce_next(v)


# ---------------------------------------------------------------- 规划器

class QueryIntent(BaseModel):
    sub_task_id: str = Field(description="稳定唯一的子任务 ID，如 st-1")
    intent: str = Field(description="这个子任务要查/改什么，一句话")
    kind: Literal["查询", "修改", "删除"] = Field(description="操作类型")
    tables: list[str] = Field(default_factory=list, description="预计涉及的表")
    depends_on: list[str] = Field(default_factory=list, description="依赖的子任务 ID")

    _v = field_validator("tables", "depends_on", mode="before")(coerce_str_list)

    @field_validator("kind", mode="before")
    @classmethod
    def _fix_kind(cls, v: Any) -> Any:
        if isinstance(v, str):
            if "删" in v or "delete" in v.lower():
                return "删除"
            if "改" in v or "更新" in v or "update" in v.lower() or "insert" in v.lower():
                return "修改"
            return "查询"
        return v


class PlannerOutput(_NextMixin):
    understanding: str = Field(description="你对用户问题的理解，一句话")
    plan_reason: str = Field(description="为什么这样拆")
    intents: list[QueryIntent] = Field(default_factory=list, description="拆出的子任务，1-4 个")
    refusal: str | None = Field(
        default=None,
        description="如果用户的要求超出允许范围（例如要删表、改建表结构、DROP/TRUNCATE 等），"
                    "把 intents 留空并在这里写清拒绝原因与建议的替代做法。",
    )

    _v = field_validator("intents", mode="before")(coerce_model_list)


# ---------------------------------------------------------------- 生成 / 修正

class SqlDraft(BaseModel):
    sub_task_id: str = Field(description="对应哪个子任务")
    intent: str = Field(description="这条 SQL 想干什么")
    sql: str = Field(description="**一条**完整 SQL，不要分号结尾，不要多条语句")
    note: str = Field(default="", description="为什么这么写（JOIN/聚合/条件的依据）")


class GeneratorOutput(_NextMixin):
    draft: SqlDraft

    _v = field_validator("draft", mode="before")(coerce_model)


class FixerOutput(_NextMixin):
    draft: SqlDraft
    what_changed: str = Field(description="这次改了什么、为什么")

    _v = field_validator("draft", mode="before")(coerce_model)


# ---------------------------------------------------------------- 验证器

class SqlCheck(BaseModel):
    check_id: str = Field(description="检查项 ID，如 ck-1")
    item: str = Field(description="检查的是什么")
    result: Literal["pass", "fail"]
    detail: str = ""

    @field_validator("detail", "item", mode="before")
    @classmethod
    def _v_txt(cls, v: Any) -> Any:
        if isinstance(v, (list, tuple)):
            return "；".join(str(x) for x in v)
        return v

    @field_validator("result", mode="before")
    @classmethod
    def _v_res(cls, v: Any) -> Any:
        if isinstance(v, str):
            low = v.strip().lower()
            if low.startswith(("pass", "ok", "通过", "✓")) or low in {"true", "yes"}:
                return "pass"
            if low.startswith(("fail", "不通过", "✕")) or low in {"false", "no"}:
                return "fail"
        if isinstance(v, bool):
            return "pass" if v else "fail"
        return v


class ValidatorOutput(_NextMixin):
    passed: bool = Field(description="这条 SQL 是否通过验证")
    checks: list[SqlCheck] = Field(default_factory=list)
    reason: str = Field(default="", description="结论理由")

    _v = field_validator("checks", mode="before")(coerce_model_list)

    @field_validator("passed", mode="before")
    @classmethod
    def _v_passed(cls, v: Any) -> Any:
        if isinstance(v, str):
            low = v.strip().lower()
            if low.startswith(("pass", "true", "通过", "yes")) or low in {"1", "是"}:
                return True
            if low.startswith(("fail", "false", "不通过", "no")):
                return False
        return v


# ---------------------------------------------------------------- 执行器

class ExecutorOutput(_NextMixin):
    ok: bool = Field(description="执行是否成功")
    summary: str = Field(default="", description="一句话说明执行结果")
    error_hint: str = Field(default="", description="失败时，你判断的失败原因（给修正智能体看）")

    @field_validator("ok", mode="before")
    @classmethod
    def _v_ok(cls, v: Any) -> Any:
        if isinstance(v, str):
            low = v.strip().lower()
            if low.startswith(("true", "ok", "成功", "yes")):
                return True
            if low.startswith(("false", "fail", "失败", "no")):
                return False
        return v


# ---------------------------------------------------------------- 审查器（汇总）

class ReviewerOutput(_NextMixin):
    decision: Literal["approve", "veto"]
    final_answer: str = Field(default="", description="给用户的最终答复，直接可读")
    checks: list[SqlCheck] = Field(default_factory=list)
    reason: str = Field(default="", description="给出该结论的理由")

    _v = field_validator("checks", mode="before")(coerce_model_list)

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

class GuardResult(BaseModel):
    guard_id: str
    title: str
    result: Literal["pass", "fail"]
    detail: str = ""
    offenders: list[str] = Field(default_factory=list)


class ConsistencyReport(BaseModel):
    passed: bool
    results: list[GuardResult]


# ---------------------------------------------------------------- LangGraph 状态

class TaskState(TypedDict, total=False):
    """共享状态：唯一的交接载体。"""

    global_task_id: str
    question: str
    schema_text: str
    status: str
    version: int
    round: int              # 修正轮次
    intents: list[dict]     # 规划器拆出的子任务
    refusal: str            # 规划器判定「这个请求超出允许范围」时的说明
    understanding: str      # 规划器对问题的理解
    cursor: int             # 当前处理到第几个子任务
    draft: dict             # 当前 SQL 草稿（生成/修正的产物）
    verdict: dict           # sql_guard 静态判定
    explain: dict           # EXPLAIN 校验结果
    checks: dict            # 验证结论
    result: dict            # 执行结果
    confirm: dict           # 人工确认请求
    confirmed: bool
    history: list[dict]     # 每条 SQL 的全程留痕（给审查器汇总）
    last_agent: str         # 上一个说话的是谁
    next_agent: str         # 模型建议的下一步（只是建议）
    route: str              # 护栏实际决定去哪
    route_reason: str       # 护栏为什么这么定
    final_answer: str
    error: str


# ---------------------------------------------------------------- 工具

def now_hash(sql: str) -> str:
    import hashlib
    return hashlib.sha256(sql.strip().encode("utf-8")).hexdigest()[:16]


_WS = re.compile(r"\s+")


def one_line(text: str) -> str:
    return _WS.sub(" ", (text or "").strip())
