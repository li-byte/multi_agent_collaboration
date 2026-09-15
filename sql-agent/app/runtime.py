"""运行时上下文：配置 + 账本 + 业务库 + LLM + 事件总线 + schema 快照。"""

from __future__ import annotations

import asyncio
from collections import defaultdict

from .config import Settings
from .db import BizDatabase
from .ledger import Ledger
from . import schema_info


class EventBus:
    """按 run 分发事件；每个订阅者一个 asyncio.Queue，供 SSE 消费。"""

    def __init__(self) -> None:
        self._subs: dict[str, set[asyncio.Queue]] = defaultdict(set)

    def subscribe(self, run_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self._subs[run_id].add(queue)
        return queue

    def unsubscribe(self, run_id: str, queue: asyncio.Queue) -> None:
        subs = self._subs.get(run_id)
        if not subs:
            return
        subs.discard(queue)
        if not subs:
            self._subs.pop(run_id, None)

    def publish(self, run_id: str, event: dict) -> None:
        for queue in list(self._subs.get(run_id, set())):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass


class Runtime:
    def __init__(
        self, settings: Settings, ledger: Ledger, db: BizDatabase, llm, bus: EventBus,
    ) -> None:
        self.settings = settings
        self.ledger = ledger
        self.db = db
        self.llm = llm
        self.bus = bus
        self.schema: dict = {"tables": {}, "allowed": set()}

    # ------------------------------------------------------------ schema

    async def refresh_schema(self) -> dict:
        """重新读表结构（受限角色视角）。"""
        self.schema = await schema_info.snapshot(self.db)
        return self.schema

    @property
    def allowed_tables(self) -> set[str]:
        return set(self.schema.get("allowed") or set())

    @property
    def schema_text(self) -> str:
        return schema_info.render(self.schema)

    # ------------------------------------------------------------ LLM

    def structured(self, schema):
        """绑定结构化输出。DeepSeek 不支持 json_schema，必须走 function calling。"""
        method = (self.settings.structured_method or "function_calling").strip()
        if method == "auto":
            return self.llm.with_structured_output(schema)
        try:
            return self.llm.with_structured_output(schema, method=method)
        except TypeError:
            return self.llm.with_structured_output(schema)

    # ------------------------------------------------------------ 事件

    async def emit(
        self, run_id: str, role: str, event_type: str, payload: dict,
        version: int = 0, round_no: int = 0,
    ) -> dict:
        """事件先落账（可回放），再推给在线订阅者。"""
        event = await self.ledger.append_event(
            run_id=run_id, role=role, event_type=event_type, payload=payload,
            version=version, round_no=round_no,
        )
        self.bus.publish(run_id, event)
        return event
