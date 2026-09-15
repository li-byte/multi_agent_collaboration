"""SQL 静态防线：风险分级 + 危险语句拦截。

这是「不能删库」的**第一道**防线。第二道是数据库权限 ——
执行用户 SQL 用的是受限角色，它连 DDL 权限都没有。
两道都要有：应用层可以被绕过，权限绕不过去。

设计原则：
  · 先看**顶层 DML 类型**（sqlparse 的 token 层级，不是文本匹配）——
    这样 `WITH x AS (SELECT …) DELETE FROM t` 会被正确识别成 DELETE；
  · 扫描前先把**字符串字面量和注释**抹掉 —— 否则 `WHERE name='DROP TABLE'`
    会误报，而 `DROP/**/TABLE` 会漏报；
  · 判定「需确认」而不是「禁止」的，交给人工确认通道，不要一刀切。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

import sqlparse
from sqlparse import tokens as T

RiskLevel = Literal["只读", "写入", "需确认", "禁止"]

# 顶层只允许这四种 DML
ALLOWED_DML = {"SELECT", "INSERT", "UPDATE", "DELETE"}

# 一律禁止（在「已抹掉字符串与注释」的文本上扫描）
FORBIDDEN_PATTERNS: list[tuple[str, str]] = [
    (r"\bDROP\b", "DROP"),
    (r"\bTRUNCATE\b", "TRUNCATE"),
    (r"\bALTER\b", "ALTER"),
    (r"\bCREATE\b", "CREATE"),
    (r"\bRENAME\b", "RENAME"),
    (r"\bGRANT\b", "GRANT"),
    (r"\bREVOKE\b", "REVOKE"),
    (r"\bCOMMENT\s+ON\b", "COMMENT ON"),
    (r"\bREINDEX\b", "REINDEX"),
    (r"\bVACUUM\b", "VACUUM"),
    (r"\bCLUSTER\b", "CLUSTER"),
    (r"\bCOPY\b", "COPY"),
    (r"\bDO\s+\$", "DO 代码块"),          # 注意：不能匹配 ON CONFLICT DO UPDATE
    (r"\bCALL\b", "CALL"),
    (r"\bEXECUTE\b", "EXECUTE"),
    (r"\bPREPARE\b", "PREPARE"),
    (r"\bDEALLOCATE\b", "DEALLOCATE"),
    (r"\bBEGIN\b", "BEGIN"),
    (r"\bCOMMIT\b", "COMMIT"),
    (r"\bROLLBACK\b", "ROLLBACK"),
    (r"\bSAVEPOINT\b", "SAVEPOINT"),
    (r"\bDATABASE\b", "DATABASE"),
    (r"\bTABLESPACE\b", "TABLESPACE"),
    (r"\bPUBLICATION\b", "PUBLICATION"),
    (r"\bSUBSCRIPTION\b", "SUBSCRIPTION"),
    (r"\bFUNCTION\b", "FUNCTION"),
    (r"\bPROCEDURE\b", "PROCEDURE"),
    (r"\bTRIGGER\b", "TRIGGER"),
    (r"\bPOLICY\b", "POLICY"),
    (r"\bpg_read_file\b", "读取服务器文件"),
    (r"\bpg_read_binary_file\b", "读取服务器文件"),
    (r"\bpg_ls_dir\b", "遍历服务器目录"),
    (r"\blo_import\b", "大对象导入"),
    (r"\blo_export\b", "大对象导出"),
    (r"\bdblink\b", "外部库连接"),
    (r"\bpg_terminate_backend\b", "终止其他会话"),
    (r"\bpg_cancel_backend\b", "取消其他会话"),
    (r"\bpg_sleep\b", "阻塞等待"),
]

_TABLE_RE = re.compile(r"\b(?:from|join|update|into)\s+(?:only\s+)?([a-zA-Z_]\w*)", re.I)
_CTE_RE = re.compile(r"\b([a-zA-Z_]\w*)\s+as\s*\(", re.I)
_SELECT_INTO_RE = re.compile(r"\bselect\b[\s\S]*?\binto\b", re.I)
_WHERE_RE = re.compile(r"\bwhere\b", re.I)
_DML_RE = re.compile(r"^\s*(select|insert|update|delete)\b", re.I)


@dataclass
class SqlVerdict:
    """静态扫描结论。"""

    level: RiskLevel
    action: str = "?"
    tables: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)   # 拒绝原因
    notes: list[str] = field(default_factory=list)     # 提示（不拦）
    statements: int = 1

    @property
    def ok(self) -> bool:
        return self.level != "禁止"

    @property
    def needs_confirm(self) -> bool:
        return self.level == "需确认"

    @property
    def is_read(self) -> bool:
        return self.level == "只读"

    def summary(self) -> str:
        bits = [f"类型 {self.action}", f"风险 {self.level}"]
        if self.tables:
            bits.append("涉及表 " + "、".join(self.tables))
        return " · ".join(bits)


# ---------------------------------------------------------------- 文本预处理

def strip_literals(sql: str) -> str:
    """把字符串字面量与注释抹掉，避免误报/漏报。

    · 字符串抹成 `''`  —— 防止 `WHERE name='DROP TABLE'` 误报
    · 注释抹成一个空格 —— 让 `DROP/**/TABLE` 还原成 `DROP TABLE` 被抓到
    """
    try:
        parsed = sqlparse.parse(sql)
    except Exception:  # noqa: BLE001
        return sql
    if not parsed:
        return sql

    out: list[str] = []
    for tok in parsed[0].flatten():
        tt = tok.ttype
        if tt is None:
            out.append(tok.value)
        elif tt in T.Comment:
            out.append(" ")
        elif tt in T.String or tt in T.Literal.String:
            out.append("''")
        else:
            out.append(tok.value)
    return "".join(out)


def top_level_dml(sql: str) -> str | None:
    """取**顶层**的 DML 关键字。

    用 token 层级而不是文本匹配 —— 这样 `WITH x AS (SELECT …) DELETE FROM t`
    返回 DELETE，而不是被 CTE 里的 SELECT 骗过去。
    """
    try:
        parsed = sqlparse.parse(sql)
    except Exception:  # noqa: BLE001
        return None
    if not parsed:
        return None
    for tok in parsed[0].tokens:
        if tok.ttype is T.Keyword.DML:
            return tok.value.upper()
    # 有些写法 sqlparse 不标成 DML（例如以 WITH 开头），退一步看开头
    m = _DML_RE.match(strip_literals(sql))
    return m.group(1).upper() if m else None


def _drop_parens(text: str) -> str:
    """删掉所有括号内容（含嵌套），只留最外层。"""
    out: list[str] = []
    depth = 0
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return "".join(out)


def has_top_level_where(sql: str) -> bool:
    """是否存在**顶层** WHERE —— 子查询里的 WHERE 不算。

    为什么不能只找 `\\bwhere\\b`：`UPDATE t SET x = (SELECT … WHERE …)`
    没有顶层 WHERE，是全表更新，但文本里确实有 WHERE，会被漏判成安全。
    所以先剥掉字符串/注释，再删掉括号内容，最后才匹配。
    """
    flat = _drop_parens(strip_literals(sql))
    return bool(_WHERE_RE.search(flat))


def extract_tables(sql: str) -> list[str]:
    """粗提涉及的表名（排除 CTE 名）。用于白名单校验。"""
    flat = strip_literals(sql)
    ctes = {m.group(1).lower() for m in _CTE_RE.finditer(flat)}
    found: list[str] = []
    for m in _TABLE_RE.finditer(flat):
        name = m.group(1)
        low = name.lower()
        if low in ctes or low in {"select", "values", "set"}:
            continue
        if name not in found:
            found.append(name)
    return found


# ---------------------------------------------------------------- 主入口

def analyze(sql: str, allowed_tables: set[str] | None = None) -> SqlVerdict:
    """对一条 SQL 做静态风险判定。"""
    raw = (sql or "").strip()
    if not raw:
        return SqlVerdict(level="禁止", reasons=["SQL 为空"])

    # ① 必须恰好一条语句
    try:
        parts = [s for s in sqlparse.split(raw) if s.strip()]
    except Exception:  # noqa: BLE001
        return SqlVerdict(level="禁止", reasons=["SQL 无法解析"])
    if not parts:
        return SqlVerdict(level="禁止", reasons=["SQL 为空"])
    if len(parts) > 1:
        return SqlVerdict(
            level="禁止", statements=len(parts),
            reasons=[f"一次只允许执行一条语句（检测到 {len(parts)} 条）"],
        )
    single = parts[0].strip()

    # ② 顶层必须是允许的 DML
    action = top_level_dml(single)
    if action is None or action not in ALLOWED_DML:
        return SqlVerdict(
            level="禁止", action=action or "?",
            reasons=[f"不允许的语句类型：{(action or '未知')}。只允许 SELECT / INSERT / UPDATE / DELETE"],
        )

    flat = strip_literals(single)

    # ③ 危险关键字扫描
    hits: list[str] = []
    for pattern, label in FORBIDDEN_PATTERNS:
        if re.search(pattern, flat, re.I):
            hits.append(label)
    if hits:
        return SqlVerdict(
            level="禁止", action=action,
            reasons=[f"检测到危险操作：{'、'.join(sorted(set(hits)))}"],
        )

    # ④ SELECT ... INTO 会建表
    if action == "SELECT" and _SELECT_INTO_RE.search(flat):
        return SqlVerdict(level="禁止", action=action,
                          reasons=["SELECT ... INTO 会创建新表，已拒绝"])

    # ⑤ 表名白名单
    tables = extract_tables(single)
    notes: list[str] = []
    if allowed_tables:
        unknown = [t for t in tables if t.lower() not in {x.lower() for x in allowed_tables}]
        if unknown:
            return SqlVerdict(
                level="禁止", action=action, tables=tables,
                reasons=[f"访问了未授权的表：{'、'.join(unknown)}。只允许 {'、'.join(sorted(allowed_tables))}"],
            )
    if not tables:
        notes.append("没能识别出涉及的表，执行前请人工确认")

    # ⑥ 风险分级
    if action == "DELETE":
        level: RiskLevel = "需确认"
        if not has_top_level_where(single):
            notes.append("没有 WHERE，将删除全表数据")
    elif action == "UPDATE" and not has_top_level_where(single):
        level = "需确认"
        notes.append("UPDATE 没有 WHERE，将更新全表数据")
    elif action == "SELECT":
        level = "只读"
    else:
        level = "写入"

    if action == "SELECT" and not re.search(r"\blimit\b", flat, re.I):
        notes.append("没有 LIMIT，结果会被系统截断到上限行数")

    return SqlVerdict(level=level, action=action, tables=tables, notes=notes, statements=1)
