# 落地实现指南 · 用 LangGraph 实现《多智能体一致性标准》

> 目标：把标准里的 **14 个机制（M1~M14）** 与 **12 条判据（A1~A12）** 落成可运行的系统。
> 载体示例：**LangGraph**（负责控制流）+ **一致性账本层**（本文主角）+ **PostgreSQL / 内存**（存储）。
> 本文所有 LangGraph 接口均在本机实测（langgraph 1.2.11、langgraph-checkpoint-postgres 3.1.2、psycopg 3.3.5、Python 3.10）。

---

## 1. 总体架构：三层，各管一件事

```
┌────────────────────────────────────────────────────────────┐
│ 业务层：节点（取数 / 推理 / 写结论 / 审校 / 发布）             │
│   只做业务，不管理状态机                                      │
├────────────────────────────────────────────────────────────┤
│ 图（LangGraph）：控制流                                      │
│   节点与边、条件路由、并行扇出(Send)、图级 checkpoint、         │
│   interrupt 人工审批、retry_policy                           │
├────────────────────────────────────────────────────────────┤
│ 一致性账本层（本文实现的库）                                   │
│   任务与承诺、事实版本、两态步骤、副作用三态、                 │
│   结论与协商、交叉复核、求援、交接、可见性视图                  │
├────────────────────────────────────────────────────────────┤
│ 存储：PostgreSQL（生产） / 内存（单进程开发）                  │
└────────────────────────────────────────────────────────────┘
```

### 三条必须写进代码的原则

| 原则 | 含义 | 对应机制 |
|---|---|---|
| **账本先行、图后行** | 进节点先写 `started`，节点成功先写 `committed`，再让图落 checkpoint | M7、M8 |
| **图管"跑到哪"，账本管"算不算发生过"** | 两者不重叠、不互相同步状态机；恢复时由账本提供事实、由图决定动作 | M7 |
| **显式优于隐式** | 认领、写事实、副作用占位必须由节点显式调用；不做"自动推导" | M1、M6、M10 |

---

## 2. 机制 → 实现落点 总映射

| 机制 | 主要实现落点 | 关键约束 | 判据 |
|---|---|---|---|
| M1 单一事实源 + 版本化 | 表 `collab_fact` + `ctx.fact()` / `ctx.assumption()` | `PK(run_id,fact_key,version)`；`based_on` 必须指向未失效版本 | A1、A2 |
| M2 唯一身份 + 认领 | 表 `collab_task` + `claim()`（CAS + 租约） | 单条条件 UPDATE，影响行数=1 才算抢到 | A1、A3 |
| M3 结构化交接契约 | 表 `collab_handoff` + `ctx.handoff()` | 四件套字段缺一不可 | A2、A4 |
| M4 结论收敛规则 | 表 `collab_decision` + 可比性判定 | 不可比 → `kept_conflict`，禁止强行合并 | A5 |
| M5 完成判据前置 | `collab_task.acceptance` + 注册 checker | 建任务时写入，之后不可修改 | A6 |
| M6 副作用账本 | 表 `collab_effect` + `reserve/commit/revoke` | `PK(run_id,idempotency_key)`；`committed` 必须有 evidence | A3 |
| M7 两态留痕 | 表 `collab_step` + 节点包装器 | `UNIQUE(...,phase)`；未闭合即 torn step | A3、A4 |
| M8 留痕与回放 | 视图 `v_timeline` | 按 `step_seq` 串联因果 | A2 |
| M9 能力与角色声明 | 表/配置 `agent_spec` | 分工决策必须能回答"为什么是它" | A7 |
| M10 任务承诺 | `collab_task.status` + `accept/decline` | `accepted` 与 `owner` 必须同时存在 | A8 |
| M11 求援通道 | 表 `collab_help` | 必须有 `to_agent`、`due_at`、`answered_by` | A9 |
| M12 交叉复核 | 表 `collab_review` | `CHECK (reviewer <> producer)` 由数据库强制 | A10 |
| M13 协商共识 | `collab_decision.outcome` + 协商记录 | 禁止"压制分歧"路径 | A11 |
| M14 协作可见性 | 视图 `v_presence` | 任一时刻可查"谁在做、做到哪、缺什么" | A12 |

---

## 3. 存储层：8 张表 + 2 张视图

### 3.1 DDL（PostgreSQL，可直接执行）

```sql
-- ① 运行（一次用户请求）
create table if not exists collab_run (
  run_id      text primary key,
  goal        text not null default '',
  status      text not null default 'running',   -- running|done|failed|cancelled
  meta        jsonb not null default '{}'::jsonb,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

-- ② 任务与承诺（M2 唯一身份 + M10 承诺 + M5 完成判据）
create table if not exists collab_task (
  run_id          text not null,
  sub_task_id     text not null,
  parent_id       text,
  goal            text not null default '',
  required_skills jsonb not null default '[]'::jsonb,   -- M9：需要什么能力
  status          text not null default 'pending',      -- pending|offered|accepted|running|committed|failed|declined
  owner_agent     text,
  accepted_at     timestamptz,
  lease_until     timestamptz,
  attempt         int    not null default 0,
  version         bigint not null default 0,            -- 乐观锁
  acceptance      jsonb  not null default '[]'::jsonb,  -- M5：开始前写死、可机检
  result_ref      text,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),
  primary key (run_id, sub_task_id),
  check (status <> 'accepted' or owner_agent is not null)   -- M10：接受者必须存在
);
create index if not exists ix_task_lease on collab_task (status, lease_until);

-- ③ 事实（M1 单一事实源 + 版本 + 假设隔离）
create table if not exists collab_fact (
  run_id        text not null,
  fact_key      text not null,
  version       int  not null,
  kind          text not null default 'observation',  -- observation|derivation|assumption|decision
  value         jsonb,
  source_type   text not null default 'model',        -- authoritative|model|human
  produced_by   text not null,                        -- 哪个 agent 写的
  step_seq      bigint,
  based_on      jsonb not null default '[]'::jsonb,   -- [{"key":..,"version":..}]
  superseded_by int,
  created_at    timestamptz not null default now(),
  primary key (run_id, fact_key, version),
  check (jsonb_typeof(based_on) = 'array')
);
create index if not exists ix_fact_live on collab_fact (run_id, fact_key) where superseded_by is null;

-- ④ 步骤两态留痕（M7）
create table if not exists collab_step (
  step_id     bigserial primary key,
  run_id      text not null,
  sub_task_id text not null,
  step_seq    bigint not null,
  attempt     int not null default 1,
  agent       text not null,
  phase       text not null,                          -- started|committed|failed
  detail      jsonb not null default '{}'::jsonb,
  ts          timestamptz not null default now(),
  unique (run_id, sub_task_id, step_seq, attempt, phase)
);

-- ⑤ 副作用账本（M6）
create table if not exists collab_effect (
  run_id          text not null,
  idempotency_key text not null,
  action          text not null,
  target          text not null default '',
  status          text not null default 'reserved',   -- reserved|executing|succeeded|failed|unknown|committed|revoked
  evidence        jsonb not null default '{}'::jsonb,
  reserved_at     timestamptz not null default now(),
  settled_at      timestamptz,
  lease_until     timestamptz,
  primary key (run_id, idempotency_key),
  check (status <> 'committed' or evidence <> '{}'::jsonb)   -- 结账必须有证据
);

-- ⑥ 结论与协商（M4 收敛 + M13 协商）
create table if not exists collab_decision (
  run_id      text not null,
  decision_id text not null,
  sub_task_id text not null,
  claim       text not null,
  based_on    jsonb not null default '[]'::jsonb,
  comparable  boolean,
  outcome     text not null default 'candidate',      -- candidate|conflict|converged|kept_conflict
  rationale   text not null default '',
  created_at  timestamptz not null default now(),
  primary key (run_id, decision_id)
);

-- ⑦ 交叉复核（M12，数据库强制禁止自评）
create table if not exists collab_review (
  run_id     text not null,
  review_id  text not null,
  sub_task_id text not null,
  target_ref text not null,
  reviewer   text not null,
  producer   text not null,
  verdict    text not null,                           -- pass|challenge|reject
  objections jsonb not null default '[]'::jsonb,
  resolution text not null default '',
  created_at timestamptz not null default now(),
  primary key (run_id, review_id),
  check (reviewer <> producer)                        -- M12：不得自评
);

-- ⑧ 求援与应答（M11）
create table if not exists collab_help (
  run_id      text not null,
  help_id     text not null,
  sub_task_id text not null,
  asker       text not null,
  need        text not null,
  to_agent    text,
  due_at      timestamptz,
  answered_by text,
  answer      text,
  status      text not null default 'open',           -- open|answered|expired
  created_at  timestamptz not null default now(),
  primary key (run_id, help_id),
  check (status <> 'answered' or (answered_by is not null and answer is not null))
);
create index if not exists ix_help_open on collab_help (run_id, status, due_at);

-- ⑨ 交接契约（M3 四件套）
create table if not exists collab_handoff (
  run_id      text not null,
  handoff_id  text not null,
  sub_task_id text not null,
  from_agent  text not null,
  to_agent    text not null,
  based_on    jsonb not null default '[]'::jsonb,   -- 我基于什么
  produced    jsonb not null default '[]'::jsonb,   -- 我做了什么
  left_open   jsonb not null default '[]'::jsonb,   -- 我留了什么
  need_next   jsonb not null default '[]'::jsonb,   -- 你还需要什么
  created_at  timestamptz not null default now(),
  primary key (run_id, handoff_id),
  check (jsonb_array_length(need_next) > 0)         -- 没有验收要求的交接不算交接
);
```

### 3.2 约束就是机制：为什么这些 CHECK / PK 值得写

| 约束 | 它替你守住的机制 |
|---|---|
| `PK(run_id, fact_key, version)` | M1：同 key 多版本共存，旧版本不被覆盖 |
| `PK(run_id, idempotency_key)` | M6：副作用不可能被记两次 |
| `UNIQUE(run_id, sub_task_id, step_seq, attempt, phase)` | M7：重复写同一相位即报错，暴露重跑 |
| `CHECK (reviewer <> producer)` | M12：**自评在数据库层就写不进去** |
| `CHECK (status <> 'accepted' or owner_agent is not null)` | M10：认领与负责人必须同时存在 |
| `CHECK (status <> 'committed' or evidence <> '{}')` | M6：没有证据的结账直接被拒 |
| `CHECK (jsonb_array_length(need_next) > 0)` | M3：空交接无效 |

> 经验：**能由数据库约束表达的机制，就不要只写成流程约定**——流程约定会被人绕过，约束不会。

### 3.3 两张视图（M14 可见性、M8 回放）

```sql
-- M14：任一时刻"谁在做、做到哪、缺什么"
create or replace view v_presence as
select t.run_id, t.sub_task_id, t.owner_agent, t.status, t.lease_until,
       (select max(s.step_seq) from collab_step s
         where s.run_id = t.run_id and s.sub_task_id = t.sub_task_id and s.phase = 'committed') as last_committed_step,
       (select count(*) from collab_help h
         where h.run_id = t.run_id and h.sub_task_id = t.sub_task_id and h.status = 'open')      as open_helps,
       (select count(*) from collab_review r
         where r.run_id = t.run_id and r.sub_task_id = t.sub_task_id and r.verdict = 'challenge') as open_challenges
from collab_task t;

-- M8：回放时间线
create or replace view v_timeline as
select run_id, step_seq as seq, agent as actor, 'step' as kind, phase as detail, ts, sub_task_id
  from collab_step
union all
select run_id, step_seq, produced_by, 'fact', fact_key || '@v' || version, created_at, null
  from collab_fact
union all
select run_id, step_seq, null, 'effect', action || ' ' || target || ' → ' || status, reserved_at, null
  from collab_effect
union all
select run_id, null, null, 'decision', claim || ' [' || outcome || ']', created_at, sub_task_id
  from collab_decision
union all
select run_id, null, reviewer, 'review', verdict, created_at, sub_task_id
  from collab_review
union all
select run_id, null, asker, 'help', need || ' → ' || coalesce(answered_by, '未应答'), created_at, sub_task_id
  from collab_help
order by 1, 2 nulls last, 6;
```

---

## 4. 一致性层的公共接口

### 4.1 构造

```python
collab = Collab(
    dsn=None,                  # None → 内存；给定 → PostgreSQL
    prefix="",                 # 可选：多套账本共库时的表前缀
    lease_seconds=60,          # 认领租约
    write_closed=True,         # 写路径 fail-closed
)
```

### 4.2 调用清单

| 调用 | 用途 | 写库 | 机制 |
|---|---|---|---|
| `collab.create_task(run_id, sub_task_id, goal, skills, acceptance)` | 建任务并写死判据 | 1 | M5、M9 |
| `collab.offer / accept / decline(sub_task_id, agent)` | 承诺流程 | 1/次 | M10 |
| `collab.claim(run_id, sub_task_id, agent)` | CAS 抢单（唯一 + 租约） | 1 | M2 |
| `with collab.step(run_id, sub_task_id, agent, seq) as ctx` | 两态步骤 | 2 | M7 |
| `ctx.fact(key, value, kind, source_type, based_on)` | 写事实 | 1 | M1 |
| `ctx.assumption(text)` | 写假设（`kind=assumption`） | 1 | M1 |
| `ctx.reserve(key, action, target)` | 副作用占位 | 1 | M6 |
| `ctx.commit(token, evidence)` | 结账（证据必填） | 1 | M6 |
| `ctx.revoke(key, reason)` | 纠错 | 1 | M6 |
| `collab.verify(run_id, sub_task_id, based_on)` | 版本闭包校验（握手） | 只读 | M1、M3 |
| `collab.handoff(...)` | 写四件套交接 | 1 | M3 |
| `collab.propose(claim, based_on)` | 提结论 + 可比性判定 | 1–2 | M4 |
| `collab.negotiate(decision_id, rationale, outcome)` | 协商与保留分歧 | 1 | M13 |
| `collab.review(target_ref, reviewer, producer, verdict, objections)` | 交叉复核 | 1 | M12 |
| `collab.ask(need, to_agent, due_in)` / `collab.answer(help_id, by, text)` | 求援与应答 | 1/次 | M11 |
| `collab.timeline(run_id)` / `collab.presence(run_id)` | 回放 / 可见性 | 只读 | M8、M14 |

### 4.3 同步 / 异步双入口（必须）

图既可能 `invoke` 也可能 `ainvoke`，因此每个写操作都要有异步版本（`astep`、`aclaim`…），或统一走线程池桥。**否则一次 `ainvoke` 会被同步数据库调用阻塞事件循环。**

---

## 5. 与 LangGraph 的集成（核心章节）

### 5.1 三种接入方式对比

| 方式 | 写法 | 侵入性 | 评价 |
|---|---|---|---|
| ① **节点包装器**（推荐） | `add_node("x", collab_node(collab, "x")(fn))` | 低，节点内部零改动 | 自动两态、自动 attempt、自动异常留痕 |
| ② 子类化 StateGraph | `class CollabGraph(StateGraph)`，覆写 `add_node` | 中，依赖内部结构 | 版本升级风险高，不建议 |
| ③ 节点内手写调用 | 每个节点自己 `collab.begin/commit` | 高，易漏 | 只在特殊情况使用 |

### 5.2 节点包装器（方式①，落 M7 + M6 异常路径）

```python
import functools

def collab_node(collab, agent: str, step_of=None):
    """给任意 LangGraph 节点套上两阶段留痕与异常留痕。"""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(state, *args, **kwargs):
            run_id, sub_task_id = state["run_id"], state["sub_task_id"]
            step_seq = step_of(state) if step_of else state.get("step_seq", 0)

            # ① 账本先行：attempt 由账本维护（不用 LangGraph 内部重试计数）
            ctx = collab.begin(run_id, sub_task_id, agent, step_seq)

            try:
                out = fn(state, *args, **kwargs)          # ② 执行业务
            except Exception as exc:
                ctx.fail(exc)                             # 写 failed：崩溃点不留盲区
                raise
            ctx.commit(detail=out)                        # ③ 写 committed
            return out
        return wrapper
    return deco


# 使用
builder.add_node("fetch", collab_node(collab, "fetcher", step_of=lambda s: 1)(fetch))
builder.add_node("write", collab_node(collab, "writer",  step_of=lambda s: 2)(write))
```

**重要实践**：`attempt` 由**账本**递增（`begin()` 内部读上次 attempt +1），**不要**依赖 LangGraph 的重试计数——它不直接暴露在 state 里，且图级重跑与节点级重试是两套计数。

### 5.3 State 定义与 reducer（并行时不互相覆盖）

```python
import operator
from typing import Annotated, TypedDict

class CollabState(TypedDict, total=False):
    run_id: str
    sub_task_id: str
    step_seq: int
    attempt: int
    findings: Annotated[list, operator.add]     # 并行节点累加，避免覆盖
    fact_refs: Annotated[list, operator.add]
    errors: Annotated[list, operator.add]
```

### 5.4 条件路由：路由权在使用者手里

账本提供判断材料，路由函数由使用者实现（这对应标准里"路由不是一致性层的职责"）：

```python
def route(state, config):
    # 账本不替你做决定，只给你材料
    if state.get("interrupted_step"):
        return "retry" if state.get("retry_budget", 0) > 0 else "abort"
    if state.get("open_helps"):
        return "await_help"
    if state.get("challenges"):
        return "revise"
    return "next"

builder.add_conditional_edges("check", route, {"retry": "fetch", "revise": "write",
                                               "await_help": "await", "abort": END, "next": "publish"})
```

### 5.5 并行扇出：Send + 独立子任务

```python
from langgraph.types import Send

def fanout(state):
    return [Send("worker", {"run_id": state["run_id"],
                            "sub_task_id": f"{state['sub_task_id']}/{i}",   # 独立身份 → M2
                            "attempt": 1})
            for i in range(len(state["shards"]))]

builder.add_conditional_edges("planner", fanout, ["worker"])
```

要点：**每个并行分支必须有独立 `sub_task_id`**，事实 key 也要带分支前缀，否则并发写同一 key 会互相覆盖（M1 失效）。

### 5.6 checkpoint 与账本的写序 + 恢复握手

```python
from langgraph.checkpoint.postgres import PostgresSaver

with PostgresSaver.from_conn_string(dsn) as saver:
    saver.setup()
    graph = builder.compile(checkpointer=saver)
    cfg = {"configurable": {"thread_id": run_id}}      # run_id 必须等于 thread_id
    graph.invoke(init_state, cfg)
```

写序（`superstep` 边界天然在节点之后，与"账本先行"一致）：

```
① 节点入口 → 账本写 started
② 节点执行
③ 节点出口 → 账本写 committed
④ LangGraph 写 checkpoint
```

恢复握手（**只读账本，不争恢复权威**）：

```python
def torn_guard(collab):
    def node(state):
        torn = collab.torn_step(state["run_id"], state["sub_task_id"])
        if torn:
            # 账本只报告事实；重跑还是终止由图/使用者决定
            return {"interrupted_step": torn, "attempt": torn["attempt"] + 1}
        return {}
    return node

builder.add_node("guard", torn_guard(collab))
builder.add_edge(START, "guard")
builder.add_conditional_edges("guard", lambda s: "retry" if s.get("interrupted_step") else "go",
                              {"retry": "fetch", "go": "fetch"})
```

### 5.7 人工审批：用 LangGraph 的 interrupt（M6 / M13）

```python
from langgraph.types import interrupt, Command

def publish(state, config):
    decision = interrupt({"ask": "确认发布？", "effect": state["pending_effect"]})
    if not decision.get("approve"):
        return {"status": "cancelled"}
    tok = collab.reserve(state["run_id"], state["pending_effect"]["key"], "publish", state["target"])
    ...
    collab.commit(tok, evidence=result)

# 恢复
graph.invoke(Command(resume={"approve": True}), config)
```

要点：**审批留在 LangGraph，账本只记录"审批已发生"**；没有审批记录时，账本拒绝把高危副作用置为 `committed`。

### 5.8 重跑与重试：attempt 进幂等键

```python
from langgraph.types import RetryPolicy

builder.add_node("fetch",
                 collab_node(collab, "fetcher", step_of=lambda s: 1)(fetch),
                 retry_policy=RetryPolicy(max_attempts=3))     # 实测该参数存在
```

**注意**：`retry_policy` 会让同一节点被重跑多次。因此所有唯一键都必须包含 `attempt`，而副作用键必须**不含** attempt（要跨 attempt 去重）：

| 对象 | 唯一键是否含 attempt | 原因 |
|---|---|---|
| 步骤留痕 `collab_step` | **含** | 区分"第几次执行" |
| 事实 `collab_fact` | 不含（用 version 递增） | 事实是版本化的，不是尝试化的 |
| 副作用 `collab_effect` | **不含** | 要求跨重试只执行一次 |

### 5.9 最小可运行骨架（3 节点：取数 → 写结论 → 审校）

```python
from typing import TypedDict, Annotated
import operator
from langgraph.graph import StateGraph, START, END

class S(TypedDict, total=False):
    run_id: str
    sub_task_id: str
    step_seq: int
    facts: Annotated[list, operator.add]
    report: str
    review: dict

def fetch(state, config):
    ctx = collab.current(state)                       # 由包装器注入
    ctx.fact("raw.metrics", {"revenue": 123}, kind="observation", source_type="authoritative")
    ctx.fact("raw.note", "渠道口径待确认", kind="assumption")     # 假设与事实分开
    return {"facts": ["raw.metrics", "raw.note"]}

def write(state, config):
    ctx = collab.current(state)
    ctx.verify_based_on([("raw.metrics", 1)])         # 版本闭包校验（握手）
    with ctx.help(need="2024 年渠道口径定义", to_agent="data_expert", due_in=300) as h:
        if h.answered:
            ctx.fact("channel.caliber", h.answer, source_type="human")
    ctx.fact("report.draft", {"text": "..."}, kind="derivation",
             based_on=[("raw.metrics", 1)])
    return {"report": "draft@v1"}

def review(state, config):
    ctx = collab.current(state)
    ctx.review(target_ref="report.draft@v1", reviewer="reviewer", producer="writer",
               verdict="pass", objections=[])
    return {"review": {"verdict": "pass"}}

b = StateGraph(S)
b.add_node("fetch",  collab_node(collab, "fetcher",  step_of=lambda s: 1)(fetch))
b.add_node("write",  collab_node(collab, "writer",   step_of=lambda s: 2)(write))
b.add_node("review", collab_node(collab, "reviewer", step_of=lambda s: 3)(review))
b.add_edge(START, "fetch"); b.add_edge("fetch", "write"); b.add_edge("write", "review"); b.add_edge("review", END)
graph = b.compile(checkpointer=saver)

graph.invoke({"run_id": "r-001", "sub_task_id": "t-1"}, {"configurable": {"thread_id": "r-001"}})
```

跑完之后，`q 5` 查 `v_timeline`，就能看到事实、假设、求援、复核、步骤的完整时间线——这就是 A2 的答案。

---

## 6. 逐机制实现要点（M1~M14）

| 机制 | 具体怎么做 | 落到哪里 | 常见坑 |
|---|---|---|---|
| **M1** 单一事实源 | 节点只通过 `ctx.fact()` 写；`fact_key` 全局唯一；写新版本时把旧版本 `superseded_by` 置为新版本号；`assumption` 用独立 `kind` | `collab_fact` | 用"共享 state 列表"当事实源——它无版本、无来源，且并行时会被 reducer 合并掉 |
| **M2** 唯一身份 + 认领 | `claim` 用单条条件 UPDATE（`WHERE version=? AND (status IN (...) OR lease_until<now())`），`RETURNING version`；0 行 = 没抢到 | `collab_task` | 先 SELECT 再 UPDATE（读后写）→ 并发下必双跑 |
| **M3** 交接契约 | 交接必须写四件套，且 `need_next` 非空；下游入口用 `verify` 校验 `based_on` | `collab_handoff` | 用 checkpoint 里的 message 列表当交接，下游无法校验版本 |
| **M4** 收敛规则 | `propose()` 时比对已有结论的 `based_on` 集合：一致 → `converged`；不一致 → `kept_conflict` 并记录理由 | `collab_decision` | 用"取最新/投票"代替依据比对 → 假收敛 |
| **M5** 判据前置 | 建任务时写入 `acceptance`（可机检表达），并由注册的 checker 执行；**建后不允许修改** | `collab_task.acceptance` | 判据写成自然语言，最后仍需人判断 |
| **M6** 副作用账本 | `reserve` = `INSERT ... ON CONFLICT DO NOTHING RETURNING`；0 行则读既有状态返回给节点；`commit` 必须带 evidence；不确定 → `unknown` + `reconcile` 回调 | `collab_effect` | 只在"成功后"记日志，失败与超时无痕迹 |
| **M7** 两态留痕 | 包装器写 `started`/`committed`/`failed`；`resume` 前查未闭合步骤 | `collab_step` | 只在成功时记录 → 崩溃点成为盲区 |
| **M8** 回放 | 用 `v_timeline` 视图串起 step/fact/effect/decision/review/help，按 `step_seq` 排序 | 视图 | 日志分散在多处且无 `step_seq`，拼不出因果 |
| **M9** 能力声明 | 每个 Agent 一份 `AgentSpec(skills, cannot, depends_on)`；分工决策写入 `collab_decision.rationale` | 配置 + 决策表 | 把"模型通用能力"当成"角色分工" |
| **M10** 承诺 | `offer → accept/decline` 两步；`CHECK` 保证 `accepted` 必有 `owner`；接受带 `lease_until` | `collab_task` | 把"已通知"当"已认领" |
| **M11** 求援 | `ask(need, to_agent, due_in)` 必填去向与期限；`v_presence.open_helps` 暴露未应答；超期置 `expired` | `collab_help` | 只写一条"疑问"却不指定向谁问 |
| **M12** 交叉复核 | `review(target_ref, reviewer, producer, verdict, objections)`；`CHECK(reviewer<>producer)` 数据库强制；`challenge` 进入路由 | `collab_review` | 让产出者自己"再确认一遍" |
| **M13** 协商 | 分歧时先 `negotiate(decision_id, rationale)` 交换依据；`outcome` 只能是 `converged` 或 `kept_conflict`，无"搁置"选项 | `collab_decision` | 用"先往下走"绕过分歧 |
| **M14** 可见性 | `v_presence` 一条 SQL 给出：谁在做、最后提交到第几步、未应答求援数、未处理质疑数 | 视图 | 只在结束时汇报结果，过程黑箱 |

---

## 7. 验收：把 A1~A12 变成可执行的断言

判据不能靠"感觉做到了"，应能跑。下面每条给一个可执行检查（PG 为例）。

| 判据 | 检查方式（SQL / 断言） |
|---|---|
| **A1** 一份事实、一个状态 | `select count(*) from (select fact_key, count(*) from collab_fact where run_id=:r and superseded_by is null group by 1 having count(*)>1) x;` 结果应为 0 |
| **A2** 依据可追 | `select based_on from collab_fact where run_id=:r and fact_key=:k and version=:v;` 非空，且其引用的版本都存在且有效 |
| **A3** 动作不重复 | `select count(*) from (select idempotency_key from collab_effect where run_id=:r group by 1 having count(*)>1) y;` 为 0；且 `select count(*) from collab_effect where run_id=:r and status='committed' and evidence='{}'::jsonb;` 为 0 |
| **A4** 中断可接续 | 未闭合步骤数为 0：`select count(*) from collab_step s where s.run_id=:r and s.phase='started' and not exists (select 1 from collab_step c where c.run_id=s.run_id and c.sub_task_id=s.sub_task_id and c.step_seq=s.step_seq and c.attempt=s.attempt and c.phase='committed');` |
| **A5** 冲突有出路 | `select outcome, count(*) from collab_decision where run_id=:r group by 1;` 只允许 `converged` / `kept_conflict` / `candidate`；出现 `conflict` 未处理即不通过 |
| **A6** 完成可判定 | 任务 `acceptance` 非空，且所有条目有 checker 结论：`select count(*) from collab_task where run_id=:r and jsonb_array_length(acceptance)=0;` 为 0 |
| **A7** 能力与分工 | 每个 `owner_agent` 都能在 `agent_spec` 中找到且 `skills` 覆盖 `required_skills` |
| **A8** 承诺 | `select count(*) from collab_task where run_id=:r and status in ('running','committed') and owner_agent is null;` 为 0 |
| **A9** 求援有回音 | `select count(*) from collab_help where run_id=:r and status='open' and due_at < now();` 为 0（超期未答即不通过） |
| **A10** 独立复核 | `select count(*) from collab_review where run_id=:r and reviewer = producer;` 恒为 0（数据库约束保证） |
| **A11** 分歧显式协商 | 存在 `kept_conflict` 时，其目标产出必须被标记为"保留分歧"并出现在最终报告 |
| **A12** 可见性 | `select * from v_presence where run_id=:r and last_committed_step is null and status='running';` 为 0（运行中却没有进度 = 事实不可见） |

**建议**：把上面 12 条做成一个 `selfcheck` 命令，每次跑完图自动执行；**任何一条不通过就不允许宣布"完成"**——这正是 A6 的精神，只不过把判据用在了系统自己身上。

---

## 8. 分阶段落地路线

| 阶段 | 内容 | 覆盖机制 | 交付门槛 |
|---|---|---|---|
| **P1 单进程** | 内存账本 + 节点包装器 + 事实/假设 + 两态步骤 | M1、M3、M5、M7、M8（子集） | 单进程跑通，`v_timeline` 能重建 |
| **P2 持久化** | 切 PostgreSQL + 认领/承诺/租约 | + M2、M10、M6 | 双进程并发抢同一 `sub_task`，只有一个成功 |
| **P3 协作完备** | 复核、求援、协商、可见性 | + M4、M9、M11、M12、M13、M14 | A7~A12 全部可断言 |
| **P4 生产加固** | `reconcile` 对账、超期清理、归档、监控 | + M6 完整 | unknown 副作用可自动对账；账本可归档 |

**顺序不能颠倒**：P1 的"事实版本 + 两态步骤"是地基，P3 的协商与复核都建在它上面。

---

## 9. 运维与排错

### 9.1 常用查询

```sql
-- 现在有谁在做、做到哪、缺什么（M14）
select * from v_presence where run_id = :r;

-- 回放整件事（M8）
select * from v_timeline where run_id = :r order by seq nulls last;

-- 找未闭合步骤（M7 / A4）
select * from collab_step where run_id = :r and phase = 'started'
 except
select run_id, sub_task_id, step_seq, attempt, agent, 'committed', detail, ts
  from collab_step where run_id = :r and phase = 'committed';

-- 找未应答求援（M11 / A9）
select * from collab_help where run_id = :r and status = 'open' order by due_at;

-- 找 unknown 副作用（M6）
select * from collab_effect where run_id = :r and status = 'unknown';
```

### 9.2 故障 → 查哪张表

| 现象 | 先查 | 常见结论 |
|---|---|---|
| 两个 Agent 结论不同且都"有依据" | `collab_fact` 的 `based_on` | 事实版本分裂（F1）→ 检查是否绕过了 `ctx.fact` |
| 结论里出现没有依据的数字 | `collab_fact.kind` | 有 `assumption` 进了下游依据（F2）→ 检查 `verify` 是否被调用 |
| 同一个动作被执行两次 | `collab_effect` | 幂等键缺失或含了 attempt（F7） |
| 崩了之后整体重跑 | `collab_step` | 未闭合步骤未被检出（F8） |
| 无人能说清某结论怎么来的 | `v_timeline` | 缺 `based_on` 或未记决策（F9） |
| 任务看起来安排了却没人做 | `collab_task.owner_agent` | 只有 `pending` 没有 `accepted`（X2） |
| 卡住的 Agent 自己编了数据 | `collab_help` | 有疑问但没有求援记录（X3 → F2） |

---

## 10. 实现层反模式

| 反模式 | 为什么错 |
|---|---|
| 节点里直接写业务库，绕过账本 | 账本失去权威性，A2 永远答不出来 |
| 用 LangGraph 的 checkpoint 当账本 | checkpoint 是"图的状态快照"，不含事实版本、副作用、复核与求援；两者职责不同 |
| 只在成功时写 `committed`，不写 `started` | 崩溃点成为盲区，A4 无法判定 |
| 幂等键里包含 `attempt` | 每次重试都会生成新键，等于没有防重 |
| 用 `SELECT` 后 `UPDATE` 做认领 | 并发下双跑，必须单条条件更新 |
| 副作用失败只在日志里留一行 | 超时/失败没有终态，永远无法对账 |
| 让下游自己保证幂等 | 把公共纪律下放为个体责任 |
| 复核人由产出者自己担任 | 等于没有独立视角（数据库 `CHECK` 会直接拒绝） |
| 分歧用"先往下走"处理 | 收口时集中爆发，且对外出现两个答案 |
| 审计日志与账本分成两套 | 两套事实源，等于制造 F1 |

---

## 11. 一页速查

| 标准要求 | LangGraph 里的落点 | 表 / 视图 | 判据 |
|---|---|---|---|
| M1 事实唯一 + 版本 | `ctx.fact()` / `ctx.assumption()` / `verify()` | `collab_fact` | A1 A2 |
| M2 唯一身份 + 认领 | 节点入口 `claim()`（CAS + 租约） | `collab_task` | A1 A3 |
| M3 交接契约 | `ctx.handoff()` + 下游 `verify()` | `collab_handoff` | A2 A4 |
| M4 收敛规则 | `propose()` 比对 `based_on` | `collab_decision` | A5 |
| M5 判据前置 | 建任务写 `acceptance` + checker | `collab_task` | A6 |
| M6 副作用账本 | `reserve/commit/revoke` + `interrupt` 审批 | `collab_effect` | A3 |
| M7 两态留痕 | **节点包装器**（推荐方式①） | `collab_step` | A3 A4 |
| M8 回放 | `timeline()` | `v_timeline` | A2 |
| M9 能力声明 | `AgentSpec` + 决策理由 | 配置 + `collab_decision` | A7 |
| M10 承诺 | `offer/accept/decline` | `collab_task` | A8 |
| M11 求援 | `ask()/answer()` + 超期扫描 | `collab_help` | A9 |
| M12 交叉复核 | `review()` + 数据库 `CHECK` | `collab_review` | A10 |
| M13 协商 | `negotiate()`，只有收敛或保留 | `collab_decision` | A11 |
| M14 可见性 | `presence()` | `v_presence` | A12 |

**一句话**：LangGraph 负责把图跑起来，账本负责让这件事**只有一个答案、可追溯、不重复、协作有序**；两者通过"**账本先行、图后行**"的写序和"**节点包装器**"这一个接缝连接。
