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
                for table in ("llm_call", "agent_memory", "sql_audit", "agent_event",
                      "agent_task", "agent_run"):
                    await cur.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
                    dropped.append(table)
        return dropped

    # ------------------------------------------------------------ run

    async def create_run(self, run_id: str, question: str, max_rounds: int,
                         conversation_id: str | None = None,
                         turn: int = 1) -> dict:
        """新建一轮。`conversation_id` 相同就是同一个会话的后续追问。"""
        sql = """
        INSERT INTO agent_run (global_task_id, question, status, round, version,
                               max_rounds, conversation_id, turn)
        VALUES (%s, %s, 'created', 0, 0, %s, %s, %s)
        RETURNING *
        """
        row = await self.fetchone(sql, (_uid(run_id), question, max_rounds,
                                        conversation_id or str(run_id), turn))
        assert row is not None
        return row

    async def get_run(self, run_id: str) -> dict | None:
        return await self.fetchone(
            "SELECT * FROM agent_run WHERE global_task_id = %s", (_uid(run_id),))

    async def list_turns(self, conversation_id: str) -> list[dict]:
        """一个会话里的所有轮次，按先后顺序。"""
        return await self.fetchall(
            """SELECT global_task_id, question, status, round, turn, created_at, updated_at
                 FROM agent_run WHERE conversation_id = %s
                ORDER BY turn, created_at""", (conversation_id,))

    async def next_turn(self, conversation_id: str) -> int:
        row = await self.fetchone(
            "SELECT COALESCE(max(turn), 0) + 1 AS n FROM agent_run WHERE conversation_id = %s",
            (conversation_id,))
        return int((row or {}).get("n") or 1)

    async def list_runs(self, limit: int = 40) -> list[dict]:
        """会话列表：**一个会话一条**（多轮提问合并成一条），
        按最后活动时间倒序，并带上「这个会话一共改了什么数据」的摘要。

        没有 conversation_id 的老数据（或单轮提问）按 `global_task_id` 各自成条，
        所以升级不会打乱既有记录。
        """
        sql = """
        SELECT c.conv_id,
               (array_agg(c.question        ORDER BY c.turn DESC, c.created_at DESC))[1] AS question,
               (array_agg(c.status          ORDER BY c.turn DESC, c.created_at DESC))[1] AS status,
               (array_agg(c.global_task_id  ORDER BY c.turn DESC, c.created_at DESC))[1] AS global_task_id,
               (array_agg(c.turn            ORDER BY c.turn DESC, c.created_at DESC))[1] AS turn,
               count(*) AS turns,
               max(c.updated_at) AS updated_at,
               min(c.created_at) AS created_at,
               COALESCE(s.reads, 0)    AS reads,
               COALESCE(s.writes, 0)   AS writes,
               COALESCE(s.affected, 0) AS affected,
               COALESCE(u.calls, 0)    AS llm_calls,
               COALESCE(u.tokens, 0)   AS llm_tokens,
               tt.tables               AS write_tables
          FROM (
              SELECT COALESCE(conversation_id, global_task_id::text) AS conv_id,
                     global_task_id, question, status, turn, created_at, updated_at
                FROM agent_run
          ) c
          LEFT JOIN (
              SELECT COALESCE(r.conversation_id, r.global_task_id::text) AS conv_id,
                     count(*) FILTER (WHERE a.stage = 'executed' AND a.action =  'SELECT') AS reads,
                     count(*) FILTER (WHERE a.stage = 'executed' AND a.action <> 'SELECT') AS writes,
                     COALESCE(sum(a.affected_rows) FILTER (
                         WHERE a.stage = 'executed' AND a.action <> 'SELECT'), 0)         AS affected
                FROM sql_audit a
                JOIN agent_run r ON r.global_task_id = a.global_task_id
               GROUP BY 1
          ) s ON s.conv_id = c.conv_id
          LEFT JOIN (
              SELECT COALESCE(r.conversation_id, r.global_task_id::text) AS conv_id,
                     array_agg(DISTINCT t ORDER BY t) AS tables
                FROM sql_audit a
                JOIN agent_run r ON r.global_task_id = a.global_task_id,
                     LATERAL jsonb_array_elements_text(a.tables) AS t
               WHERE a.stage = 'executed' AND a.action <> 'SELECT'
               GROUP BY 1
          ) tt ON tt.conv_id = c.conv_id
           LEFT JOIN (
               SELECT COALESCE(r.conversation_id, r.global_task_id::text) AS conv_id,
                      count(*)                        AS calls,
                      COALESCE(sum(l.total_tokens), 0) AS tokens
                 FROM llm_call l
                 JOIN agent_run r ON r.global_task_id = l.global_task_id
                GROUP BY 1
           ) u ON u.conv_id = c.conv_id
         GROUP BY c.conv_id, s.reads, s.writes, s.affected, u.calls, u.tokens, tt.tables
         ORDER BY max(c.updated_at) DESC
         LIMIT %s
        """
        return await self.fetchall(sql, (limit,))

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

    # ------------------------------------------------------------ 共享记忆

    async def save_memory(
        self, run_id: str, sub_task_id: str, claim: str, *, sql_id: int | None,
        sql_text: str, action: str | None, stage: str, verified: bool,
        entities: dict | None = None, columns: list | None = None,
        rows: list | None = None, rowcount: int | None = None, round_no: int = 0,
        truncated: bool = False, sampled: bool = False,
        interpretation: str = "", interpretation_verified: bool = False,
    ) -> int:
        """写一条共享记忆（同一子任务只保留最新一条），返回 mem_id。

        **`verified` 只能由「执行器真的执行过」置位** —— 这是这条记忆算不算
        「事实」的唯一判据，所以由调用方按 stage 决定，不接受模型说辞。
        """
        sql = """
        INSERT INTO agent_memory (global_task_id, sub_task_id, claim, sql_id, sql_text,
                                  action, stage, verified, entities, columns, rows,
                                  rowcount, round, truncated, sampled, interpretation, interpretation_verified)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (global_task_id, sub_task_id) DO UPDATE SET
            claim = EXCLUDED.claim, sql_id = EXCLUDED.sql_id, sql_text = EXCLUDED.sql_text,
            action = EXCLUDED.action, stage = EXCLUDED.stage, verified = EXCLUDED.verified,
            entities = EXCLUDED.entities, columns = EXCLUDED.columns, rows = EXCLUDED.rows,
            rowcount = EXCLUDED.rowcount, round = EXCLUDED.round,
            truncated = EXCLUDED.truncated, sampled = EXCLUDED.sampled,
            interpretation = EXCLUDED.interpretation,
            interpretation_verified = EXCLUDED.interpretation_verified, created_at = now()
        RETURNING mem_id
        """
        row = await self.fetchone(sql, (
            _uid(run_id), sub_task_id, claim, sql_id, sql_text, action, stage, verified,
            _json(entities or {}), _json(columns or []), _json(rows or []),
            rowcount, round_no, truncated, sampled, interpretation, interpretation_verified))
        assert row is not None
        return int(row["mem_id"])

    async def list_memory(self, run_id: str,
                          sub_task_ids: list[str] | None = None) -> list[dict]:
        """读共享记忆。给了 `sub_task_ids` 就只取这几条（按依赖取，不全灌）。"""
        if sub_task_ids:
            rows = await self.fetchall(
                """SELECT * FROM agent_memory
                    WHERE global_task_id = %s AND sub_task_id = ANY(%s)
                    ORDER BY mem_id""",
                (_uid(run_id), list(sub_task_ids)))
        else:
            rows = await self.fetchall(
                "SELECT * FROM agent_memory WHERE global_task_id = %s ORDER BY mem_id",
                (_uid(run_id),))
        # 记忆要进 state、进事件流、进前端 —— 这里统一成可序列化的形状
        for r in rows:
            if r.get("global_task_id") is not None:
                r["global_task_id"] = str(r["global_task_id"])
            created = r.get("created_at")
            if hasattr(created, "isoformat"):
                r["created_at"] = created.isoformat()
        return rows

    # ------------------------------------------------------------ 大模型消耗

    async def save_llm_call(
        self, run_id: str, *, role: str, stage: str, round_no: int = 0, cursor: int = 0,
        attempt: int = 1, model: str | None = None, prompt_tokens: int = 0,
        completion_tokens: int = 0, total_tokens: int = 0, cached_tokens: int = 0,
        reasoning_tokens: int = 0, duration_ms: int | None = None,
        ok: bool = True, error: str | None = None,
    ) -> int:
        """记一次大模型调用的消耗。返回 call_id。

        失败的调用**也要记**：它一样花了输入 token，而且"为什么重试"要看得见。
        所以 `ok=false` 时 token 记 0 或不记由调用方决定 —— 这里只如实存。
        """
        sql = """
        INSERT INTO llm_call (global_task_id, role, stage, round, cursor, attempt, model,
                              prompt_tokens, completion_tokens, total_tokens,
                              cached_tokens, reasoning_tokens, duration_ms, ok, error)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING call_id
        """
        row = await self.fetchone(sql, (
            _uid(run_id), role, stage, round_no, cursor, attempt, model,
            prompt_tokens, completion_tokens, total_tokens,
            cached_tokens, reasoning_tokens, duration_ms, ok,
            (error or "")[:500] or None))
        assert row is not None
        return int(row["call_id"])

    async def list_llm_calls(self, run_id: str) -> list[dict]:
        """本轮的消耗明细，按调用顺序。"""
        rows = await self.fetchall(
            """SELECT call_id, global_task_id, role, stage, round, cursor, attempt, model,
                      prompt_tokens, completion_tokens, total_tokens, cached_tokens,
                      reasoning_tokens, duration_ms, ok, error, created_at
                 FROM llm_call WHERE global_task_id = %s ORDER BY call_id""",
            (_uid(run_id),))
        for r in rows:
            if r.get("global_task_id") is not None:
                r["global_task_id"] = str(r["global_task_id"])
            if r.get("created_at") is not None:
                r["created_at"] = r["created_at"].isoformat()
        return rows

    async def usage_summary(self, run_id: str | None = None, *, conversation_id: str | None = None) -> dict:
        """消耗汇总。按 run_id 或 conversation_id 过滤，均不指定时统计全部会话。

        只统计真数（`SUM`），不做任何估算 —— 数字要能对账。
        """
        where, params = ("WHERE global_task_id = %s", (_uid(run_id),)) if run_id else ("", ())
        if conversation_id:
            if run_id:
                raise ValueError("run_id 和 conversation_id 不能同时指定")
            where = "WHERE global_task_id IN (SELECT global_task_id FROM agent_run WHERE conversation_id = %s)"
            params = (_uid(conversation_id),)
        total = await self.fetchone(
            f"""SELECT count(*) AS calls,
                       coalesce(sum(prompt_tokens), 0)     AS prompt_tokens,
                       coalesce(sum(completion_tokens), 0) AS completion_tokens,
                       coalesce(sum(total_tokens), 0)      AS total_tokens,
                       coalesce(sum(cached_tokens), 0)     AS cached_tokens,
                       coalesce(sum(reasoning_tokens), 0)  AS reasoning_tokens,
                       coalesce(sum(duration_ms), 0)       AS duration_ms,
                       count(*) FILTER (WHERE NOT ok)      AS failed
                  FROM llm_call {where}""", params)
        by_role = await self.fetchall(
            f"""SELECT role, count(*) AS calls,
                       coalesce(sum(prompt_tokens), 0)     AS prompt_tokens,
                       coalesce(sum(completion_tokens), 0) AS completion_tokens,
                       coalesce(sum(total_tokens), 0)      AS total_tokens,
                       coalesce(sum(duration_ms), 0)       AS duration_ms
                  FROM llm_call {where}
                 GROUP BY role ORDER BY total_tokens DESC""", params)
        by_stage = await self.fetchall(
            f"""SELECT role, stage, count(*) AS calls,
                       coalesce(sum(total_tokens), 0)      AS total_tokens
                  FROM llm_call {where}
                 GROUP BY role, stage ORDER BY total_tokens DESC""", params)
        by_model = await self.fetchall(
            f"""SELECT coalesce(model, '（未知）') AS model, count(*) AS calls,
                       coalesce(sum(total_tokens), 0)      AS total_tokens
                  FROM llm_call {where}
                 GROUP BY model ORDER BY total_tokens DESC""", params)
        return {"total": total or {}, "by_role": by_role, "by_stage": by_stage,
                "by_model": by_model}

    async def usage_by_conversation(self, limit: int = 30) -> list[dict]:
        """按会话聚合的消耗 —— 「消耗清单」看整体时用。"""
        return await self.fetchall(
            """SELECT r.conversation_id,
                      min(r.question)              AS question,
                      count(DISTINCT r.global_task_id) AS runs,
                      count(c.call_id)             AS calls,
                      coalesce(sum(c.total_tokens), 0)      AS total_tokens,
                      coalesce(sum(c.prompt_tokens), 0)     AS prompt_tokens,
                      coalesce(sum(c.completion_tokens), 0) AS completion_tokens,
                      max(c.created_at)            AS last_at
                 FROM agent_run r JOIN llm_call c ON c.global_task_id = r.global_task_id
                GROUP BY r.conversation_id
                ORDER BY max(c.created_at) DESC
                LIMIT %s""", (limit,))

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
