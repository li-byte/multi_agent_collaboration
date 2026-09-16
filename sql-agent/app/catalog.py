"""表目录 —— 智能体关于「库里有什么」的**唯一事实来源**。

## 表信息是**数据**，不是代码

表、字段、关系全部写在外部文件里（默认 `config/tables.json`，可用环境变量
`TABLES_FILE` 指向别处）。改表 = 改文件，代码一行都不用动；
调 `reload()` 立即生效，不必重启。这样换库、换租户、换环境都不用碰代码，
也不会出现「验证逻辑里写死了某张表」这种事。

## 为什么不读数据库

读库就等于「系统去偷看现实」。一旦表被改名或删掉，系统会立刻自己发现并改口，
于是**修正智能体永远等不到那个真实的报错**，也就永远测不出它能不能处理。

所以这份目录是一份**外部给定的、明确的**定义，而不是"实时扫描数据库的结果"；
数据库只是**执行时的现实**。两者不一致时，报错应该由**执行器**抛出来，
再交给修正智能体 —— 这正是要测的东西。

## 两层结构

    第一层（`tier1_text`）  表名 + 一句话描述 + 表之间的关系
                            → 给生成器做**过滤**：这次要碰哪几张表

    第二层（`detail_text`） 过滤后每张表的**完整字段 + 字段说明 + 表说明 + 关系**
                            → 给生成器**生成 SQL**

不要一次把全部细节灌给模型：先粗筛、再看细节，模型的注意力才落对地方。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    desc: str
    pk: bool = False
    nullable: bool = False
    ref: str | None = None            # "orders.id"
    on_delete: str | None = None      # CASCADE / RESTRICT …
    enum: tuple[str, ...] = ()


@dataclass(frozen=True)
class Table:
    name: str
    summary: str                                   # 第一层：一句话
    columns: tuple[Column, ...] = field(default_factory=tuple)
    notes: str = ""                                # 第二层：补充说明

    @property
    def fks(self) -> list[Column]:
        return [c for c in self.columns if c.ref]

    @property
    def pk(self) -> str | None:
        for c in self.columns:
            if c.pk:
                return c.name
        return None


CATALOG: dict[str, Table] = {}
"""当前生效的表目录。**内容来自外部数据文件，不写死在代码里。**

改文件即可换一套表（换库、换租户、换环境），代码一行都不用动；
调 `reload()` 立即生效，不必重启。
"""

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "tables.json"

_source: Path | None = None
_error: str = ""


class CatalogError(RuntimeError):
    """表目录加载失败。**宁可起不来，也不要用一份空目录跑起来** ——
    空目录会让所有 SQL 都判「未授权」，那个报错会把人带偏到完全错误的方向。"""


def _table_from(raw: dict) -> Table:
    cols = []
    for c in raw.get("columns") or []:
        ref = (c.get("ref") or "").strip() or None
        cols.append(Column(
            name=str(c["name"]),
            type=str(c.get("type") or ""),
            desc=str(c.get("desc") or ""),
            pk=bool(c.get("pk")),
            nullable=bool(c.get("nullable")),
            ref=ref,
            on_delete=(c.get("on_delete") or None),
            enum=tuple(c.get("enum") or ()),
        ))
    return Table(name=str(raw["name"]), summary=str(raw.get("summary") or ""),
                 columns=tuple(cols), notes=str(raw.get("notes") or ""))


def load(path: str | Path | None = None) -> dict[str, Table]:
    """从数据文件重新加载表目录（**原地更新**，所以外部持有的 `CATALOG` 引用依然有效）。

    路径优先取参数，其次环境变量 `TABLES_FILE`，最后是随项目发布的
    `config/tables.json`。
    """
    global _source, _error
    p = Path(path) if path else Path(os.environ.get("TABLES_FILE") or DEFAULT_PATH)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CatalogError(f"表目录文件不存在：{p}（可用环境变量 TABLES_FILE 指定）") from exc
    except json.JSONDecodeError as exc:
        raise CatalogError(f"表目录不是合法 JSON：{p}\n{exc}") from exc

    raw_tables = data.get("tables")
    if not isinstance(raw_tables, list) or not raw_tables:
        raise CatalogError(f"表目录里没有任何表：{p}")

    parsed: dict[str, Table] = {}
    for raw in raw_tables:
        if not isinstance(raw, dict) or not raw.get("name"):
            raise CatalogError(f"表目录里有条目缺少 name：{p}")
        if not raw.get("columns"):
            raise CatalogError(f"表 {raw['name']} 没有定义任何字段：{p}")
        t = _table_from(raw)
        parsed[t.name] = t

    # 外键指向的表必须在同一份目录里，否则「关系」是假的
    for t in parsed.values():
        for c in t.fks:
            target = c.ref.split(".")[0]
            if target not in parsed:
                raise CatalogError(
                    f"表 {t.name} 的字段 {c.name} 指向 {c.ref}，但目录里没有表 {target}")

    CATALOG.clear()
    CATALOG.update(parsed)
    _source, _error = p, ""
    return CATALOG


def reload(path: str | Path | None = None) -> dict[str, Table]:
    """运行时换一套表 —— 表信息是动态的，不该要求重启服务。"""
    return load(path)


def source_path() -> str:
    return str(_source) if _source else ""


def info() -> dict:
    """给 `/api/health` 与 `/api/catalog/reload` 用的状态。

    `error` 一定要带上：表目录是**外部文件**，路径写错 / JSON 写坏是最常见的
    上手问题，而这个错误只在"加载的那一刻"存在 —— 不暴露出来，
    用户看到的就是"目录里一张表都没有"这种没头没尾的现象。
    """
    return {"source": source_path(), "tables": names(), "count": len(CATALOG),
            "error": _error, "ok": not _error}


try:                                   # 导入即加载，让「目录坏了」在启动时就暴露
    load()
except CatalogError as exc:            # 服务真正启动时 migrate() 会再读一次并直接报错
    _error = str(exc)


# ------------------------------------------------------------------ 基本查询

def names() -> list[str]:
    return sorted(CATALOG)


def allowed_tables() -> set[str]:
    """sql_guard 的表名白名单 —— 系统定义的表才算数，库里多出来的表一律不认。"""
    return set(CATALOG)


def get(name: str) -> Table | None:
    return CATALOG.get((name or "").strip())


def parents(name: str) -> set[str]:
    t = CATALOG.get(name)
    if not t:
        return set()
    return {c.ref.split(".")[0] for c in t.fks if c.ref}


def children(name: str) -> set[str]:
    return {o.name for o in CATALOG.values()
            for c in o.fks if c.ref and c.ref.split(".")[0] == name}


def connected(names_: list[str]) -> list[str]:
    """把选中表**直接相连**的表补进来，保证 JOIN 走得通。

    模型只挑了 orders，但要按城市筛就得有 customers；
    要按商品统计就得有 order_items。关系本身就是第一层的过滤依据，
    所以这里按关系补齐一圈。
    """
    picked = {n for n in names_ if n in CATALOG}
    for n in list(picked):
        picked |= parents(n) | children(n)
    return sorted(picked)


# ------------------------------------------------------------------ 两层渲染

def tier1_text() -> str:
    """第一层：表名 + 一句话描述 + 关系。给生成器做**过滤**。"""
    lines: list[str] = []
    for name in names():
        t = CATALOG[name]
        lines.append(f"- {name}：{t.summary}")
        rels: list[str] = []
        for c in t.fks:
            if c.ref:
                rels.append(f"{c.name} → {c.ref}")
        for other in names():
            if other == name:
                continue
            for c in CATALOG[other].fks:
                if c.ref and c.ref.split(".")[0] == name:
                    rels.append(f"被 {other}.{c.name} 引用")
        if rels:
            lines.append(f"    关系：{'；'.join(rels)}")
    return "\n".join(lines) if lines else "（系统目录里没有任何表）"


def detail_text(selected: list[str] | None = None) -> str:
    """第二层：过滤后表的完整字段、字段说明、表说明与关系。给生成器**生成 SQL**。"""
    targets = connected(list(selected or [])) or names()
    blocks: list[str] = []
    for name in targets:
        t = CATALOG.get(name)
        if not t:
            continue
        body = [f"表 {name} —— {t.summary}"]
        for c in t.columns:
            bits = f"    {c.name} {c.type}"
            if c.pk:
                bits += "  PK"
            bits += f"  —— {c.desc}"
            if c.ref:
                bits += f"（外键 → {c.ref}"
                bits += f"，ON DELETE {c.on_delete}）" if c.on_delete else "）"
            if c.enum:
                bits += f"  取值：{'/'.join(c.enum)}"
            body.append(bits)
        if t.notes:
            body.append(f"    备注：{t.notes}")
        blocks.append("\n".join(body))
    if not blocks:
        return "（没有匹配到任何表）"
    return "\n\n".join(blocks)


def cascade_note(targets: list[str]) -> list[str]:
    """删除某些表的数据时，外键会牵连哪些表 —— 用于人工确认提示。"""
    notes: list[str] = []
    for name in targets or []:
        t = CATALOG.get(name)
        if not t:
            continue
        for other in names():
            for c in CATALOG[other].fks:
                if not c.ref or c.ref.split(".")[0] != name:
                    continue
                rule = (c.on_delete or "").upper()
                if rule == "CASCADE":
                    notes.append(f"{name} 的删除会**级联删除** {other}.{c.name}")
                elif rule in ("NO ACTION", "RESTRICT", ""):
                    notes.append(f"{name} 被 {other}.{c.name} 引用，删不掉时数据库会报错")
    return notes


# ------------------------------------------------------------------ 关系备注

# 说明：原先这里还有 `relations()` 与 `as_dict()`，只服务于前端的「表结构」视图。
# 那个视图已经去掉（表目录是给智能体看的数据，不是给用户看的页面），
# 两个函数随之删除 —— 没人调用的导出函数留着只会让人以为它还有用。

