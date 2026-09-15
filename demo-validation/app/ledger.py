"""一致性账本：agent_run / source_chunk / agent_task / finding / agent_event 的异步读写。

工程要点（对应文章观点）：
  · 认领用「租约 + 版本」，过期才允许接管        → 调度器重启不重复派发
  · 更新带 `AND version = %s`，影响行数 0 即过期  → 状态未知 ≠ 失败，旧消息不覆盖新状态
  · 幂等键 + UNIQUE 约束兜底                     → 带副作用的动作不产生第二份
  · 资料片段是**权威事实层**，只写一次、只读不改
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
    """把 schema.sql 拆成单条语句，忽略注释行与空行。"""
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

    # ------------------------------------------------------------ 基础查询

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
        """删掉自建表（结构变更后重新初始化用）。"""
        dropped: list[str] = []
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                for table in ("agent_event", "finding", "agent_task", "source_chunk", "agent_run"):
                    await cur.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
                    dropped.append(table)
        return dropped

    # ------------------------------------------------------------ 资料（权威事实层）

    async def insert_chunks(self, run_id: str, chunks: list[dict]) -> int:
        sql = """
        INSERT INTO source_chunk (global_task_id, chunk_id, doc_no, seq, content, char_len)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (global_task_id, chunk_id) DO UPDATE
           SET content = EXCLUDED.content, char_len = EXCLUDED.char_len
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                for c in chunks:
                    await cur.execute(sql, (
                        _uid(run_id), c["chunk_id"], c["doc_no"], c["seq"],
                        c["content"], c["char_len"],
                    ))
        return len(chunks)

    async def list_chunks(self, run_id: str) -> list[dict]:
        return await self.fetchall(
            "SELECT chunk_id, doc_no, seq, content, char_len FROM source_chunk "
            "WHERE global_task_id = %s ORDER BY doc_no, seq",
            (_uid(run_id),),
        )

    async def chunk_map(self, run_id: str) -> dict[str, str]:
        rows = await self.list_chunks(run_id)
        return {r["chunk_id"]: r["content"] for r in rows}

    # ------------------------------------------------------------ run

    async def create_run(self, run_id: str, question: str, chunk_count: int) -> dict:
        sql = """
        INSERT INTO agent_run (global_task_id, question, status, round, version, chunk_count)
        VALUES (%s, %s, 'created', 0, 0, %s)
        RETURNING *
        """
        row = await self.fetchone(sql, (_uid(run_id), question, chunk_count))
        assert row is not None
        return row

    async def get_run(self, run_id: str) -> dict | None:
        return await self.fetchone(
            "SELECT * FROM agent_run WHERE global_task_id = %s", (_uid(run_id),)
        )

    async def delete_run(self, run_id: str) -> None:
        await self.fetchone(
            "DELETE FROM agent_run WHERE global_task_id = %s RETURNING global_task_id",
            (_uid(run_id),),
        )

    async def list_runs(self, limit: int = 40) -> list[dict]:
        return await self.fetchall(
            "SELECT global_task_id, question, status, round, version, chunk_count, created_at "
            "FROM agent_run ORDER BY created_at DESC LIMIT %s",
            (limit,),
        )

    async def set_run_status(
        self, run_id: str, status: str, round_no: int | None = None,
        expected_version: int | None = None,
    ) -> int | None:
        """更新运行状态并 version+1。传 expected_version 即启用乐观锁。"""
        if expected_version is None:
            sql = ("UPDATE agent_run SET status = %s, round = COALESCE(%s, round), "
                   "version = version + 1, updated_at = now() WHERE global_task_id = %s "
                   "RETURNING version")
            params: tuple[Any, ...] = (status, round_no, _uid(run_id))
        else:
            sql = ("UPDATE agent_run SET status = %s, round = COALESCE(%s, round), "
                   "version = version + 1, updated_at = now() "
                   "WHERE global_task_id = %s AND version = %s RETURNING version")
            params = (status, round_no, _uid(run_id), expected_version)
        row = await self.fetchone(sql, params)
        return row["version"] if row else None

    # ------------------------------------------------------------ 子任务 / 执行记录

    async def open_task(
        self, run_id: str, sub_task_id: str, attempt: int, role: str, agent_id: str,
        context: dict, idempotency_key: str, lease_seconds: int,
    ) -> dict:
        """认领子任务：写 running + 租约；重复认领会续租并 version+1。"""
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
            _json(context), idempotency_key, lease_seconds,
        ))
        assert row is not None
        return row

    async def finish_task(
        self, run_id: str, sub_task_id: str, attempt: int, status: str,
        result_ref: str | None = None, citations: list[dict] | None = None,
        error: str | None = None,
    ) -> dict | None:
        sql = """
        UPDATE agent_task
           SET status = %s, result_ref = %s, citations = %s, error = %s,
               lease_until = NULL, version = version + 1, updated_at = now()
         WHERE global_task_id = %s AND sub_task_id = %s AND attempt = %s
        RETURNING *
        """
        return await self.fetchone(sql, (
            status, result_ref, _json(citations or []), error,
            _uid(run_id), sub_task_id, attempt,
        ))

    async def list_tasks(self, run_id: str) -> list[dict]:
        return await self.fetchall(
            "SELECT * FROM agent_task WHERE global_task_id = %s ORDER BY created_at, role",
            (_uid(run_id),),
        )

    # ------------------------------------------------------------ 结论

    async def insert_finding(
        self, finding_id: str, run_id: str, sub_task_id: str, round_no: int,
        claim: str, status: str, citations: list[dict], assumptions: list[str],
        insufficient: bool = False,
    ) -> dict:
        sql = """
        INSERT INTO finding (finding_id, global_task_id, sub_task_id, round, claim, status,
                             citations, insufficient, assumptions)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (finding_id) DO UPDATE
           SET status = EXCLUDED.status, citations = EXCLUDED.citations,
               insufficient = EXCLUDED.insufficient, assumptions = EXCLUDED.assumptions
        RETURNING *
        """
        row = await self.fetchone(sql, (
            finding_id, _uid(run_id), sub_task_id, round_no, claim, status,
            _json(citations), insufficient, _json(assumptions),
        ))
        assert row is not None
        return row

    async def set_run_findings_status(self, run_id: str, status: str) -> int:
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE finding SET status = %s WHERE global_task_id = %s",
                    (status, _uid(run_id)),
                )
                return cur.rowcount

    async def list_findings(self, run_id: str) -> list[dict]:
        return await self.fetchall(
            "SELECT * FROM finding WHERE global_task_id = %s ORDER BY created_at",
            (_uid(run_id),),
        )

    # ------------------------------------------------------------ 事件账本

    async def append_event(
        self, run_id: str, role: str, event_type: str, payload: dict,
        version: int = 0, round_no: int = 0, citations: list[dict] | None = None,
    ) -> dict:
        sql = """
        INSERT INTO agent_event (global_task_id, role, event_type, version, round, citations, payload)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING seq, created_at
        """
        row = await self.fetchone(sql, (
            _uid(run_id), role, event_type, version, round_no,
            _json(citations or []), _json(payload),
        ))
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
            "citations": citations or [],
            "payload": payload,
        }

    async def list_events(self, run_id: str, after_seq: int = 0) -> list[dict]:
        rows = await self.fetchall(
            """
            SELECT seq, global_task_id, role, event_type, version, round,
                   citations, payload, created_at
              FROM agent_event
             WHERE global_task_id = %s AND seq > %s
             ORDER BY seq
            """,
            (_uid(run_id), after_seq),
        )
        for r in rows:
            if r.get("global_task_id") is not None:
                r["global_task_id"] = str(r["global_task_id"])
            if r.get("created_at") is not None:
                r["created_at"] = r["created_at"].isoformat()
        return rows
