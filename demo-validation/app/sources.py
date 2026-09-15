"""资料处理：把用户贴的原文切成可引用的片段，并提供「逐字命中」校验。

这是整个通用版一致性的着力点：
  · 智能体只能引用用户给的资料；
  · 每条引用必须给出**原文摘录**；
  · 运行时把摘录拿去资料里做**字符串匹配** —— 找得到才算数。

于是「引用依据必须真实存在」从一句口号变成了一条**确定性可检查**的规则，
不需要任何领域知识，健康、代码、任何行业都适用。
"""

from __future__ import annotations

import re

MAX_CHUNK = 520      # 单段上限，超了就按句子再切
MIN_QUOTE = 6        # 摘录至少 6 个非空白字符，避免 "的" 这种无意义引用


def split_material(text: str) -> list[str]:
    """把用户贴的原文按空行切段；过长的段落再按句号切。返回片段列表。"""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    for para in paragraphs:
        if len(para) <= MAX_CHUNK:
            chunks.append(para)
            continue
        # 太长：按句末标点切
        pieces = re.split(r"(?<=[。！？!?；;])\s*", para)
        buf = ""
        for piece in pieces:
            if buf and len(buf) + len(piece) > MAX_CHUNK:
                chunks.append(buf.strip())
                buf = piece
            else:
                buf += piece
        if buf.strip():
            chunks.append(buf.strip())
    return [c for c in chunks if c.strip()]


def build_chunks(materials: list[str], limit: int = 60) -> list[dict]:
    """materials 是用户依次贴进来的多份资料。

    返回 [{chunk_id, doc_no, seq, content, char_len}]，chunk_id 形如 D1-2。
    """
    out: list[dict] = []
    for doc_no, mat in enumerate(materials, start=1):
        for seq, content in enumerate(split_material(mat), start=1):
            out.append({
                "chunk_id": f"D{doc_no}-{seq}",
                "doc_no": doc_no,
                "seq": seq,
                "content": content,
                "char_len": len(content),
            })
            if len(out) >= limit:
                return out
    return out


def normalize(text: str) -> str:
    """去掉所有空白后再比对，避免换行/缩进差异造成误判。"""
    return re.sub(r"\s+", "", text or "")


def quote_matches(quote: str, content: str) -> bool:
    """摘录是否能在该片段里逐字找到。"""
    q = normalize(quote)
    if len(q) < MIN_QUOTE:
        return False
    return q in normalize(content)


def locate(quote: str, content: str) -> tuple[int, int] | None:
    """返回摘录在原文中的位置（用于界面上高亮），找不到返回 None。"""
    q = (quote or "").strip()
    if not q:
        return None
    idx = content.find(q)
    if idx >= 0:
        return idx, idx + len(q)
    # 退一步：忽略空白后定位
    flat, pos = normalize(content), 0
    mapping = []
    for ch in content:
        if not ch.isspace():
            mapping.append(pos)
        pos += 1
    i = flat.find(normalize(q))
    if i < 0 or i >= len(mapping):
        return None
    start = mapping[i]
    end = mapping[min(i + len(normalize(q)) - 1, len(mapping) - 1)]
    return start, end + 1
