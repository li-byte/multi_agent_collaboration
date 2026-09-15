"""表结构快照：智能体的「事实来源」。

刻意用**受限角色**去读 information_schema —— 于是有两层好处：

  1. 智能体只看得到它真正能访问的表，看不见的就一定在授权之外；
  2. sql_guard 的表名白名单直接来自这份快照，不会出现
     「白名单说能访问，实际没权限」的错位。

这和冻结项目里「结论必须引用用户提供的资料」是同一个思路：
**给智能体的事实来源，必须是它能真正依据的那一份。**
"""

from __future__ import annotations

from typing import Any

from .db import BizDatabase


async def snapshot(db: BizDatabase) -> dict[str, Any]:
    """读受限角色可见的表结构。返回 {tables: {...}, allowed: set}。"""
    async with db.runner.connection() as conn:
        async with conn.cursor() as cur:
            # 表（information_schema 会按权限过滤）
            await cur.execute("""
                SELECT table_name
                  FROM information_schema.tables
                 WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
                 ORDER BY table_name
            """)
            names = [r["table_name"] for r in await cur.fetchall()]

            # 字段
            await cur.execute("""
                SELECT table_name, column_name, data_type, is_nullable, column_default
                  FROM information_schema.columns
                 WHERE table_schema = 'public'
                 ORDER BY table_name, ordinal_position
            """)
            columns = await cur.fetchall()

            # 主键
            await cur.execute("""
                SELECT tc.table_name, kcu.column_name
                  FROM information_schema.table_constraints tc
                  JOIN information_schema.key_column_usage kcu
                    ON kcu.constraint_name = tc.constraint_name
                   AND kcu.table_schema = tc.table_schema
                 WHERE tc.constraint_type = 'PRIMARY KEY' AND tc.table_schema = 'public'
                 ORDER BY tc.table_name, kcu.ordinal_position
            """)
            pks = await cur.fetchall()

            # 外键（含级联规则，用于「删除会牵连哪些表」的提示）
            await cur.execute("""
                SELECT tc.table_name       AS child_table,
                       kcu.column_name     AS child_column,
                       ccu.table_name      AS parent_table,
                       ccu.column_name     AS parent_column,
                       rc.delete_rule      AS delete_rule
                  FROM information_schema.table_constraints tc
                  JOIN information_schema.key_column_usage kcu
                    ON kcu.constraint_name = tc.constraint_name
                   AND kcu.table_schema = tc.table_schema
                  JOIN information_schema.constraint_column_usage ccu
                    ON ccu.constraint_name = tc.constraint_name
                   AND ccu.table_schema = tc.table_schema
                  JOIN information_schema.referential_constraints rc
                    ON rc.constraint_name = tc.constraint_name
                   AND rc.constraint_schema = tc.table_schema
                 WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = 'public'
                 ORDER BY tc.table_name
            """)
            fks = await cur.fetchall()

    tables: dict[str, dict] = {}
    for name in names:
        tables[name] = {
            "name": name,
            "columns": [
                {"name": c["column_name"], "type": c["data_type"],
                 "null": c["is_nullable"] == "YES", "default": c["column_default"]}
                for c in columns if c["table_name"] == name
            ],
            "pk": [r["column_name"] for r in pks if r["table_name"] == name],
            "fk": [
                {"column": r["child_column"], "ref_table": r["parent_table"],
                 "ref_column": r["parent_column"], "on_delete": r["delete_rule"]}
                for r in fks if r["child_table"] == name
            ],
            "referenced_by": [
                {"table": r["child_table"], "column": r["child_column"],
                 "on_delete": r["delete_rule"]}
                for r in fks if r["parent_table"] == name
            ],
        }
    return {"tables": tables, "allowed": set(tables)}


def render(snap: dict) -> str:
    """渲染成给 LLM 看的表结构说明。"""
    lines: list[str] = []
    for name, t in sorted(snap.get("tables", {}).items()):
        cols = []
        for c in t["columns"]:
            bits = f"{c['name']} {c['type']}"
            if c["name"] in t["pk"]:
                bits += " PK"
            if not c["null"]:
                bits += " NOT NULL"
            if c["default"]:
                bits += f" DEFAULT {c['default']}"
            cols.append(bits)
        lines.append(f"表 {name}")
        for c in cols:
            lines.append(f"    {c}")
        for fk in t["fk"]:
            lines.append(f"    外键 {fk['column']} → {fk['ref_table']}.{fk['ref_column']}"
                         f"（ON DELETE {fk['on_delete']}）")
    return "\n".join(lines) if lines else "（没有任何可访问的表）"


def cascade_note(snap: dict, target_tables: list[str]) -> list[str]:
    """删除某张表的数据时，外键会牵连哪些表。用于人工确认提示。"""
    notes: list[str] = []
    for name in target_tables:
        t = snap.get("tables", {}).get(name)
        if not t:
            continue
        for ref in t.get("referenced_by", []):
            rule = (ref.get("on_delete") or "").upper()
            if rule == "CASCADE":
                notes.append(f"{name} 的删除会**级联删除** {ref['table']}.{ref['column']}")
            elif rule in ("NO ACTION", "RESTRICT"):
                notes.append(f"{name} 被 {ref['table']} 引用（{rule}），删不掉时数据库会报错")
    return notes
