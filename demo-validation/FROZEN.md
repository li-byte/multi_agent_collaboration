# 🧊 已冻结 · Frozen

> **冻结日期**：2026-09-15
> **状态**：验证已通过，**不再修改**
> **用途**：通用多智能体协作一致性的可运行验证版本（归档对照用）

---

## 一、验证结论

| 项目 | 结果 |
| --- | --- |
| Python 编译 | ✅ 18 个文件 |
| 前端 JS 语法（Node `--check`） | ✅ 33,172 字符 |
| 契约 + 引用校验自检 | ✅ **13/13** |
| Web 端到端自检（两个真实场景） | ✅ **15/15** |
| 场景 A · 资料覆盖问题 | 1 轮通过 · **12 条引用全部逐字命中** · 0 次否决 |
| 场景 B · 资料答不上问题 | 1 轮通过 · 0 条引用 · 3 个分点**显式声明「资料不足」** |

两个场景都：不编造、不漏答、以 `done` 结束。

---

## 二、这一版是什么

```
用户提问 + 提供资料
      ↓
规划器 → 研究员 → 执行器 → 审查器（可否决回流）
      ↓
返回答案（每条结论都能点回你给的原文）
```

- **不限行业**：健康、代码、法律、任何领域都行
- **没有快照**：不需要 commit / 时间窗，「同一份事实」由引用保证
- **核心约束**：结论必须引用用户提供的原文，且**逐字命中**
- **没资料不干活**：建任务直接被拒（`400 need_material`），前端追问用户补资料

### 八条不变量（`app/validators.py`）

| ID | 不变量 |
| --- | --- |
| V1 | 答案的每个分点要有原文依据，或显式声明资料不足 |
| **V2** | **引用的摘录必须能在资料里逐字找到**（防编造） |
| V3 | 每条研究结论要有原文依据，或显式声明资料不足 |
| V4 | 答案只能引用研究结论引用过的资料片段 |
| V5 | 每条验收条件要有原文依据，或显式声明资料不足 |
| **V6** | **拆出的每个子问题都要有答案覆盖** |
| V7 | 状态版本必须被推进过 |
| V8 | 所有执行记录必须带幂等键 |

> 只要有一条 fail，审查器**不得** approve（硬门禁，`app/agents.py::reviewer`）。
> 模型的 approve 会被运行时强制改写成 veto。
>
> **回流没有任何人为注入** —— 只在模型真的漏答或改写引用时触发。

---

## 三、怎么跑

虚拟环境在**上一级目录**（没有随代码一起搬进来）：

```powershell
cd E:\pycharm_python_project\multi_agent_collaboration\demo-validation
$py = '..\.venv310\Scripts\python.exe'
$env:PYTHONIOENCODING = 'utf-8'          # 控制台中文不乱码

# ① 自检（不依赖服务）
& $py scripts\check_contracts.py         # 13 项
& $py scripts\run_cli.py                 # 完整链路

# ② 起服务
& $py run_server.py                      # → http://127.0.0.1:8000

# ③ 端到端（另开终端，需先起服务）
& $py scripts\check_web.py --base http://127.0.0.1:8000    # 15 项
```

**数据库**：`.env` 里指向 `192.168.10.101/agents`。
表结构变了要重建时：`& $py scripts\init_db.py --reset`

> ⚠️ **不要用 `uvicorn app.server:app`**。Windows 上 uvicorn 会显式选用
> `ProactorEventLoop`，而 psycopg 异步只能跑 `SelectorEventLoop`。
> `run_server.py` 自己拿循环直接 `serve()`，绕开了这个坑。

---

## 四、目录结构

```
demo-validation/
├── run_server.py                 # 启动入口（Windows 必须用它）
├── requirements.txt
├── schema.sql                    # 5 张自建表
├── .env / .env.example / .gitignore
├── README.md                     # 完整使用说明
├── 多Agent协作一致性.md            # 设计出发点的原文（文章）
├── app/
│   ├── sources.py                # 资料切分 + 逐字引用校验  ← 一致性的着力点
│   ├── state.py                  # Pydantic 契约 + 接入层适配器
│   ├── validators.py             # 8 条一致性不变量
│   ├── agents.py                 # 四个智能体节点 + 硬门禁
│   ├── ledger.py                 # 账本：租约 / 幂等 / 乐观锁
│   ├── graph.py                  # LangGraph 编排
│   ├── runtime.py                # 事件总线
│   ├── llm.py                    # DeepSeek 接入
│   ├── mock_llm.py               # 离线兜底
│   ├── mock_data.py              # 界面示例资料
│   ├── config.py                 # 配置
│   ├── server.py                 # FastAPI + SSE
│   └── __init__.py               # Windows SelectorEventLoop 策略
├── web/index.html                # 对话式单页
├── scripts/                      # 4 个脚本（init_db / run_cli / check_contracts / check_web）
└── docs/
    ├── 01~04-*.md                # 早期「带快照」版本的设计记录（已标注作废）
    └── 05-通用版设计.md           # ✅ 当前实现的权威设计文档
```

---

## 五、实施过程记录（跑起来才发现的 4 个真 bug）

留档，避免新验证重复踩：

1. **`DROP TABLE ... RETURNING` 不是合法 SQL** —— 建表重置逻辑报错。
2. **接入层适配器把 `Citation` 对象丢了** —— mock 模式直接传 `Citation` 实例，
   而 `coerce_citations` 只放行 dict/str，导致引用全空、链路一路否决到失败。
3. **`chunk_id` 是全局主键** —— 每次提问都生成 `D1-1`、`D1-2`…
   **第二次提问就撞主键被 `DO NOTHING` 丢掉**，资料插不进去。
   已改成 `PRIMARY KEY (global_task_id, chunk_id)`。**只跑一次永远发现不了。**
4. **「资料答不上来」时会被否决到失败** —— V2/V5 当时要求"必须有引用"，
   于是诚实地回答"资料不足"反而被判不合格。已引入 `insufficient` 显式声明通道。

---

## 六、别再改这里

这是**冻结快照**，只用于归档、演示、对照。

新的验证请在上一级目录另开新目录进行 —— 那里现在只剩 `.idea` / `.venv` / `.venv310` / `demo-validation`，
位置是空的，可以直接放新目录。

如果新验证也要用同一个数据库，注意 `init_db.py --reset` 会**删掉所有表**，
会和这边的历史数据冲突；建议新验证换一个 `PG_DB`。
