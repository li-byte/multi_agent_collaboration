"""业务库（cs_v1）访问层。

两个连接池，职责刻意分开：

    admin   （postgres）          建表、灌种子数据 —— 只在初始化时用
    runner  （agent_sql_runner）  EXPLAIN 校验 + 执行用户 SQL —— 只跑受限角色的活

**用户 SQL 永远不经过 admin 连接。** 这样即使静态防线被绕过，
数据库层也没有 DDL 权限可以滥用。
"""

from __future__ import annotations

import json
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool


def jsonable(value: Any) -> Any:
    """把查询结果转成能进 JSON 的形式。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    return str(value)


class BizDatabase:
    """cs_v1：智能体执行 SQL 的目标库。"""

    def __init__(self, admin_dsn: str, runner_dsn: str, statement_timeout_ms: int = 5000) -> None:
        self._admin_dsn = admin_dsn
        self._runner_dsn = runner_dsn
        self._timeout_ms = statement_timeout_ms
        self._admin: AsyncConnectionPool | None = None
        self._runner: AsyncConnectionPool | None = None

    # ------------------------------------------------------------ 生命周期

    async def open(self) -> None:
        self._admin = AsyncConnectionPool(
            conninfo=self._admin_dsn, min_size=1, max_size=4, open=False,
            kwargs={"row_factory": dict_row, "autocommit": True},
        )
        await self._admin.open(wait=True, timeout=20)

        # 受限连接：给每条语句上超时，兜住「跑一个超慢查询」这类滥用
        self._runner = AsyncConnectionPool(
            conninfo=self._runner_dsn, min_size=1, max_size=4, open=False,
            kwargs={"row_factory": dict_row, "autocommit": True,
                    "options": f"-c statement_timeout={self._timeout_ms}"},
        )
        await self._runner.open(wait=True, timeout=20)

    async def close(self) -> None:
        for pool in (self._runner, self._admin):
            if pool is not None:
                await pool.close()
        self._admin = self._runner = None

    @property
    def admin(self) -> AsyncConnectionPool:
        if self._admin is None:
            raise RuntimeError("BizDatabase 尚未 open()")
        return self._admin

    @property
    def runner(self) -> AsyncConnectionPool:
        if self._runner is None:
            raise RuntimeError("BizDatabase 尚未 open()")
        return self._runner

    # ------------------------------------------------------------ 建表 / 种子

    async def apply_script(self, sql: str, splitter) -> int:
        """在 admin 连接上按语句执行脚本（建表 / 授权 / 种子）。"""
        count = 0
        async with self.admin.connection() as conn:
            async with conn.cursor() as cur:
                for statement in splitter(sql):
                    await cur.execute(statement)
                    count += 1
        return count

    async def runner_identity(self) -> dict:
        """确认执行身份：受限角色是谁、有哪些权限。"""
        async with self.runner.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT current_user AS u, current_database() AS d")
                row = await cur.fetchone() or {}
                await cur.execute("""
                    SELECT table_name, string_agg(privilege_type, ',' ORDER BY privilege_type) AS privs
                      FROM information_schema.table_privileges
                     WHERE grantee = current_user AND table_schema = 'public'
                     GROUP BY table_name ORDER BY table_name
                """)
                return {"user": row.get("u"), "database": row.get("d"),
                        "privileges": await cur.fetchall()}

    # ------------------------------------------------------------ EXPLAIN 校验

    async def explain(self, sql: str) -> dict:
        """用 EXPLAIN（**不加 ANALYZE**）做语法+语义校验，并预估影响行数。

        这是最划算的一步：PostgreSQL 会完整做语法分析、表/字段存在性检查、
        类型检查、计划生成，但**不执行**。所以 DELETE/UPDATE 也能安全地预估。
        """
        async with self.runner.connection() as conn:
            async with conn.cursor() as cur:
                try:
                    await cur.execute(f"EXPLAIN (FORMAT JSON) {sql}")
                    rows = await cur.fetchall()
                except Exception as exc:  # noqa: BLE001
                    return {"ok": False,
                            "error": str(exc).strip(),
                            "error_type": type(exc).__name__}
        plan = []
        if rows:
            value = rows[0].get("QUERY PLAN")
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except json.JSONDecodeError:
                    value = []
            plan = value or []
        node = (plan[0] or {}).get("Plan", {}) if plan else {}
        return {
            "ok": True,
            "node_type": node.get("Node Type"),
            "est_rows": node.get("Plan Rows"),
            "plan": plan,
        }

    # ------------------------------------------------------------ 执行

    async def execute(self, sql: str, max_rows: int = 500) -> dict:
        """在受限连接上执行一条 SQL。

        · 包在**显式事务**里 —— 出错自动回滚，不留半截状态
        · 结果行数截断到 max_rows，并标记 truncated
        """
        started = datetime.now()
        async with self.runner.connection() as conn:
            try:
                async with conn.transaction():
                    async with conn.cursor() as cur:
                        await cur.execute(sql)
                        if cur.description is not None:
                            columns = [d.name for d in cur.description]
                            rows = await cur.fetchmany(max_rows + 1)
                            truncated = len(rows) > max_rows
                            return {
                                "ok": True, "kind": "rows",
                                "columns": columns,
                                "rows": [[jsonable(v) for v in r.values()] for r in rows[:max_rows]],
                                "rowcount": len(rows[:max_rows]),
                                "truncated": truncated,
                                "duration_ms": int((datetime.now() - started).total_seconds() * 1000),
                            }
                        return {
                            "ok": True, "kind": "affected",
                            "rowcount": cur.rowcount,
                            "duration_ms": int((datetime.now() - started).total_seconds() * 1000),
                        }
            except Exception as exc:  # noqa: BLE001
                return {
                    "ok": False,
                    "error": str(exc).strip(),
                    "error_type": type(exc).__name__,
                    "duration_ms": int((datetime.now() - started).total_seconds() * 1000),
                }
