"""运行时上下文：配置 + 账本 + 业务库 + LLM + 事件总线 + 系统内置表目录。

注意这里**没有 schema 快照**。表结构来自 `app/catalog.py` 里系统内置的定义，
不读数据库 —— 理由写在那个文件的开头（读了库，修正智能体就永远等不到真实报错）。
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

from .config import Settings
from .db import BizDatabase
from .ledger import Ledger
from . import catalog

logger = logging.getLogger("sql_agent.runtime")


def split_raw(res) -> tuple[object | None, object | None]:
    """拆开 `include_raw=True` 的返回值：({"raw":…, "parsed":…, "parsing_error":…}).

    万一某个 provider / 版本不支持 include_raw，结果是**解析后的对象本身** ——
    这里认两种形状，调用方就不用关心用的是哪种。
    """
    if isinstance(res, dict) and "parsed" in res:
        return res.get("raw"), res.get("parsed")
    return None, res


def usage_of(raw) -> dict:
    """从原始响应里读出 token 用量。

    各家的字段名不统一（OpenAI 风格 `usage_metadata` / 原始 `response_metadata.token_usage`），
    **读不到就记 0** —— 不按字符数猜。DeepSeek 另外会给提示词缓存命中量，
    命中部分比未命中便宜得多，所以单独记一列。

    返回值**形状恒定**（永远有这 6 个键）：拿到没拿到原始响应都一样，
    调用方不必区分"没有用量"和"用量是 0"。
    """
    um = (getattr(raw, "usage_metadata", None) or {}) if raw is not None else {}
    meta = (getattr(raw, "response_metadata", None) or {}) if raw is not None else {}
    tu = meta.get("token_usage") or meta.get("usage") or {}
    in_details = um.get("input_token_details") or {}
    out_details = um.get("output_token_details") or {}
    tu_in_details = tu.get("prompt_tokens_details") or {}
    tu_out_details = tu.get("completion_tokens_details") or {}

    def pick(*vals) -> int:
        for v in vals:
            if v:
                return int(v)
        return 0

    return {
        "model": meta.get("model_name") or meta.get("model"),
        "prompt_tokens": pick(um.get("input_tokens"), tu.get("prompt_tokens")),
        "completion_tokens": pick(um.get("output_tokens"), tu.get("completion_tokens")),
        "total_tokens": pick(um.get("total_tokens"), tu.get("total_tokens")),
        "cached_tokens": pick(in_details.get("cache_read"),
                              tu_in_details.get("cached_tokens"),
                              tu.get("prompt_cache_hit_tokens")),
        # 推理型模型才有；普通对话模型这里是 0
        "reasoning_tokens": pick(out_details.get("reasoning"),
                                 tu_out_details.get("reasoning_tokens"),
                                 tu.get("reasoning_tokens")),
    }


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

    # ------------------------------------------------------------ 表（系统内置）

    @property
    def allowed_tables(self) -> set[str]:
        """白名单 = 系统目录里定义的表。库里多出来的表一律不认。"""
        return catalog.allowed_tables()

    @property
    def catalog(self):
        return catalog

    # ------------------------------------------------------------ LLM

    def structured(self, schema):
        """绑定结构化输出。DeepSeek 不支持 json_schema，必须走 function calling。

        `include_raw=True`：解析结果照旧在 `parsed` 里，同时把**原始响应**一起带回来。
        为什么要多这一份 —— token 用量只存在于原始响应里，
        不带回来就只能按字符数估算，而估算的数字没法对账。
        """
        method = (self.settings.structured_method or "function_calling").strip()
        kwargs = {} if method == "auto" else {"method": method}
        try:
            return self.llm.with_structured_output(schema, include_raw=True, **kwargs)
        except TypeError:
            # 这个 provider / 版本不吃 include_raw —— 退回去，用量就记 0（如实，不估）
            try:
                return self.llm.with_structured_output(schema, **kwargs)
            except TypeError:
                return self.llm.with_structured_output(schema)

    @property
    def llm_enabled(self) -> bool:
        """离线 mock 模式没有大模型 —— 消耗清单应该显示「没有请求」，而不是显示 0 装成很省。"""
        return self.llm is not None

    async def record_llm(
        self, ctx: dict, *, usage: dict, duration_ms: int | None,
        ok: bool = True, error: str | None = None, attempt: int = 1,
    ) -> None:
        """把一次调用的消耗落账 + 广播事件（前端据此实时累加）。

        账本失败不能拖垮主流程 —— 消耗是观测，不是业务。
        """
        run_id = (ctx or {}).get("run_id")
        if not run_id or not self.llm_enabled:
            return
        role = (ctx or {}).get("role") or "unknown"
        stage = (ctx or {}).get("stage") or ""
        round_no = int((ctx or {}).get("round") or 0)
        cursor = int((ctx or {}).get("cursor") or 0)
        try:
            call_id = await self.ledger.save_llm_call(
                run_id, role=role, stage=stage, round_no=round_no, cursor=cursor,
                attempt=attempt, model=usage.get("model"),
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                total_tokens=int(usage.get("total_tokens") or 0),
                cached_tokens=int(usage.get("cached_tokens") or 0),
                reasoning_tokens=int(usage.get("reasoning_tokens") or 0),
                duration_ms=duration_ms, ok=ok, error=error)
            await self.emit(run_id, role, "llm_call",
                            {"call_id": call_id, "role": role, "stage": stage,
                             "round": round_no, "cursor": cursor, "attempt": attempt,
                             "model": usage.get("model"),
                             "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                             "completion_tokens": int(usage.get("completion_tokens") or 0),
                             "total_tokens": int(usage.get("total_tokens") or 0),
                             "cached_tokens": int(usage.get("cached_tokens") or 0),
                             "duration_ms": duration_ms, "ok": ok,
                             "error": (error or "")[:300] or None},
                            version=int((ctx or {}).get("version") or 0),
                            round_no=round_no)
        except Exception:  # noqa: BLE001
            logger.exception("记录大模型消耗失败（不影响主流程）：%s", role)

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
