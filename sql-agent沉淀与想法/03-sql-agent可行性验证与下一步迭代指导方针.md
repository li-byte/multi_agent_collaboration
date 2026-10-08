# sql-agent 可行性验证、代码不足与下一步迭代指导方针

## 1. 结论与适用范围

**可以借鉴 DeepSeek-Harness 的协作设计，但应先补执行可靠性，再扩展并行协作。** 当前 sql-agent 已具备角色分工、程序路由、独立执行器、人工确认、持久化账本和结构化输出，适合逐步引入任务生命周期、受限子任务、明确结果协议与恢复机制。直接给现有 LangGraph 增加并行边会发生共享状态冲突；当前账本也不能保证并发认领排他性或业务写入幂等性。

建议下一轮优先完成三件事：封住 SQL 风险分类缺口；纠正取消、失败、完成与事实的表达；建立任务身份和运行排他性。然后先并行化无副作用的分析任务，业务写入继续由执行器统一提交。

本文件补充 [01-多智能体协作指导方针](./01-sql-agent多智能体协作指导方针.md) 与 [02-Harness 源码设计完善建议](./02-引入DeepSeek-Harness协作设计的完善建议.md)。02 负责解释 Harness 源码机制；本文件负责验证这些机制在 sql-agent 上的落点与前置条件。这里的改造设计均为建议，**尚未修改应用代码**。

分析日期：2026-10-08。sql-agent 基线提交：`5c7a72014a4dfb2b916f0e73b6cebfd5c2c4f38f`。Harness 的来源、文件定位及未固定同一提交的限制沿用 02，不能据此宣称完成两个系统的真实集成验证。

## 2. 验证方法与实际结果

### 2.1 已完成的验证

| 检查 | 结果 | 能证明什么 |
|---|---|---|
| check_router | 33 项断言通过 | 现有路由用例符合当前规则 |
| check_guard | 99 项断言通过 | 已收录的 SQL 风险用例通过 |
| check_prompts | 24 项断言通过 | 提示词约束检查通过 |
| check_usage | 20 项断言通过 | 当前用量提取规则通过，含缺失用量返回零的现有约定 |
| check_export | 74 项断言通过 | 已收录的导出检查通过 |
| check_js | 1 个内联脚本语法检查通过 | JavaScript 语法可解析，未验证浏览器交互 |
| 新增离线定向探针 | 15 项观察完成：14 项缺口、1 项可行性证据 | 补查现有测试未覆盖的执行与协作路径 |

现有五组断言合计 **250 项**通过，另有 JavaScript 语法检查。通过只说明原有覆盖内行为成立，不说明本文列出的缺口不存在。

新增探针调用真实的 `sql_guard.analyze`、`catalog_check.check_sql`、`agents.validator/executor/planner/fixer`、`router.decide`、`validators.validate` 与 LangGraph 状态通道；数据库、模型和账本采用替身。探针里的 PASS 意味着观察断言成立；标为 GAP 的 PASS 意味着**缺口已复现，绝非已经修复**。

验证环境为 Python 3.12.10、LangGraph 1.2.14、langchain-core 1.6.7、Pydantic 2.13.5、psycopg 3.3.6。补充依赖安装在隔离目录，没有修改应用 requirements。完整版本见 [结构化探针结果](./验证记录/probe_results.json)。Windows 沙箱中 asyncio 本地 socketpair 初始化曾阻塞，放开该探针执行限制后完成；这是验证环境问题，不计作应用缺陷。

### 2.2 验证材料与边界

- [可复跑探针](./验证记录/probe_feasibility.py)、[运行输出](./验证记录/probe_feasibility.txt)、[JSON 证据](./验证记录/probe_results.json)。
- 现有检查日志：[router](./验证记录/check_router.txt)、[guard](./验证记录/check_guard.txt)、[prompts](./验证记录/check_prompts.txt)、[usage](./验证记录/check_usage.txt)、[export](./验证记录/check_export.txt)、[JS](./验证记录/check_js.txt)。
- 未调用真实模型，未连接业务数据库，未执行真实 DELETE，也未验证多进程抢占、断电恢复、线上确认接口和端到端 SSE。
- 未运行 `check_isolation`：该检查包含实际数据库写入与权限操作，应在可销毁测试库运行。不能把未执行写成通过。
- 本次运行环境不是 Python 3.10；`graph._with_config` 的 3.10 兼容路径仍需单独验证。当前依赖版本下的结果不能直接外推到所有 requirements 允许的版本。

## 3. 代码分析：已复现的不足

### 3.1 P0：带副作用 CTE 被判为只读

证据 G01/G02；入口：`app/sql_guard.py:266`，顶层操作识别在 `:135`，风险判断在 `:287` 及后续；执行入口 `app/agents.py:751`。

```sql
WITH removed AS (
  DELETE FROM products WHERE id = 1 RETURNING id
)
SELECT id FROM removed
```

实际静态检查结果：`level=只读`、`action=SELECT`、`needs_confirm=false`，表目录检查通过。真实 validator 节点在 mock 语义校验下通过，executor 调用执行替身一次、确认函数零次。根因是风险主要按最外层 DML 分类，没有递归识别 CTE 内的写操作。

这证明存在静态防线与确认路径缺口；没有证明真实模型必然接受，也没有在真实 PostgreSQL 上验证删除。业务执行角色本身允许 DML，不能指望禁止 DDL 的权限设计兜住这类写入。

迭代要求：使用能识别 PostgreSQL 语法树及嵌套语句的分析方式，递归汇总副作用；不支持的语法拒绝执行或进入明确人工审核路径。只读执行使用数据库只读事务作第二道防线；函数调用也需能力与副作用策略。不能只加一个 DELETE 关键词正则就宣称覆盖 SQL 安全。

验收：普通 SELECT 可执行；写 CTE 不得走只读路径；未知语法不能默认为安全；确认后 SQL 变化必须重新检查并确认。

### 3.2 P0：执行过、执行成功、用户取消与任务完成混在一起

证据 G03/G04/G05；入口 `app/validators.py:81、103` 及 S7；取消分支 `app/agents.py:826–835`。

已观察到：

- `exec_ok=false` 的合成历史仍使整个一致性报告通过，S7 仅按 `executed` 计覆盖。
- `validated=true`、`validated_passed=false` 的合成历史也通过 S3。正常路由另有检查，因此此项说明最终门禁不足，不能说正常流程必然执行未通过 SQL。
- 在真实 validator 后走真实 executor 取消分支，数据库替身调用为零，但历史记为 `executed=true, exec_ok=true, kind=cancelled`，一致性报告仍通过。

根因是记录存在性被当作成功证据，取消通过成功字段推进游标。取消可以结束当前动作，但不能贡献“成功执行”的事实或下游依赖。

迭代要求：将动作状态显式分成 `succeeded / failed / cancelled / blocked / unknown` 等；区分“工作流已结束”与“用户目标已完成”。最终审查以成功结果、计划版本和 acceptance 为依据；取消/拦截允许成为正常终态，但答复必须说明哪些目标没有执行。

验收：失败、取消不能满足成功依赖；被取消动作可以终止流程且不伪造成功；正常执行必须对应校验通过记录；合成损坏历史被最终门禁识别。

### 3.3 P0：节点认领与结算身份不一致

证据 G06/G07；入口 `app/agents.py:_claim(:95)、planner(:332)、fixer(:966)`。

planner 在 cursor=1 时实际认领 `planner-r0-c1`，结算却传 `planner-r0-c0`。fixer 在 round=0 时认领 `fixer-r0-c0`，结算传 `fixer-r1-c0`。前者硬编码 cursor，后者计算了新 round 但认领仍使用旧 state。

替身记录确认了调用参数不一致；未运行真实账本 SQL，不能声称观察到具体数据库脏行。按照 `ledger.finish_task` 的 WHERE 条件，身份不一致可能更新不到认领行或误触同名旧行，影响租约、恢复和审计。

迭代要求：`_claim` 返回唯一任务句柄，后续结算、事件、模型用量引用同一句柄；业务子任务 ID、节点 activation ID、attempt 各司其职。结算零行必须报冲突，不能静默完成。

### 3.4 P0/P1：记忆把模型解释与完整事实混合

证据 G08/G09；入口 `app/agents.py:886–925`、`app/ledger.py:304`。

执行替身返回 60 行且 `truncated=true`；保存记忆仅保留 50 行，没有截断字段，但实体列表含 60 个值。另一个探针让模型输出“模型声称这就是全部商品”，该摘要被拼入 claim 并存为 `verified=true`。

数据库执行成功可以证实 SQL 和返回数据，不能自动证实模型对数据的概括，更不能证实“全部”。当下游依据不完整实体清单做修改时，这会成为执行风险。

迭代要求：拆分 `execution_evidence` 与 `interpretation`，保存来源 SQL hash、查询范围、数据库返回是否截断、记忆是否裁剪、总数是否已知及结构化结果引用。子任务需要完整实体集合时应读取可验证结果集或重新查询，不能依赖展示摘要。对解释类 claim 单独标明验证状态。

验收：60 行结果与 50 行展示样本的关系可追溯；截断不会丢失；模型声称“全部”不能直接成为 verified fact；完整性不足的写操作依赖被阻塞。

### 3.5 P1：重复失败判定与依赖调度不够严格

证据 G10/G11；入口 `app/router.py:35`、`app/state.py:QueryIntent`、`app/agents.py:_handoff`。

失败序列 A、B、A，全部来自另一个子任务，当前 router 仍以“同一个错误连续出现两次”停止修正。实际实现是在历史内计数，既未限定当前任务，也不是相邻窗口。QueryIntent 接受自依赖；当前任务依赖 missing 时，router 仍进入 generator。提示上下文里的 FACTS_MISSING 不是调度门禁。

迭代要求：停滞判断按 task、计划版本与具体失败阶段归组，分别记录连续重复与累计失败；计划落库前检查唯一 ID、依赖存在性、无环性。只调度依赖已成功且证据满足要求的任务，取消/失败按策略向下游传播。

验收：A/B/A 不被解释为连续 A；其他任务的失败不污染当前重试；自环、循环、未知依赖被拒绝；可识别依赖任务等待与死锁。

### 3.6 P1：并行需要独立状态；checkpoint 降级路径没有闭合

证据 G12/G13；入口 `app/state.py:356`、`app/graph.py:341`。

真实 LangGraph 最小图让两个节点同一步更新 TaskState.draft，抛出 `InvalidUpdateError`。现有 draft/checks/result/cursor 表达当前单任务状态，不能直接承载同时运行的多个子任务。仅加 reducer 也不足以解决相互覆盖的业务含义，需要按 task_id 组织状态和明确归并规则。

无 checkpointer 的实际编译图执行后，`aget_state` 抛出 `No checkpointer set`；当前 `_finish` 无条件调用该方法。源码允许禁用 checkpoint 或初始化失败后继续构建图，因而结束/恢复能力与降级策略不一致。

迭代要求：生产恢复模式要求 checkpoint 可用，否则启动失败；如允许无 checkpoint 模式，则用本次运行最终输出结束，并明确禁用确认暂停与恢复。把最终一致性报告显式写入状态/账本，避免仅由 `status=done` 推导 consistency_passed。

### 3.7 P1：用量缺失无法与真实零消耗区分

证据 G14；入口 `app/runtime.py:32`。

`usage_of(None)` 返回 total_tokens=0，现有 check_usage 也认可这个约定。这不是原测试失败，而是扩展到任务预算时数据模型不足：缺失统计与真实零消耗被混在一起。

迭代要求：增加 usage_available/source，未知值保持 null；预算同时限制 token、调用次数、运行时长、并发数。缺少 token 时仍以次数和时长形成上限；明确 SDK 重试与应用重试的计量边界。

## 4. 静态确认的结构性风险，尚未做真实集成复现

| 问题 | 代码依据 | 下一步验证/改进 |
|---|---|---|
| 租约与 version 没有形成排他认领 | `ledger.py:231` 冲突更新直接重置 running/owner；`:253` 结算未检查 owner、version、有效租约 | 测试库两 worker 竞争：仅一个获权；旧持有者不得结算；使用原子条件更新和 fencing token |
| 有幂等键不代表 SQL 只执行一次 | `open_task` 保存 key，但冲突仍可重认领；业务库执行与账本更新分离 | 重复启动、重复确认、提交后进程退出三个场景验证；幂等记录需与业务写入处于可证明的事务边界，状态不确定时先对账 |
| 同一 run 可产生多个后台调用 | `server.py:274、286` 创建后台 asyncio task；全局 Semaphore(2) 不等于每 run 排他 | 同 run 原子 claim、运行注册表、确认一次消费；再做多进程压测 |
| 计划修订缺少语义版本 | planner 用历史 executed ID 推断已完成，允许 revise/finish；acceptance 没有形成完整程序契约 | 引入 plan_version 和 task_spec_hash；旧 ID 改语义必须新版本；保留原始目标与 dropped/waived 原因 |
| 动态表目录可能使一次 run 使用不同版本 | `catalog.py:141–142` 全局替换；`server.py:166` reload | 每 run 绑定不可变 catalog_version；确认/恢复时处理版本变化 |
| SSE 推送有丢尾事件风险 | `runtime.py:94` QueueFull 忽略；SSE 依赖队列实时通知 | 账本作为权威事件源，队列只唤醒；按 seq 定期补拉；模拟满队列、断线重连和 done 丢失 |
| 行数确认阈值未参与执行决策 | `config.py:66` 与 health 输出存在参数，executor 未使用该阈值升级确认 | 明确高影响操作定义，接入确定性策略；EXPLAIN 估计不能单独作为安全证明 |
| 对话 turn 分配存在并发风险 | `ledger.next_turn` 使用 MAX(turn)+1 | 数据库原子分配及唯一约束，双请求竞争验证 |
| 后台任务缺少完整取消/恢复归属 | create_task 及进程内 semaphore 只提供当前进程调度 | 记录父任务、activation、取消请求、终态；进程重启后明确接管或失效策略 |

这些风险通过源码路径确认设计缺口，但尚未执行竞争、恢复或真实数据库实验。不得把“可能重复写”描述成已经观察到重复写入。

## 5. Harness 源码协作设计的迁移可行性

对应 Harness 的具体源文件和函数见 02 的源码证据表。以下是基于本项目验证后的采用顺序。

| Harness 机制 | sql-agent 可复用基础 | 可行性与限制 | 建议顺序 |
|---|---|---|---|
| provider/runtime 接口分离 | Runtime、结构化 `_call`、六角色节点 | 可先封装分析任务后端；不要求整体迁移到 TypeScript | 第二阶段 |
| 前台一次性任务与后台 job 分开 | 节点函数与 server 后台任务已有雏形 | 需要 JobRegistry、明确等待/取消/终态；不能把 create_task 当完整 job 系统 | 第二阶段 |
| Session 与 Activation 分离 | conversation/run/agent_task 已有存储概念 | 需补独立 task/session/activation 身份、输入版本和唯一结算规则 | 第一至二阶段 |
| ownership、settlement、dispose | 租约、version、finish_task | G06/G07 先修身份；随后原子认领、结算校验、父任务清理 | 第一阶段 |
| authoritative result 与输出捕获 | 执行器有真实 SQL 结果，模型有结构化输出 | G08/G09 说明必须隔离证据与解释；不采信任意子任务总结为事实 | 第一阶段 |
| scoped tools 与委派权限固定 | validator 无 DB、executor 唯一执行 | 适合只给分析子任务表目录与证据读取权限；不要传完整 Runtime/业务 DB 能力 | 第二阶段 |
| fork/fresh 上下文 | prompts 与 `_handoff` | fresh 先落地；fork 必须定义可见证据、计划版本、快照边界与脱敏 | 第二阶段 |
| queue/steer inbox、父子消息 | 当前只有 EventBus/UI 事件与 handoff | 新建控制消息模型；UI 事件总线不能直接充当可靠任务 inbox | 第三阶段 |
| coldResume 与资源恢复 | LangGraph checkpoint + ledger | 描述符、进程内对象、数据库证据职责不同；不能照抄 live object 关系为跨进程恢复 | 第三阶段 |
| workflow host 的任务创建/结果/清理闭环 | 动态 router | 增加 DAG、任务状态和生命周期；不是允许模型任意调度即可 | 第三阶段 |

正向证据 F01：两个真实 validator 节点各用独立输入并发调用异步模型替身，峰值在途调用为 2，均返回通过。它只验证节点函数的异步组合基础；**没有验证真实模型限流、共享数据库账本、并行 LangGraph 集成或收益**。G12 则证明当前共享图状态不能直接并行更新 draft。

SQL 场景与通用编码 Harness 的主要差异是业务副作用：撤销一个子智能体不能自动撤销已提交 SQL。应保留“分析可并行、业务提交由唯一执行器协调”的第一版边界；执行器还必须具备任务幂等与事务证据，而非只在提示词中约定唯一。

## 6. 下一步迭代指导方针与验收门槛

### 第一阶段：安全与执行一致性，作为下一次迭代主目标

建议拆成可独立审查的改动，避免一次引入整个 Harness。

1. **风险识别与确认绑定**：递归识别 SQL 副作用；确认记录绑定 run/task/plan_version/sql_hash/risk/catalog_version；SQL 或风险变化使旧确认失效。为只读执行加数据库事务防线。
2. **结果与终态协议**：修正取消历史；区别失败、取消、成功与整体目标达成；最终门禁检查校验通过和成功证据；把完整性元数据贯穿 history/memory/依赖输入。
3. **统一任务句柄与执行排他**：修复 planner/fixer 认领结算身份；引入 owner/version 条件；拒绝重复 start/confirm；对 SQL 提交与账本落库之间的未知结果明确对账策略。
4. **checkpoint 模式闭合**：确认/恢复模式缺少 checkpoint 时启动失败，或完整实现并声明无恢复模式；落库最终一致性报告。

完成门槛：本文 G01–G09、G13 对应的反例转为防护回归用例；取消不再写为已执行成功；真实隔离测试库验证 DML 权限、只读事务、重复确认与重复启动。原有 250 项断言继续通过。安全解析改动需扩充语法变体，而非只测试单条样例。

### 第二阶段：任务协议与无副作用并行试点

1. 计划增加版本、唯一 ID、依赖 DAG 校验、acceptance 和证据要求；按任务归组重试、预算和停滞判定。
2. 定义 AnalysisTask 输入：目标、允许表目录快照、可见 evidence refs、输出 schema、预算、截止时间、能力范围。
3. 定义 AnalysisResult：task_id、activation_id、status、结构化 payload、evidence refs、usage_available、错误与完成时间。模型解释不能冒充执行证据。
4. 先试点两个并行只读分析：候选 SQL 生成与独立语义审查，或两个候选生成；子任务不持有业务执行能力。父协调器归并结果后仍逐一走风险校验和执行流程。
5. 将图状态组织为 `tasks[task_id]`，结果 reducer 只归并独立任务结果；计划与执行游标由父协调器独占。动态 fan-out 所需 LangGraph API 应在锁定版本上验证。

完成门槛：G10/G11/G12/G14 对应反例有确定性处理；两分支互不覆盖，失败能局部归因；取消与超时有明确终态；并行/串行使用同一任务集比较正确率、P50/P95 延迟、调用数、token、冲突和结果一致性。只有实测收益成立才扩大并发。

### 第三阶段：后台生命周期、消息与恢复

引入持久 JobRegistry、父子归属、activation 生命周期、queue/steer 区分、取消传播与结果结算。只把可序列化 descriptor、证据引用和权限快照作为恢复材料，不持久化活连接或依赖进程内对象重建关系。事件通知与控制消息分离；账本事件按 seq 可补拉。

完成门槛：worker 在模型返回前、SQL 提交前、SQL 提交后账本写入前、人工确认等待时退出，均有明确恢复结果；旧 worker 不得覆盖新 owner 结果；父任务终止后无失控子任务；SSE 丢通知后仍可读到权威终态。对 SQL 提交后状态未知，先查证再重试，不能以“恢复成功”掩盖重复写风险。

## 7. 工程落地原则

- **先形成程序不变量，再扩智能体数量**：依赖、权限、确认、身份、预算和终态由代码控制，模型只提供方案与解释。
- **事实来源可追溯**：数据库事实、静态检查、模型判断和用户决定分开保存；每项结果有来源、版本与完整性。
- **并发从独立分析开始**：没有业务副作用的子任务最适合首次试点；并行写入需另行证明事务与幂等设计。
- **一项机制一组失败场景**：新能力必须覆盖取消、超时、重复消息、部分结果、恢复和竞争，而不只是成功演示。
- **用收益决定复杂度**：为小任务保留串行路径。若多智能体增加成本而未提高质量或延迟，就不扩大默认调用链。

下一轮建议以第一阶段作为可交付范围，并为第二阶段预留协议边界。先把“执行是否可靠、事实是否真实、任务是否可恢复”做实，再让多个智能体同时工作。
