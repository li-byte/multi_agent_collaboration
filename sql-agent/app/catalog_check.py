"""按**系统内置的表信息**静态核对 SQL 里的表和字段。

只读 `app/catalog.py`，**一个字节都不碰数据库** ——
理由和校验阶段零数据库操作是同一条：查库就等于让系统去偷看现实，
表被改名时它会自己发现并改口，真实报错就永远轮不到执行器抛出来。

它挡的是这一类：「SQL 语法没错、表也在目录里，但字段是编的 / 抄错的」。
只挡表和字段**存在性**，不做语义判断（那归模型）。
保守优先：**拿不准的宁可放过**，误杀一条正常查询比漏掉一条更糟。
"""

from __future__ import annotations

import re

from . import catalog

# SQL 关键字 / 函数里会大量出现、但不是字段名的词
_KEYWORDS = {
    "select", "from", "where", "group", "by", "order", "having", "limit", "offset",
    "join", "inner", "left", "right", "full", "outer", "cross", "on", "using", "as",
    "and", "or", "not", "in", "is", "null", "like", "ilike", "between", "exists",
    "case", "when", "then", "else", "end", "distinct", "all", "union", "except",
    "intersect", "asc", "desc", "nulls", "first", "last", "with", "recursive",
    "insert", "into", "values", "update", "set", "delete", "returning", "conflict",
    "do", "nothing", "true", "false", "interval", "cast", "over", "partition",
    "filter", "within", "lateral", "natural", "if", "array", "row", "any", "some",
}
_FUNCS = {
    "count", "sum", "avg", "min", "max", "coalesce", "nullif", "greatest", "least",
    "abs", "round", "ceil", "floor", "trunc", "mod", "power", "sqrt", "now",
    "current_date", "current_timestamp", "date_trunc", "extract", "to_char",
    "to_date", "to_number", "length", "lower", "upper", "trim", "ltrim", "rtrim",
    "substring", "replace", "concat", "string_agg", "array_agg", "jsonb_array_elements_text",
    "jsonb_array_elements", "json_agg", "row_number", "rank", "dense_rank", "lag",
    "lead", "generate_series", "unnest", "age", "justify_interval",
}

_STR = re.compile(r"'(?:[^']|'')*'")          # 字符串字面量
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_QUALIFIED = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b")
_FROM = re.compile(r"\b(?:from|join)\s+([A-Za-z_][A-Za-z0-9_.]*)", re.I)
_ALIAS = re.compile(r"\b(?:from|join)\s+([A-Za-z_][A-Za-z0-9_.]*)\s+(?:as\s+)?([A-Za-z_][A-Za-z0-9_]*)", re.I)
_OUT_ALIAS = re.compile(r"\bas\s+([A-Za-z_][A-Za-z0-9_]*)", re.I)
# CTE 名：WITH x AS (...), y AS (...) —— x / y 是临时结果集，不是表字段，
# 不排除掉会把 `SELECT * FROM x` 误判成「目录里没有 x 这张表/字段」
_CTE = re.compile(r"(?:\bwith\b|,)\s*([A-Za-z_][A-Za-z0-9_]*)\s+as\s*\(", re.I)


def _strip_literals(sql: str) -> str:
    """抹掉字符串字面量与注释，避免把里面的内容当成标识符。"""
    out = _STR.sub("''", sql)
    out = re.sub(r"--[^\n]*", " ", out)
    out = re.sub(r"/\*.*?\*/", " ", out, flags=re.S)
    return out


def _relations(code: str) -> dict[str, str]:
    """返回 {别名或表名: 目录里的表名}。只认目录里有的表。"""
    alias: dict[str, str] = {}
    for m in _FROM.finditer(code):
        raw = m.group(1)
        table = raw.split(".")[-1].lower()
        if table in catalog.CATALOG:
            alias[table] = table
    for m in _ALIAS.finditer(code):
        raw, name = m.group(1), m.group(2)
        table = raw.split(".")[-1].lower()
        if name.lower() in _KEYWORDS or not name:
            continue
        if table in catalog.CATALOG:
            alias[name.lower()] = table
    return alias


_TBL_FROM = re.compile(r"\b(?:from|join)\s+([A-Za-z_][A-Za-z0-9_.]*)", re.I)
# 注意：**不加 `\b`**。Python 的 `\w` 默认把汉字也算单词字符，
# 于是「表里」之间没有词边界，`(?:表|table)\b` 会漏掉「users 表里」这种写法。
_TBL_WORD = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*(?:表|table)", re.I)
_TBL_WORD2 = re.compile(r"表\s*([A-Za-z_][A-Za-z0-9_]*)")


def unknown_tables(text: str) -> list[str]:
    """从**自由文本**里找出「像表名、但不在系统目录里」的标识符。

    为什么必须有这一步：用户说「查 users 表」，而目录里没有 users。
    这时模型会**很聪明地**换成 customers 顶上 ——语句跑通了、还执行成功、返回 0 行，
    可用户问的是 A、查的是 B。护栏拦不住，因为它拿的是「生成器自己选定的表」去比对，
    自己跟自己比当然过。

    所以要在**链路开始前**就把用户提到的表认出来，不在目录里就直接说不可用，
    绝不允许换一张顶替。这不是"读数据库"，是跟一份写死的目录比字符串。
    """
    code = _strip_literals(text or "")
    found: set[str] = set()
    for rx in (_TBL_FROM, _TBL_WORD, _TBL_WORD2):
        for m in rx.finditer(code):
            name = m.group(1).split(".")[-1].lower()
            if len(name) < 2 or name in _KEYWORDS or name in _FUNCS:
                continue
            found.add(name)
    return sorted(n for n in found if n not in catalog.CATALOG)


def check_sql(sql: str) -> tuple[bool, str, list[str]]:
    """按目录核对 SQL 用到的表与字段。

    返回 (是否通过, 说明, 有问题的引用列表)。**不碰数据库。**
    """
    code = _strip_literals(sql or "")
    low = code.lower()

    tables = {t for t in _FROM.findall(low) if t.split(".")[-1] in catalog.CATALOG}
    if not tables:
        # 没有一张目录里的表 —— 表层面的问题交给 sql_guard 的白名单去报，这里不重复
        return True, "SQL 里没有目录中的表，字段核对跳过", []

    alias = _relations(code)
    known_cols: set[str] = set()
    for t in set(alias.values()):
        tb = catalog.get(t)
        if tb:
            known_cols |= {c.name.lower() for c in tb.columns}
    for t in tables:
        tb = catalog.get(t)
        if tb:
            known_cols |= {c.name.lower() for c in tb.columns}

    offenders: list[str] = []

    # ① 限定引用 `别名.字段` —— 高置信度，能直接定位到某张表
    for m in _QUALIFIED.finditer(code):
        owner, col = m.group(1).lower(), m.group(2).lower()
        if owner in ("public",) or col in _KEYWORDS:
            continue
        table = alias.get(owner)
        if not table:
            continue
        tb = catalog.get(table)
        if tb and col not in {c.name.lower() for c in tb.columns}:
            offenders.append(f"{table}.{col}（{table} 没有这个字段）")

    # ② 非限定标识符 —— 保守：只在「排除了关键字/函数/表名/别名/输出别名」之后才算
    out_aliases = {m.group(1).lower() for m in _OUT_ALIAS.finditer(code)}
    ctes = {m.group(1).lower() for m in _CTE.finditer(code)}
    skip = (_KEYWORDS | _FUNCS | set(alias) | out_aliases | ctes
            | {t.lower() for t in tables})
    # 函数名（后面紧跟括号的）也不算
    funcs_used = {m.group(1).lower() for m in re.finditer(r"\b([A-Za-z_]\w*)\s*\(", code)}
    skip |= funcs_used

    for m in _IDENT.finditer(code):
        name = m.group(0).lower()
        if name in skip or name in known_cols:
            continue
        # 前后紧挨着 '.' 的是限定引用的一部分，上面已经查过
        s, e = m.start(), m.end()
        if (s > 0 and code[s - 1] == ".") or (e < len(code) and code[e] == "."):
            continue
        # 紧跟在 :: 后面的是类型名（如 ::text）
        if code[max(0, s - 2):s] == "::":
            continue
        offenders.append(f"{name}（目录里没有这个字段）")

    if offenders:
        uniq = sorted(set(offenders))
        return False, ("按表信息核对不过：" + "；".join(uniq[:6])
                       + "。可用的表与字段见系统目录（顶栏「🗂 表结构」）"), uniq
    return True, f"表和字段都能在系统目录里对上（{len(tables)} 张表）", []
