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

-- ①b 多轮对话：一次提问 = 一轮；同一个会话里的多轮共享 conversation_id
--     用 ADD COLUMN IF NOT EXISTS，对已有库是幂等的（不重建表、不丢数据）
ALTER TABLE agent_run ADD COLUMN IF NOT EXISTS conversation_id TEXT;
ALTER TABLE agent_run ADD COLUMN IF NOT EXISTS turn INT NOT NULL DEFAULT 1;
CREATE INDEX IF NOT EXISTS idx_agent_run_conv ON agent_run(conversation_id, turn);


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

-- ⑤ 共享记忆：子任务**执行成功后**写入的「带来源的事实」，供后续子任务引用
--
-- 写入顺序严格按原始文档的标准：
--     带来源的候选主张 → 经检查 → 可复用事实层
--
-- 「经检查」在这里有确切含义：**verified 只能由「执行器真的执行过」置位**，
-- 模型说的不算。所以它记的是事实，不是传闻。
--
-- 「不能取代权威数据源」也落到了字段上：sql_id + sql_text 是来源，
-- 要核对就回到 sql_audit 去看原语句与执行结果。
CREATE TABLE IF NOT EXISTS agent_memory (
    mem_id         BIGSERIAL   PRIMARY KEY,
    global_task_id UUID        NOT NULL REFERENCES agent_run(global_task_id) ON DELETE CASCADE,
    sub_task_id    TEXT        NOT NULL,
    claim          TEXT        NOT NULL,   -- 候选主张（一句话）
    sql_id         BIGINT,                 -- 来源：sql_audit.sql_id
    sql_text       TEXT        NOT NULL,   -- 来源原文（可直接核对）
    action         TEXT,                   -- SELECT / INSERT / UPDATE / DELETE
    stage          TEXT        NOT NULL,   -- 只有 executed 才允许 verified=true
    verified       BOOLEAN     NOT NULL DEFAULT false,
    entities       JSONB       NOT NULL DEFAULT '{}'::jsonb,  -- {table, key, values}
    columns        JSONB       NOT NULL DEFAULT '[]'::jsonb,
    rows           JSONB       NOT NULL DEFAULT '[]'::jsonb,  -- 关键行（截断）
    rowcount       BIGINT,
    round          INT         NOT NULL DEFAULT 0,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- 一个子任务一条事实；重跑（修正后）就覆盖它，避免留下过期版本
    UNIQUE (global_task_id, sub_task_id)
);
CREATE INDEX IF NOT EXISTS idx_memory_run ON agent_memory(global_task_id, sub_task_id);


-- ================================================================
-- 大模型消耗明细（llm_call）
--
-- 每一次大模型调用落一条。**token 用量是响应里带回来的真数，不是按字符数估算的** ——
-- 估算出来的数字没法用来对账，而「消耗清单」如果不能对账就只是个装饰。
--
-- 一次调用 = 一个角色 + 一件事（role + stage）：
--   planner  复查
--   generator 选表 / 生成 SQL
--   validator 校验 SQL
--   ...
-- 所以按 role / stage 分组就能看出「钱花在哪一步」。
--
-- `cached_tokens` 是提示词缓存命中的部分（DeepSeek 会返回），
-- 它和 `prompt_tokens` 是包含关系：命中越多越便宜，所以要分开记。
CREATE TABLE IF NOT EXISTS llm_call (
    call_id           BIGSERIAL   PRIMARY KEY,
    global_task_id    UUID        NOT NULL REFERENCES agent_run(global_task_id) ON DELETE CASCADE,
    role              TEXT        NOT NULL,
    stage             TEXT        NOT NULL DEFAULT '',   -- 这次调用在干什么
    round             INT         NOT NULL DEFAULT 0,
    cursor            INT         NOT NULL DEFAULT 0,    -- 第几个子任务
    attempt           INT         NOT NULL DEFAULT 1,    -- 契约校验失败后的第几次重试
    model             TEXT,
    prompt_tokens     INT         NOT NULL DEFAULT 0,
    completion_tokens INT         NOT NULL DEFAULT 0,
    total_tokens      INT         NOT NULL DEFAULT 0,
    cached_tokens     INT         NOT NULL DEFAULT 0,
    reasoning_tokens  INT         NOT NULL DEFAULT 0,
    duration_ms       INT,
    ok                BOOLEAN     NOT NULL DEFAULT true,
    error             TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_llm_call_run ON llm_call(global_task_id, call_id);
CREATE INDEX IF NOT EXISTS idx_llm_call_time ON llm_call(created_at DESC);


