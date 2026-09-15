"""运行时上下文：配置 + 账本 + LLM + 事件总线。"""

from __future__ import annotations

import asyncio
from collections import defaultdict

from .config import Settings
from .ledger import Ledger


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
                # 慢消费者丢事件，但事件已落库，刷新页面仍可完整回放
                pass


class Runtime:
    """节点执行时能拿到的一切依赖。"""

    def __init__(self, settings: Settings, ledger: Ledger, llm, bus: EventBus) -> None:
        self.settings = settings
        self.ledger = ledger
        self.llm = llm
        self.bus = bus

    def structured(self, schema):
        """把 LLM 绑定到结构化输出契约。

        DeepSeek 不支持 OpenAI 的 `response_format: json_schema`，必须走 function calling。
        """
        method = (self.settings.structured_method or "function_calling").strip()
        if method == "auto":
            return self.llm.with_structured_output(schema)
        try:
            return self.llm.with_structured_output(schema, method=method)
        except TypeError:
            return self.llm.with_structured_output(schema)

    async def emit(
        self, run_id: str, role: str, event_type: str, payload: dict,
        version: int = 0, round_no: int = 0, citations: list[dict] | None = None,
    ) -> dict:
        """事件先落账（可回放），再推给在线订阅者。"""
        event = await self.ledger.append_event(
            run_id=run_id, role=role, event_type=event_type, payload=payload,
            version=version, round_no=round_no, citations=citations,
        )
        self.bus.publish(run_id, event)
        return event
