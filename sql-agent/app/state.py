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
    # 中文别名（「校验」和「验证」都认 —— 术语统一成「校验」了，
    # 但模型偶尔还会吐「验证」，这里不能因为它换了词就认不出来）
    alias = {"规划": "planner", "生成": "generator",
             "校验": "validator", "验证": "validator",
             "执行": "executor", "修正": "fixer", "审查": "reviewer", "汇总": "reviewer"}
    for key, name in alias.items():
        if key in low:
            return name
    return None


def coerce_mode(value: Any) -> Any:
    """把模型给的模式归一化到 chat / execute 两种。

    模型经常把 `chat` 写成「闲聊」「打招呼」，直接进 `Literal` 会校验失败、白触发一次重试。
    """
    if not isinstance(value, str):
        return value
    low = value.strip().lower()
    if any(k in low for k in ("chat", "闲聊", "聊天", "打招呼", "问候", "寒暄")):
        return "chat"
    return "execute"


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


# 用户这一句是「正常聊天」还是「要动数据」—— 规划器第一步要判对的东西。
#
# 刻意只有两种：**SQL 只能由生成器根据任务生成**，
# 所以「用户贴了一条 SQL」不构成一种模式 —— 那只是问题里的一段文字。
PlanMode = Literal[
    "chat",          # 打招呼 / 闲聊 / 与数据库无关 → 直接回复，不碰数据库
    "execute",       # 要动数据：拆任务 → 生成 SQL → 校验 → 执行 → 回答
]


# ---------------------------------------------------------------- 规划器

class QueryIntent(BaseModel):
    """规划器的产物：**只有任务，不涉及表**。

    表是生成器的事 —— 规划器负责"要做什么"，生成器负责"用哪张表、怎么写"。
    """

    sub_task_id: str = Field(description="稳定唯一的子任务 ID，如 st-1")
    intent: str = Field(description="这个子任务要查/改什么，一句话")
    kind: Literal["查询", "修改", "删除"] = Field(description="操作类型")
    depends_on: list[str] = Field(
        default_factory=list,
        description="这个子任务**依赖哪些子任务的结果**（写 sub_task_id）。"
                    "例如「把上一步查出的商品补货」就依赖那一步。"
                    "有依赖时，系统会把被依赖子任务的**实际执行结果**交给生成器。",
    )
    acceptance: str = Field(
        default="",
        description="怎样算完成 —— 这个子任务交付什么东西才算做完（一句话）。",
    )

    _v = field_validator("depends_on", mode="before")(coerce_str_list)

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
    mode: PlanMode = Field(
        default="execute",
        description="chat=打招呼/闲聊，不需要动数据库（把要说的话写进 chat_reply）；"
                    "execute=要动数据，按下面的 intents 拆任务。",
    )
    plan_action: Literal["initial", "continue", "revise", "finish"] = Field(
        default="initial",
        description="**首次拆解**填 initial。**复查**（拿到前面子任务的实际结果之后）填："
                    "continue=剩余计划不变，继续做；"
                    "revise=后续任务需要调整（把调整后的完整 intents 一并给出）；"
                    "finish=已经够了，可以收尾。",
    )
    review_note: str = Field(
        default="",
        description="复查结论：为什么继续 / 为什么调整 / 为什么可以收尾。首次拆解留空。",
    )
    safety: str = Field(
        default="",
        description="对这个问题**危险性与越界性**的评估：是否试图做 DDL / 越权访问 / "
                    "绕过授权 / 与数据库无关却要求改数据。安全就写「安全」。",
    )
    plan_reason: str = Field(description="为什么这样拆")
    intents: list[QueryIntent] = Field(default_factory=list, description="拆出的子任务，1-4 个")
    chat_reply: str | None = Field(
        default=None,
        description="mode=chat 时给用户的直接回复（打招呼就正常回一句，别硬扯数据库）。",
    )
    refusal: str | None = Field(
        default=None,
        description="如果用户的要求超出允许范围（例如要删表、改建表结构、DROP/TRUNCATE 等），"
                    "把 intents 留空并在这里写清拒绝原因与建议的替代做法。",
    )

    _v = field_validator("intents", mode="before")(coerce_model_list)

    @field_validator("mode", mode="before")
    @classmethod
    def _fix_mode(cls, v: Any) -> Any:
        return coerce_mode(v)


# ---------------------------------------------------------------- 生成 / 修正

class TableFilterOutput(BaseModel):
    """生成器的**第一层**：只拿「表简述 + 表关系」粗筛出这次要碰哪几张表。"""

    tables: list[str] = Field(default_factory=list, description="这次需要用到哪些表")
    reason: str = Field(default="", description="为什么选这几张表")

    _v = field_validator("tables", mode="before")(coerce_str_list)


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
    give_up: str | None = Field(
        default=None,
        description="如果判断这条 SQL 根本无法修复（例如需求本身超出允许范围、或缺少必要条件），"
                    "把原因写在这里并停止尝试。**绝对不要重复生成和上一版一样的 SQL。**",
    )

    _v = field_validator("draft", mode="before")(coerce_model)


# ---------------------------------------------------------------- 校验器

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
    passed: bool = Field(description="这条 SQL 是否通过校验")
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
    conversation_id: str    # 多轮对话：同一个会话里的多轮共享它
    turn: int               # 第几轮
    prior_turns: list[dict]  # 前几轮的摘要（问题 + 做了什么 + 结果），给规划器当上文
    mode: str               # chat / execute（规划器判定）
    safety: str             # 规划器对危险性与越界性的评估
    chat_reply: str         # mode=chat 时规划器给的直接回复
    status: str
    version: int
    round: int              # 修正轮次
    intents: list[dict]     # 规划器拆出的子任务（只有任务，没有表；含 depends_on / acceptance）
    memory: list[dict]      # **共享记忆**：已验证的「带来源的事实」，供后续子任务引用
    replanned_cursor: int   # 已经复查到第几个子任务（防止同一个位置反复复查）
    plan_note: str          # 规划器复查的结论（为什么继续 / 改了什么 / 为什么收尾）
    refusal: str            # 规划器判定「这个请求超出允许范围」时的说明
    understanding: str      # 规划器对问题的理解
    cursor: int             # 当前处理到第几个子任务
    draft: dict             # 当前 SQL 草稿（生成/修正的产物，含生成器筛出的 tables）
    verdict: dict           # sql_guard 静态判定
    explain: dict           # EXPLAIN 结果 —— **由执行器产生**，校验器不碰数据库
    checks: dict            # 校验结论（静态防线 + 模型语义判断）
    result: dict            # 执行结果
    confirmed: bool
    history: list[dict]     # 每条 SQL 的全程留痕（给审查器汇总）
    attempts: list[dict]    # 每次校验的 {sql_hash, error_sig, ok} —— 用来检测「修正没有进展」
    last_agent: str         # 上一个说话的是谁
    next_agent: str         # 模型建议的下一步（只是建议）
    route: str              # 护栏实际决定去哪（条件边读它）
    error: str
    give_up: str            # 修正智能体判断「无法修复」时的原因


# ---------------------------------------------------------------- 工具

def now_hash(sql: str) -> str:
    import hashlib
    return hashlib.sha256(sql.strip().encode("utf-8")).hexdigest()[:16]


_WS = re.compile(r"\s+")


def one_line(text: str) -> str:
    return _WS.sub(" ", (text or "").strip())
