"""消耗统计自检：token 到底是从哪儿读出来的、读不到时会怎样。

为什么值得单独检一次：

  · 用量字段各家叫法不一样（`usage_metadata` / `response_metadata.token_usage`），
    **读不到就静默记 0** —— 那种 bug 不会报错，只会让"消耗清单"常年显示 0，
    然后你以为"这个模型真省"。
  · 结构化输出默认只给解析后的对象，**原始响应被丢掉**，用量也就跟着丢了；
    这里同时把「带 include_raw」和「没带」两种形状都验一遍。
  · 缓存命中量（DeepSeek 会给）必须单独记：它和输入 token 是包含关系，
    混在一起就算不出真实花费。
  · **账本返回的时间有两种形状**（datetime 或已经转好的字符串），
    序列化层必须两种都吃 —— 猜错只在**真有数据**时才炸，
    空列表一路无事，正好骗过冒烟测试（这个坑真踩过一次）。

用法：
    python scripts/check_usage.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.runtime import split_raw, usage_of  # noqa: E402
from app.server import _audit_json, _call_json, _iso, _task_json  # noqa: E402


class FakeRaw:
    """假装是模型返回的原始响应对象。"""

    def __init__(self, usage_metadata=None, response_metadata=None) -> None:
        self.usage_metadata = usage_metadata
        self.response_metadata = response_metadata or {}


# (说明, 原始响应, 期望的字段子集)
CASES: list[tuple[str, object, dict]] = [
    (
        "OpenAI 风格 usage_metadata（langchain-openai 的标准位置）",
        FakeRaw({"input_tokens": 1200, "output_tokens": 80, "total_tokens": 1280,
                 "input_token_details": {"cache_read": 900}},
                {"model_name": "deepseek-chat"}),
        {"prompt_tokens": 1200, "completion_tokens": 80, "total_tokens": 1280,
         "cached_tokens": 900, "reasoning_tokens": 0, "model": "deepseek-chat"},
    ),
    (
        "只有原始 response_metadata.token_usage（部分 provider 只给这个）",
        FakeRaw(None, {"token_usage": {"prompt_tokens": 10, "completion_tokens": 5,
                                       "total_tokens": 15, "prompt_cache_hit_tokens": 4}}),
        {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
         "cached_tokens": 4},
    ),
    (
        "prompt_tokens_details.cached_tokens（OpenAI 的另一种写法）",
        FakeRaw(None, {"token_usage": {"prompt_tokens": 100, "completion_tokens": 20,
                                       "total_tokens": 120,
                                       "prompt_tokens_details": {"cached_tokens": 64}}}),
        {"cached_tokens": 64, "total_tokens": 120},
    ),
    (
        "推理模型的思考 token 要单独记",
        FakeRaw({"input_tokens": 30, "output_tokens": 200, "total_tokens": 230,
                 "output_token_details": {"reasoning": 150}}),
        {"reasoning_tokens": 150, "completion_tokens": 200},
    ),
    (
        "**没有任何用量字段** → 全记 0，绝不按字符数估算",
        FakeRaw(None, {}),
        {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
         "cached_tokens": 0, "reasoning_tokens": 0, "model": None},
    ),
    (
        "根本没拿到原始响应（provider 不支持 include_raw）→ 也记 0",
        None,
        {"prompt_tokens": 0, "total_tokens": 0},
    ),
]

# include_raw 的两种返回形状
RAW_CASES: list[tuple[str, object, tuple]] = [
    ("带 include_raw：拆出原始响应与解析结果",
     {"raw": "RAW-OBJ", "parsed": "PARSED", "parsing_error": None},
     ("RAW-OBJ", "PARSED")),
    ("不带 include_raw：只有解析结果，原始响应是 None",
     "PARSED", (None, "PARSED")),
    ("解析失败：parsed 是 None，原始响应还在（照样能记用量）",
     {"raw": "RAW-OBJ", "parsed": None, "parsing_error": "boom"},
     ("RAW-OBJ", None)),
]

# 接口序列化：账本不同方法给的时间形状不一样，**两种都要能过**
NOW = datetime(2026, 9, 16, 9, 40, 29, tzinfo=timezone.utc)
ISO = NOW.isoformat()

CALL_ROW = {"call_id": 7, "role": "generator", "stage": "第二层·生成 SQL", "round": 0,
            "cursor": 1, "attempt": 2, "model": "deepseek-chat", "prompt_tokens": 1441,
            "completion_tokens": 253, "total_tokens": 1694, "cached_tokens": 512,
            "reasoning_tokens": 0, "duration_ms": 1437, "ok": False, "error": "契约校验失败"}
TASK_ROW = {"sub_task_id": "generator-r0-c0", "role": "generator", "agent_id": "generator",
            "attempt": 1, "status": "completed", "version": 3, "lease_until": None,
            "input_context": {}, "result_ref": "gen", "idempotency_key": "k", "error": None}
AUDIT_ROW = {"sql_id": 12, "round": 0, "sub_task_id": "st-1", "stage": "executed",
             "sql_text": "SELECT 1", "sql_hash": "abc", "intent": "数一下",
             "risk_level": "只读", "action": "SELECT", "tables": ["products"],
             "est_rows": 3, "affected_rows": 3, "need_confirm": False,
             "confirmed_by": None, "confirmed_at": None, "error": None, "duration_ms": 5}

# (说明, 序列化函数, 行, 检查哪个字段)
SERIALIZE_CASES: list[tuple[str, object, dict, str]] = [
    ("消耗明细 · 时间已是字符串（list_llm_calls 会先转好）",
     _call_json, {**CALL_ROW, "created_at": ISO}, "created_at"),
    ("消耗明细 · 时间是原生 datetime（直接来自游标）",
     _call_json, {**CALL_ROW, "created_at": NOW}, "created_at"),
    ("消耗明细 · 时间为空（老行 / 异常行）",
     _call_json, {**CALL_ROW, "created_at": None}, "created_at"),
    ("任务行 · datetime", _task_json, {**TASK_ROW, "created_at": NOW}, "created_at"),
    ("任务行 · 字符串", _task_json, {**TASK_ROW, "created_at": ISO}, "created_at"),
    ("审计行 · datetime", _audit_json, {**AUDIT_ROW, "created_at": NOW}, "created_at"),
    ("审计行 · 字符串", _audit_json, {**AUDIT_ROW, "created_at": ISO}, "created_at"),
]

ISO_CASES: list[tuple[str, object, object]] = [
    ("None → None", None, None),
    ("字符串 → 原样（不能再去调 .isoformat()）", ISO, ISO),
    ("datetime → isoformat 字符串", NOW, ISO),
    ("没有 isoformat 的对象 → 原样返回，不炸", 12345, 12345),
]


def main() -> int:
    bad = 0
    print("用量解析（token 从哪儿读）：\n")
    for label, raw, want in CASES:
        got = usage_of(raw)
        diff = {k: (want[k], got.get(k)) for k in want if got.get(k) != want[k]}
        if diff:
            bad += 1
            print(f"  ✕ {label}")
            for k, (w, g) in diff.items():
                print(f"      {k}: 期望 {w}，实际 {g}")
        else:
            print(f"  ✓ {label}  → {got.get('total_tokens', 0)} token")

    print("\ninclude_raw 的两种形状：\n")
    for label, res, want in RAW_CASES:
        got = split_raw(res)
        if got != want:
            bad += 1
            print(f"  ✕ {label}\n      期望 {want}，实际 {got}")
        else:
            print(f"  ✓ {label}")

    print("\n时间字段（`_iso` 要幂等）：\n")
    for label, given, want in ISO_CASES:
        try:
            got = _iso(given)
        except Exception as exc:  # noqa: BLE001
            got = f"抛异常：{type(exc).__name__}: {exc}"
        if got != want:
            bad += 1
            print(f"  ✕ {label}\n      期望 {want!r}，实际 {got!r}")
        else:
            print(f"  ✓ {label}")

    print("\n接口序列化（时间两种形状都要能吃）：\n")
    for label, fn, row, field in SERIALIZE_CASES:
        try:
            out = fn(dict(row))
            got = out.get(field)
            ok = (got is None and row.get(field) is None) or isinstance(got, str)
            if not ok:
                bad += 1
                print(f"  ✕ {label}：{field} 序列化成了 {type(got).__name__}")
            else:
                print(f"  ✓ {label}  → {field}={got!r}")
        except Exception as exc:  # noqa: BLE001
            bad += 1
            print(f"  ✕ {label}\n      抛异常：{type(exc).__name__}: {exc}")

    total = len(CASES) + len(RAW_CASES) + len(ISO_CASES) + len(SERIALIZE_CASES)
    print(f"\n共 {total} 项，失败 {bad} 项。")
    if bad:
        print("× 消耗统计有问题 —— 数字不对的账单比没有账单更糟")
    else:
        print("✓ 消耗统计符合预期：读得到就读真数，读不到就记 0，绝不估算")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
