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

# 允许显式写的 schema（不写就走 search_path）
ALLOWED_SCHEMAS = {"public"}

# 抓 from/join/update/into 后面的表名，**同时抓可选的 schema 前缀**
_TABLE_RE = re.compile(
    r"\b(?:from|join|update|into)\s+(?:only\s+)?"
    r"(?:(?P<schema>[a-zA-Z_]\w*)\s*\.\s*)?(?P<table>[a-zA-Z_]\w*)",
    re.I,
)
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
    normalized_sql: str = ""

    @property
    def ok(self) -> bool:
        return self.level != "禁止"

    @property
    def needs_confirm(self) -> bool:
        return self.level == "需确认"


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


def extract_refs(sql: str) -> tuple[list[str], list[str]]:
    """返回 (表名列表, 非法的 schema 前缀列表)。

    `FROM public.products` 必须提取出表名 `products` 而不是 `public` ——
    早期版本没处理 schema 前缀，把 `public` 当成了表名，于是
    「修正智能体给表名加上 public. 前缀」这种**本来合理的修正**被误判成越权，
    导致它在同一个地方反复打转、最后轮次耗尽。
    """
    flat = strip_literals(sql)
    ctes = {m.group(1).lower() for m in _CTE_RE.finditer(flat)}
    tables: list[str] = []
    bad_schemas: list[str] = []
    for m in _TABLE_RE.finditer(flat):
        schema = (m.group("schema") or "").strip()
        table = m.group("table")
        if schema and schema.lower() not in ALLOWED_SCHEMAS:
            if schema.lower() not in bad_schemas:
                bad_schemas.append(schema.lower())
            continue
        low = table.lower()
        if low in ctes or low in {"select", "values", "set"}:
            continue
        if table not in tables:
            tables.append(table)
    return tables, bad_schemas


# ---------------------------------------------------------------- 用户输入体检

# 用户**只能通过提问**引导智能体；输入里夹带 SQL 一律按危险操作处理。
# 这条是硬规则，所以放在运行时（确定性），不指望模型自觉。
_SQL_HEAD = re.compile(
    r"\b(select|insert|update|delete|drop|truncate|alter|create|grant|revoke)\b", re.I)
_SQL_STRUCT = re.compile(
    r"\b(from|into|set|table|database|index|view|where|values|schema|join)\b", re.I)


def find_user_sql(text: str) -> str:
    """用户消息里是否夹带了 SQL；夹带了就返回那段原文（截断），否则返回空串。

    判定用「动词 + 结构词」两条同时命中，避免把英文散文里的 select / update 误判。
    正常的中文提问（「查一下北京客户的订单总额」）不会命中。
    """
    t = (text or "").strip()
    if not t:
        return ""
    head = _SQL_HEAD.search(t)
    if not head:
        return ""
    if _SQL_STRUCT.search(t) or ";" in t:
        return t[:160]
    return ""


# ---------------------------------------------------------------- 主入口

def check_intent_tables(sql: str, intent_tables: list[str] | None) -> tuple[bool, str]:
    """SQL 涉及的表必须落在**生成器选定的表**里。

    （这份清单由生成器的第一层「选表」步骤产出，修正智能体只能继承、不能扩大。）

    为什么必须拦这一条：
      模型发现 `products` 不存在、旁边恰好有个同构的 `products1`，
      就会「顺手」把表名换掉 —— 看起来查询成功了，
      **实际回答的是另一个问题**。这比查不出来危险得多：
      用户拿到的是 products1 的数据，却以为是 products 的。

    确实需要别的表？走「重新选表 / 重新拆任务」这条正式路径，
    而不是在执行前偷偷换掉。

    另外，系统内置目录（`app/catalog.py`）已经是一道更硬的墙：
    目录里没有的表，在 `analyze()` 阶段就会被判「未授权」。
    这里再拦一道，防的是「目录里有、但这次任务不该用」的表。
    """
    want = {t.lower() for t in (intent_tables or []) if t}
    if not want:
        return True, "生成器未选定表，跳过该检查"
    got, _ = extract_refs(sql)
    extra = sorted({t for t in got if t.lower() not in want})
    if not extra:
        return True, "只用了生成器选定的表：" + "、".join(sorted(want))
    return False, (f"生成器只选定了 {'、'.join(sorted(want))}，SQL 还用了 {'、'.join(extra)}；"
                   f"**不允许为了绕开「表不存在」而更换表名** —— 那等于换了问题。"
                   f"确实需要别的表，请退回重新选表")


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

    # PostgreSQL AST 检查嵌套语句；sqlparse 的顶层分类不能发现写 CTE。
    # 首版不支持嵌套写入：直接拒绝，避免外层 SELECT 掩盖副作用。
    try:
        from pglast import ast, parse_sql
        root = parse_sql(single)[0].stmt
        def nodes(value):
            if isinstance(value, ast.Node):
                yield value
                for name in value:
                    yield from nodes(getattr(value, name))
            elif isinstance(value, (tuple, list)):
                for item in value:
                    yield from nodes(item)
        tree = list(nodes(root))
        ast_tables, ast_schemas = set(), set()
        def refs(value, scope=frozenset()):
            if isinstance(value, ast.RangeVar):
                if value.catalogname:
                    ast_schemas.add(value.catalogname)
                if value.schemaname and value.schemaname != "public":
                    ast_schemas.add(value.schemaname)
                if value.schemaname or value.catalogname or value.relname not in scope:
                    ast_tables.add(value.relname)
            elif isinstance(value, ast.Node):
                clause = getattr(value, "withClause", None)
                local = set(scope)
                if clause:
                    # 非递归 CTE 仅可见此前 CTE；递归 CTE 可见同层声明。
                    recursive = {c.ctename for c in clause.ctes} if clause.recursive else set()
                    for cte in clause.ctes:
                        refs(cte.ctequery, frozenset(local | recursive))
                        local.add(cte.ctename)
                for name in value:
                    if name != "withClause":
                        # INSERT/UPDATE/DELETE 的目标始终是真实表，不能被 CTE 名遮蔽。
                        refs(getattr(value, name), frozenset() if name == "relation" else frozenset(local))
            elif isinstance(value, (tuple, list)):
                for item in value:
                    refs(item, scope)
        refs(root)
    except Exception:
        return SqlVerdict(level="禁止", action=action, reasons=["PostgreSQL 语法解析失败，拒绝执行"])
    if any(isinstance(n, (ast.InsertStmt, ast.UpdateStmt, ast.DeleteStmt))
           and n is not root for n in tree):
        return SqlVerdict(level="禁止", action=action, reasons=["不支持包含写入操作的 CTE/嵌套语句，请拆为独立任务"])
    if any(isinstance(n, ast.SelectStmt) and n.intoClause is not None for n in tree):
        return SqlVerdict(level="禁止", action=action, reasons=["SELECT INTO 会创建新表，已拒绝"])
    safe_functions = {
        "count", "sum", "avg", "min", "max", "round", "abs", "ceil", "ceiling", "floor",
        "lower", "upper", "length", "char_length", "btrim", "ltrim", "rtrim",
        "concat", "concat_ws", "replace", "substring", "substr", "left", "right",
        "date_trunc", "date_part", "now", "to_char", "to_date", "to_timestamp",
        "array_agg", "string_agg", "json_agg", "jsonb_agg", "bool_and", "bool_or",
        "row_number", "rank", "dense_rank", "lag", "lead", "first_value", "last_value",
    }
    for node in tree:
        if isinstance(node, ast.FuncCall):
            name = [part.sval for part in node.funcname]
            if name[-1] not in safe_functions or (len(name) > 1 and name[:-1] != ["pg_catalog"]):
                return SqlVerdict(level="禁止", action=action,
                                  reasons=[f"函数不在允许范围内：{'.'.join(name)}"])
            fixed_arity = {"sum": 1, "avg": 1, "min": 1, "max": 1, "abs": 1,
                           "lower": 1, "upper": 1, "length": 1, "char_length": 1,
                           "array_agg": 1, "json_agg": 1, "jsonb_agg": 1,
                           "bool_and": 1, "bool_or": 1, "now": 0}
            if name[-1] in fixed_arity and len(node.args or ()) != fixed_arity[name[-1]]:
                return SqlVerdict(level="禁止", action=action, reasons=["函数参数数量不在允许范围内"])

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

    # ⑤ 表名白名单（含 schema 前缀校验）
    tables, bad_schemas = extract_refs(single)
    tables = sorted(set(tables) | ast_tables)
    bad_schemas = sorted(set(bad_schemas) | ast_schemas)
    notes: list[str] = []
    if bad_schemas:
        return SqlVerdict(
            level="禁止", action=action, tables=tables,
            reasons=[f"不允许访问 schema：{'、'.join(bad_schemas)}；"
                     f"只能访问 public 下的表，且**不要写 schema 前缀**"],
        )
    if allowed_tables is not None:
        # AST 已按 PostgreSQL 规则折叠非引号名称；引号名称必须精确匹配。
        if ast_tables - set(allowed_tables):
            return SqlVerdict(level="禁止", action=action, tables=tables,
                              reasons=["访问了未授权的真实表：" + "、".join(sorted(ast_tables - set(allowed_tables)))])
        unknown = [t for t in tables if t.lower() not in {x.lower() for x in allowed_tables}]
        if unknown:
            return SqlVerdict(
                level="禁止", action=action, tables=tables,
                reasons=[f"访问了未授权的表：{'、'.join(unknown)}；"
                         f"只允许 {'、'.join(sorted(allowed_tables))}（**不要写 schema 前缀**）"],
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

    scalar_aggregate = (isinstance(root, ast.SelectStmt) and not root.groupClause
                        and not root.windowClause and root.op.name == "SETOP_NONE"
                        and bool(root.targetList)
                        and all(isinstance(t.val, ast.FuncCall) and not t.val.over
                                and t.val.funcname[-1].sval in {"count", "sum", "avg", "min", "max"}
                                for t in root.targetList))
    if action == "SELECT" and not scalar_aggregate and not re.search(r"\blimit\b", flat, re.I):
        notes.append("没有 LIMIT，结果会被系统截断到上限行数")

    normalized = single
    calls = [n for n in tree if isinstance(n, ast.FuncCall)]
    if calls:
        # 在生成/修正阶段固定内置函数解析，防止 public 的同名重载被选中。
        from pglast.stream import RawStream
        for call in calls:
            call.funcname = (ast.String(sval="pg_catalog"), call.funcname[-1])
        normalized = RawStream()(root)
    return SqlVerdict(level=level, action=action, tables=tables, notes=notes,
                      statements=1, normalized_sql=normalized)
