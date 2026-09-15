"""LLM 接入：DeepSeek 走 OpenAI 兼容协议。

关于参数名的兼容处理：
  langchain-openai 各版本对 `api_key` / `openai_api_key`、`base_url` / `openai_api_base`
  的字段别名处理不完全一致。这里同时做两件事，避免踩坑：
    1. 写入标准 OpenAI 环境变量（OPENAI_API_KEY / OPENAI_BASE_URL），SDK 自身会读取；
    2. 仍显式传参；若构造失败，则退回纯环境变量模式。
"""

from __future__ import annotations

import os

from .config import Settings


def build_llm(settings: Settings):
    """返回配置好的 ChatOpenAI；llm_mode=mock 时返回 None。"""
    if settings.llm_mode != "deepseek":
        return None

    if not settings.deepseek_api_key:
        raise RuntimeError(
            "DEEPSEEK_API_KEY 未配置：请在 .env 中填写，或设置 LLM_MODE=mock 使用离线模式"
        )

    # ① 标准 OpenAI 环境变量（langchain-openai / openai SDK 都会读取）
    os.environ.setdefault("OPENAI_API_KEY", settings.deepseek_api_key)
    os.environ.setdefault("OPENAI_BASE_URL", settings.deepseek_base_url)

    from langchain_openai import ChatOpenAI

    common = dict(
        model=settings.deepseek_model,
        temperature=settings.llm_temperature,
        timeout=settings.llm_timeout,
        max_retries=1,
    )

    # ② 优先显式传参
    try:
        return ChatOpenAI(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            **common,
        )
    except Exception:
        # ③ 退回环境变量模式
        return ChatOpenAI(**common)


def structured(llm, schema):
    """把 LLM 绑定到某个结构化输出契约上。"""
    return llm.with_structured_output(schema)
