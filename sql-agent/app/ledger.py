"""一致性账本：agent_run / agent_task / agent_event / sql_audit 的异步读写。

**这些表在 agent_sql 库**，和智能体执行的 cs_v1 完全隔离。
工程要点（沿用冻结项目的做法）：
  · 认领用「租约 + 版本」，过期才允许接管
  · 更新带 `AND version = %s`，影响行数 0 即过期消息，丢弃
  · 幂等键 + UNIQUE 约束兜底 —— 尤其是「确认后执行」不能重复执行
"""

from __future__ import annotations

import uuid
from typing import Any, Sequence

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool


def _json(value: Any) -> Jsonb:
    return Jsonb(value if value is not None else {})


def _uid(value: Any) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def split_statements(sql: str) -> list[str]:
    """把 .sql 脚本拆成单条语句（忽略注释行与空行）。

    注意：本项目的脚本里刻意**不用** DO $$ 代码块，
    因为其中的分号会被这种朴素拆分误伤。
    """
    statements: list[str] = []
    buf: list[str] = []
    for raw_line in sql.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("--"):
            continue
        buf.append(raw_line)
        if line.endswith(";"):
            statements.append("\n".join(buf).strip().rstrip(";"))
            buf = []
    tail = "\n".join(buf).strip()
    if tail:
        statements.append(tail)
    return [s for s in statements if s.strip()]


class Ledger:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: AsyncConnectionPool | None = None

    # ------------------------------------------------------------ 生命周期

    async def open(self) -> None:
        self._pool = AsyncConnectionPool(
            conninfo=self._dsn, min_size=1, max_size=8, open=False,
            kwargs={"row_factory": dict_row, "autocommit": True},
        )
        await self._pool.open(wait=True, timeout=20)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @property
    def pool(self) -> AsyncConnectionPool:
        if self._pool is None:
            raise RuntimeError("Ledger 尚未 open()")
        return self._pool

    # ------------------------------------------------------------ 基础

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> dict | None:
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, params)
                return await cur.fetchone()

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, params)
                return await cur.fetchall()

    async def ping(self) -> bool:
        row = await self.fetchone("SELECT 1 AS ok")
        return bool(row and row.get("ok") == 1)

    async def apply_schema(self, sql: str) -> int:
        count = 0
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                for statement in split_statements(sql):
                    await cur.execute(statement)
                    count += 1
        return count

    async def reset(self) -> list[str]:
        dropped: list[str] = []
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                for table in ("sql_audit", "agent_event", "agent_task", "agent_run"):
                    await cur.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
                    dropped.append(table)
        return dropped

    # ------------------------------------------------------------ run

    async def create_run(self, run_id: str, question: str, max_rounds: int) -> dict:
        sql = """
        INSERT INTO agent_run (global_task_id, question, status, round, version, max_rounds)
        VALUES (%s, %s, 'created', 0, 0, %s)
        RETURNING *
        """
        row = await self.fetchone(sql, (_uid(run_id), question, max_rounds))
        assert row is not None
        return row

    async def get_run(self, run_id: str) -> dict | None:
        return await self.fetchone(
            "SELECT * FROM agent_run WHERE global_task_id = %s", (_uid(run_id),))

    async def list_runs(self, limit: int = 40) -> list[dict]:
        return await self.fetchall(
            "SELECT global_task_id, question, status, round, version, created_at "
            "FROM agent_run ORDER BY created_at DESC LIMIT %s", (limit,))

    async def delete_run(self, run_id: str) -> None:
        await self.fetchone(
            "DELETE FROM agent_run WHERE global_task_id = %s RETURNING global_task_id",
            (_uid(run_id),))

    async def set_run_status(
        self, run_id: str, status: str, round_no: int | None = None,
        expected_version: int | None = None,
    ) -> int | None:
        """更新运行状态并 version+1；传 expected_version 即启用乐观锁。"""
        if expected_version is None:
            sql = ("UPDATE agent_run SET status = %s, round = COALESCE(%s, round), "
                   "version = version + 1, updated_at = now() "
                   "WHERE global_task_id = %s RETURNING version")
            params: tuple[Any, ...] = (status, round_no, _uid(run_id))
        else:
            sql = ("UPDATE agent_run SET status = %s, round = COALESCE(%s, round), "
                   "version = version + 1, updated_at = now() "
                   "WHERE global_task_id = %s AND version = %s RETURNING version")
            params = (status, round_no, _uid(run_id), expected_version)
        row = await self.fetchone(sql, params)
        return row["version"] if row else None

    # ------------------------------------------------------------ 执行记录

    async def open_task(
        self, run_id: str, sub_task_id: str, attempt: int, role: str, agent_id: str,
        context: dict, idempotency_key: str, lease_seconds: int,
    ) -> dict:
        """认领：写 running + 租约；重复认领会续租并 version+1。"""
        sql = """
        INSERT INTO agent_task (global_task_id, sub_task_id, attempt, role, agent_id, status,
                                input_context, idempotency_key, lease_until, version)
        VALUES (%s, %s, %s, %s, %s, 'running', %s, %s,
                now() + make_interval(secs => %s), 1)
        ON CONFLICT (global_task_id, sub_task_id, attempt) DO UPDATE
           SET status = 'running', agent_id = EXCLUDED.agent_id,
               lease_until = EXCLUDED.lease_until,
               version = agent_task.version + 1, updated_at = now()
        RETURNING *
        """
        row = await self.fetchone(sql, (
            _uid(run_id), sub_task_id, attempt, role, agent_id,
            _json(context), idempotency_key, lease_seconds))
        assert row is not None
        return row

    async def finish_task(
        self, run_id: str, sub_task_id: str, attempt: int, status: str,
        result_ref: str | None = None, error: str | None = None,
    ) -> dict | None:
        sql = """
        UPDATE agent_task
           SET status = %s, result_ref = %s, error = %s,
               lease_until = NULL, version = version + 1, updated_at = now()
         WHERE global_task_id = %s AND sub_task_id = %s AND attempt = %s
        RETURNING *
        """
        return await self.fetchone(sql, (
            status, result_ref, error, _uid(run_id), sub_task_id, attempt))

    async def list_tasks(self, run_id: str) -> list[dict]:
        return await self.fetchall(
            "SELECT * FROM agent_task WHERE global_task_id = %s ORDER BY created_at, role",
            (_uid(run_id),))

    # ------------------------------------------------------------ SQL 审计

    async def audit_sql(
        self, run_id: str, round_no: int, sub_task_id: str, stage: str,
        sql_text: str, sql_hash: str, intent: str | None = None,
        risk_level: str | None = None, action: str | None = None,
        tables: list[str] | None = None, est_rows: int | None = None,
        affected_rows: int | None = None, need_confirm: bool = False,
        confirmed_by: str | None = None, error: str | None = None,
        duration_ms: int | None = None,
    ) -> int:
        sql = """
        INSERT INTO sql_audit (global_task_id, round, sub_task_id, stage, sql_text, sql_hash,
                               intent, risk_level, action, tables, est_rows, affected_rows,
                               need_confirm, confirmed_by, confirmed_at, error, duration_ms)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                CASE WHEN %s::text IS NULL THEN NULL ELSE now() END, %s, %s)
        RETURNING sql_id
        """
        row = await self.fetchone(sql, (
            _uid(run_id), round_no, sub_task_id, stage, sql_text, sql_hash,
            intent, risk_level, action, _json(tables or []), est_rows, affected_rows,
            need_confirm, confirmed_by, confirmed_by, error, duration_ms))
        return int(row["sql_id"]) if row else 0

    async def list_audit(self, run_id: str) -> list[dict]:
        return await self.fetchall(
            "SELECT * FROM sql_audit WHERE global_task_id = %s ORDER BY sql_id",
            (_uid(run_id),))

    # ------------------------------------------------------------ 事件账本

    async def append_event(
        self, run_id: str, role: str, event_type: str, payload: dict,
        version: int = 0, round_no: int = 0,
    ) -> dict:
        sql = """
        INSERT INTO agent_event (global_task_id, role, event_type, version, round, payload)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING seq, created_at
        """
        row = await self.fetchone(sql, (
            _uid(run_id), role, event_type, version, round_no, _json(payload)))
        assert row is not None
        created = row.get("created_at")
        return {
            "seq": row["seq"],
            "created_at": created.isoformat() if created is not None else None,
            "global_task_id": str(run_id),
            "role": role,
            "event_type": event_type,
            "version": version,
            "round": round_no,
            "payload": payload,
        }

    async def list_events(self, run_id: str, after_seq: int = 0) -> list[dict]:
        rows = await self.fetchall(
            """
            SELECT seq, global_task_id, role, event_type, version, round, payload, created_at
              FROM agent_event
             WHERE global_task_id = %s AND seq > %s
             ORDER BY seq
            """,
            (_uid(run_id), after_seq))
        for r in rows:
            if r.get("global_task_id") is not None:
                r["global_task_id"] = str(r["global_task_id"])
            if r.get("created_at") is not None:
                r["created_at"] = r["created_at"].isoformat()
        return rows
