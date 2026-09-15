-- ============================================================
-- 系统表 · 建在 agent_sql 库（项目自身记录）
--   只有应用自己的 postgres 连接用；**不授权**给 agent_sql_runner
-- ============================================================

-- ① 全局任务
CREATE TABLE IF NOT EXISTS agent_run (
    global_task_id UUID        PRIMARY KEY,
    question       TEXT        NOT NULL,
    status         TEXT        NOT NULL,   -- created/planning/generating/validating/confirming/
                                           -- executing/fixing/reviewing/done/failed
    round          INT         NOT NULL DEFAULT 0,   -- 修正轮次
    version        INT         NOT NULL DEFAULT 0,   -- 乐观锁
    max_rounds     INT         NOT NULL DEFAULT 3,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ② 智能体执行记录（租约 / 幂等 / 尝试次数）
CREATE TABLE IF NOT EXISTS agent_task (
    global_task_id  UUID        NOT NULL REFERENCES agent_run(global_task_id) ON DELETE CASCADE,
    sub_task_id     TEXT        NOT NULL,
    attempt         INT         NOT NULL DEFAULT 1,
    role            TEXT        NOT NULL,   -- planner/generator/validator/executor/fixer/reviewer
    agent_id        TEXT        NOT NULL,
    status          TEXT        NOT NULL,   -- pending/running/completed/failed/unknown
    version         INT         NOT NULL DEFAULT 0,
    lease_until     TIMESTAMPTZ,
    input_context   JSONB       NOT NULL,
    result_ref      TEXT,
    idempotency_key TEXT        UNIQUE,
    error           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (global_task_id, sub_task_id, attempt)
);
CREATE INDEX IF NOT EXISTS idx_task_run ON agent_task(global_task_id, created_at);

-- ③ 事件账本（前端时间线的唯一数据源，SSE 可回放）
CREATE TABLE IF NOT EXISTS agent_event (
    seq            BIGSERIAL   PRIMARY KEY,
    global_task_id UUID        NOT NULL REFERENCES agent_run(global_task_id) ON DELETE CASCADE,
    role           TEXT        NOT NULL,
    event_type     TEXT        NOT NULL,   -- run_created/node_start/node_end/handoff/plan/sql/
                                           -- verdict/check/await_confirm/confirmed/result/review/done/error
    version        INT         NOT NULL DEFAULT 0,
    round          INT         NOT NULL DEFAULT 0,
    payload        JSONB       NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_event_run ON agent_event(global_task_id, seq);

-- ④ SQL 审计：每条 SQL 从生成到执行的全程留痕
CREATE TABLE IF NOT EXISTS sql_audit (
    sql_id         BIGSERIAL   PRIMARY KEY,
    global_task_id UUID        NOT NULL REFERENCES agent_run(global_task_id) ON DELETE CASCADE,
    round          INT         NOT NULL DEFAULT 0,
    sub_task_id    TEXT        NOT NULL,
    stage          TEXT        NOT NULL,   -- generated/validated/rejected/await_confirm/
                                           -- confirmed/executed/failed
    sql_text       TEXT        NOT NULL,
    sql_hash       TEXT        NOT NULL,
    intent         TEXT,
    risk_level     TEXT,                   -- 只读/写入/需确认/禁止
    action         TEXT,                   -- SELECT/INSERT/UPDATE/DELETE
    tables         JSONB       NOT NULL DEFAULT '[]'::jsonb,
    est_rows       BIGINT,
    affected_rows  BIGINT,
    need_confirm   BOOLEAN     NOT NULL DEFAULT false,
    confirmed_by   TEXT,
    confirmed_at   TIMESTAMPTZ,
    error          TEXT,
    duration_ms    INT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_audit_run ON sql_audit(global_task_id, sql_id);
