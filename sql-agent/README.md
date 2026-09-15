# sql-agent · 多智能体自然语言转 SQL

用 **LangGraph 1.x** 搭的多智能体系统：**你说一句话，六个智能体协作把 SQL 写出来、验证、执行**，
失败就自动修正；删除操作停下等你确认；任何 DDL 一律拒绝。

> 设计思想沿用已冻结的 `demo-validation`：**模型负责判断，运行时负责证明。**
> 但这一版的**协作链路不固定** —— 每个智能体自己建议下一步交给谁，护栏决定能不能去。

---

## 1. 一句话看懂它做什么

```
你：删掉所有已取消的订单
  ↓
规划器  → 拆成「删除 orders 表中 status='已取消' 的行」
生成     → DELETE FROM orders WHERE status = '已取消'
验证     → 静态防线（是 DELETE） + EXPLAIN（语法语义 OK） + 判断风险=需确认
执行     → ⏸ 暂停：「这条会删除 N 行，需要你确认」  [确认执行] [取消]
  ↓
你点确认 → 执行 → 影响 3 行 → 汇总汇报
```

---

## 2. 六个智能体 + 动态链路

| 智能体 | 职责 |
| --- | --- |
| **规划器** | 读表结构，把问题拆成数据操作意图；**判定"这个请求我不做"**（要删表等） |
| **SQL 生成** | 为当前子任务写一条 SQL |
| **SQL 验证** | 静态防线 + `EXPLAIN`（不执行）+ 语义校验 |
| **SQL 执行** | 在**受限连接**上执行；需确认时暂停 |
| **SQL 修正** | 拿失败原因重写 SQL |
| **汇总审查** | 汇总结果 + 硬门禁 + 给用户的答复 |

### 链路不固定

每个智能体产出时都带一个 `handoff_to`（**它自己建议下一步交给谁**），
`router` 节点用运行时候选集过滤：

| 当前状态 | 允许去哪 | 守什么 |
| --- | --- | --- |
| 还没规划 | 规划器 | 不能直接生成 |
| 没有 SQL | 生成 | — |
| 有 SQL 未验证 | 验证 | **绝不能跳过去执行** |
| 判定禁止 | 汇总 | 永不进执行 |
| 验证不通过 | 修正 / 规划器 | **让模型选**：小修还是重新理解 |
| 需确认未确认 | 执行（内部暂停） | 人工协作 |
| 执行失败 | 修正 / 规划器 | **让模型选**：改 SQL 还是重拆问题 |
| 执行成功还有子任务 | 生成 | — |
| 全部完成 | 汇总 | — |

所以简单问题一条 SQL 直达；复杂问题可能反复修正、甚至退回重新规划。
界面上每一步的 **`改了什么`** 都会标出来（模型想去 A，护栏拦住了 → 实际去 B）。

---

## 3. 三层安全防线（都有实测）

### 第一层 · 静态防线（`app/sql_guard.py`）

| 级别 | 语句 | 处理 |
| --- | --- | --- |
| 🚫 **禁止** | `DROP` `TRUNCATE` `ALTER` `CREATE` `GRANT` `COPY` `DO $$` `SELECT INTO`、多语句、事务控制、读文件函数… | **直接拒绝** |
| ⏸ **需确认** | `DELETE`（任何形式）；`UPDATE` 无顶层 `WHERE` | 暂停等确认 |
| ✅ **自动** | `SELECT` / `WITH`；`INSERT`；`UPDATE` 带 `WHERE` | 直接执行 |

细节处理过：抹掉字符串与注释后再扫描（`WHERE x='DROP TABLE'` 不误报、`DROP/**/TABLE` 不漏报）；
用 token 层级取**顶层 DML**（`WITH x AS (SELECT…) DELETE FROM t` 正确识别为 DELETE）；
剥掉括号内容再找 `WHERE`（子查询里的 WHERE 不算，避免全表更新被误判为安全）。

**39 个对抗性用例全过**：`python scripts\check_guard.py`

### 第二层 · 数据库权限兜底（**真正的保证**）

应用层校验可以被绕过，权限绕不过去。执行用户 SQL 用的是受限角色：

```sql
CREATE ROLE agent_sql_runner LOGIN PASSWORD '...';
GRANT SELECT, INSERT, UPDATE, DELETE ON customers, products, orders, order_items TO agent_sql_runner;
-- 刻意不给任何 DDL 权限
```

`python scripts\check_isolation.py` 实测：

```
cs_v1        读/插/改/删 ✓   删表/清空/改结构/建表/建索引/删库 全部被拒
             提权尝试（GRANT/REVOKE/改owner）→ 权限快照前后完全一致
agent_sql    连项目自己的系统表都读不到（permission denied）
```

### 第三层 · 执行沙箱

- 每条 SQL 包在**显式事务**里，出错自动 `ROLLBACK`
- 连接级 `statement_timeout=5000ms`
- 查询结果截断到 `MAX_ROWS=500`
- 预估影响超过 `CONFIRM_ROW_THRESHOLD=1000` 也转人工确认

---

## 4. 人工协作（human-in-the-loop）

需确认的操作，执行器调用 LangGraph 原生 `interrupt()` 暂停，前端渲染确认卡片：

```
⏸ 需要你确认后才执行
   DELETE FROM orders WHERE status = '已取消'
   风险：需确认（DELETE）｜ 涉及表：orders ｜ 预估影响：3 行
   ⚠ orders 的删除会级联删除 order_items.order_id
   [ 确认执行 ]   [ 取消 ]
```

用户点确认 → `POST /api/runs/{id}/confirm` → `Command(resume=...)` 继续跑。
**取消的话一行都不改**（下方自检场景 C 验证了这点）。

> ⚠️ **Python 3.10 的坑**：LangGraph 给异步节点注入 config 靠
> `asyncio.create_task(coro, context=ctx)`，而 `context=` 是 **3.11+** 才有的。
> 3.10 上节点里调 `interrupt()` 会报 `Called get_config outside of a runnable context`。
> `app/graph.py` 的 `_with_config` 手动补了这个上下文，等价于 3.11+ 的默认行为。

---

## 5. 表设计 · 两库隔离

| 库 | 表 | 谁用 |
| --- | --- | --- |
| **cs_v1** | `customers` / `products` / `orders` / `order_items` | 智能体执行 SQL 的**目标**（受限角色只有 DML） |
| **agent_sql** | `agent_run` / `agent_task` / `agent_event` / `sql_audit` | 项目**自身**记录（只有应用的 postgres 连接能碰） |

业务表刻意带外键关系（`orders.customer_id → customers`、`order_items.order_id → orders ON DELETE CASCADE`），
既能耗演示 JOIN/聚合，也能演示"删除会级联"这种真实提醒。

`sql_audit` 记下每条 SQL 的全程：`generated → validated / rejected → await_confirm → confirmed → executed / failed`，
含 `risk_level`、`est_rows`、`affected_rows`、`confirmed_by`、`error`、`duration_ms`。

---

## 6. 八条运行时一致性不变量（`app/validators.py`）

| ID | 不变量 |
| --- | --- |
| S1 | 每条 SQL 都要有意图说明并对应到子任务 |
| S2 | SQL 只能访问被授权的表 |
| S3 | SQL **必须经过验证**才能执行 |
| S4 | 静态判定禁止的语句**永远不能被执行** |
| S5 | 需确认的操作必须有**用户确认记录** |
| S6 | 执行过的 SQL 必须有结果留痕 |
| S7 | 每个子任务都要有对应的 SQL |
| S8 | 所有执行记录都要带幂等键（防"确认后重复执行"） |

只要有一条 fail，汇总审查器**不得** approve —— 硬门禁，模型说了不算。

---

## 7. 快速开始

```powershell
cd E:\pycharm_python_project\multi_agent_collaboration\sql-agent
$py = '..\.venv310\Scripts\python.exe'      # 沿用冻结版建的 3.10 环境（已装 sqlparse）
$env:PYTHONIOENCODING = 'utf-8'

# ① 初始化：建角色 + 建两个库的表 + 灌种子数据
& $py scripts\init_db.py --reset

# ② 自检（不依赖服务）
& $py scripts\check_guard.py          # 39 项：静态防线
& $py scripts\check_isolation.py      # 两库隔离 + 权限兜底
& $py scripts\run_cli.py "查一下北京客户的订单总额"

# ③ 起服务
& $py run_server.py                   # → http://127.0.0.1:8000

# ④ 端到端（另开终端，需先起服务）
& $py scripts\check_web.py --base http://127.0.0.1:8000    # 14 项
```

`.env` 关键配置：

```dotenv
PG_DB_LEDGER=agent_sql     # 项目自身记录
PG_DB_BIZ=cs_v1            # 智能体执行 SQL 的目标库
RUNNER_USER=agent_sql_runner
LLM_MODE=deepseek          # 或 mock（离线兜底）
USE_CHECKPOINTER=on        # 人工确认依赖它
```

---

## 8. 验证结果

```
Python 编译                    ✅
前端 JS 语法（Node --check）   ✅
静态防线（对抗性用例）         ✅ 39/39
两库隔离 + 权限兜底            ✅ 全部符合预期（含提权尝试）
Web 端到端自检                 ✅ 14/14
      场景 A 只读查询    → 自动执行，审计 generated→validated→executed
      场景 B 删除 + 确认 → 暂停 confirming → 确认后执行，留下 confirmed_by
      场景 C 删除 + 取消 → 暂停后取消，**执行阶段数 = 0**
真实 DeepSeek 危险请求         ✅ 「删掉 orders 表」被规划器直接拒绝，
                                 并给出替代建议与级联风险提醒，未产生任何 SQL
```

---

## 9. 演示讲解顺序（5 分钟）

1. **先来一个正常的**：「查一下北京客户的订单总额」→ 规划→生成→验证→执行一气呵成
2. **看链路不固定**：点「🔀 协作链路」，看每一步的 `handoff` 与护栏的理由
3. **危险请求**：「帮我删掉 orders 表」→ 规划器拒绝，解释 + 给替代方案
4. **人工协作**：「删掉所有已取消的订单」→ ⏸ 暂停，看确认卡片里的影响评估与级联提醒 → 确认执行
5. **看审计**：点「🧾 SQL 审计」，每条 SQL 的生成→验证→确认→执行全程留痕
6. **收尾讲安全**：应用层拦截可以被绕过，但**数据库权限绕不过去** —— `check_isolation.py` 实测为证

> 技术选型可以不同，边界不能空着。
