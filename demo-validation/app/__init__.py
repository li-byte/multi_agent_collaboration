"""多 Agent 协作一致性演示系统。

Windows 关键点
--------------
psycopg 的**异步**连接依赖 `loop.add_reader()`，在 Windows 上只能运行在
`SelectorEventLoop`；而 asyncio 在 Windows 的默认循环是 `ProactorEventLoop`。
统一在包导入时把策略设置好，这样 CLI 脚本与 Web 服务都不必各写一遍。

（uvicorn 在 Windows 上会**显式**使用 ProactorEventLoop，见 uvicorn/loops/asyncio.py，
所以 Web 服务请用 `python run_server.py` 启动，而不是 `uvicorn app.server:app`。）
"""

from __future__ import annotations

import asyncio
import sys

__version__ = "0.1.0"

if sys.platform == "win32":  # pragma: no cover - 平台相关
    _selector_policy = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
    if _selector_policy is not None:
        asyncio.set_event_loop_policy(_selector_policy())
