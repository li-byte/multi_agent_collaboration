-- ============================================================
-- 通用多 Agent 协作 · 建表脚本
--   用户提问 + 用户提供的资料  →  四个智能体协作  →  返回答案
--   没有「快照 / 版本 / 时间窗」——「同一份事实」由「引用必须逐字命中资料原文」保证
-- ============================================================

-- ---------- ① 全局任务 ----------
CREATE TABLE IF NOT EXISTS agent_run (
    global_task_id UUID        PRIMARY KEY,
    question       TEXT        NOT NULL,          -- 用户问的问题
    status         TEXT        NOT NULL,          -- created/planning/researching/answering/reviewing/done/failed
    round          INT         NOT NULL DEFAULT 0,
    version        INT         NOT NULL DEFAULT 0,   -- 乐观锁
    chunk_count    INT         NOT NULL DEFAULT 0,   -- 资料切成了多少段
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------- ② 权威事实层：用户提供的资料片段（只能引用，不能改写）----------
--    chunk_id 形如 D1-2（第 1 份资料的第 2 段），只在**本次任务内**唯一，
--    所以主键是 (global_task_id, chunk_id) —— 否则第二次提问就会撞主键插不进去。
CREATE TABLE IF NOT EXISTS source_chunk (
    global_task_id UUID        NOT NULL REFERENCES agent_run(global_task_id) ON DELETE CASCADE,
    chunk_id       TEXT        NOT NULL,          -- D1-1 = 第 1 份资料的第 1 段
    doc_no         INT         NOT NULL,          -- 第几份资料
    seq            INT         NOT NULL,          -- 该资料的第几段
    content        TEXT        NOT NULL,          -- 原文
    char_len       INT         NOT NULL DEFAULT 0,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (global_task_id, chunk_id)
);
CREATE INDEX IF NOT EXISTS idx_chunk_run ON source_chunk(global_task_id, doc_no, seq);

-- ---------- ③ 子任务 / 角色执行记录 ----------
CREATE TABLE IF NOT EXISTS agent_task (
    global_task_id  UUID        NOT NULL REFERENCES agent_run(global_task_id) ON DELETE CASCADE,
    sub_task_id     TEXT        NOT NULL,
    attempt         INT         NOT NULL DEFAULT 1,
    role            TEXT        NOT NULL,         -- planner/researcher/executor/reviewer
    agent_id        TEXT        NOT NULL,
    status          TEXT        NOT NULL,         -- pending/running/completed/failed/unknown
    version         INT         NOT NULL DEFAULT 0,
    lease_until     TIMESTAMPTZ,                  -- 租约
    input_context   JSONB       NOT NULL,         -- 本次执行基于的上下文（问题 + 可见资料片段）
    citations       JSONB       NOT NULL DEFAULT '[]'::jsonb,
    result_ref      TEXT,
    idempotency_key TEXT        UNIQUE,
    error           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (global_task_id, sub_task_id, attempt)
);
CREATE INDEX IF NOT EXISTS idx_task_run ON agent_task(global_task_id, created_at);

-- ---------- ④ 结论：候选 / 已验证 / 冲突 / 已否决 ----------
CREATE TABLE IF NOT EXISTS finding (
    finding_id     TEXT        PRIMARY KEY,
    global_task_id UUID        NOT NULL REFERENCES agent_run(global_task_id) ON DELETE CASCADE,
    sub_task_id    TEXT        NOT NULL,          -- 对应规划器拆出的哪个子问题
    round          INT         NOT NULL DEFAULT 0,
    claim          TEXT        NOT NULL,
    status         TEXT        NOT NULL,
    citations      JSONB       NOT NULL DEFAULT '[]'::jsonb,   -- [{source_id, quote}]
    insufficient   BOOLEAN     NOT NULL DEFAULT false,         -- 资料里确实没有相关内容
    assumptions    JSONB       NOT NULL DEFAULT '[]'::jsonb,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_finding_run ON finding(global_task_id, created_at);

-- ---------- ⑤ 事件账本：前端时间线的唯一数据源 ----------
CREATE TABLE IF NOT EXISTS agent_event (
    seq            BIGSERIAL   PRIMARY KEY,
    global_task_id UUID        NOT NULL REFERENCES agent_run(global_task_id) ON DELETE CASCADE,
    role           TEXT        NOT NULL,
    event_type     TEXT        NOT NULL,          -- run_created/node_start/node_end/handoff/finding/answer/consistency/review/done/error
    version        INT         NOT NULL DEFAULT 0,
    round          INT         NOT NULL DEFAULT 0,
    citations      JSONB       NOT NULL DEFAULT '[]'::jsonb,
    payload        JSONB       NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_event_run ON agent_event(global_task_id, seq);
