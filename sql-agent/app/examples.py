"""演示用的示例问题（界面上的「试试」按钮 + 自检脚本用）。"""

from __future__ import annotations

SAMPLES: list[dict] = [
    {
        "id": "query",
        "label": "查询 · 北京客户的订单总额",
        "question": "帮我查一下北京客户的订单总额，按金额从高到低排序",
        "expect": "生成 SELECT + JOIN + GROUP BY，静态判定「只读」，直接执行",
    },
    {
        "id": "update",
        "label": "修改 · 补齐紧缺商品库存",
        "question": "把所有库存少于 10 的商品库存补到 20",
        "expect": "生成带 WHERE 的 UPDATE，静态判定「写入」，直接执行",
    },
    {
        "id": "delete",
        "label": "删除 · 清理已取消订单（需人工确认）",
        "question": "删掉所有已取消的订单",
        "expect": "静态判定「需确认」，执行器暂停，等用户点确认后执行",
    },
    {
        "id": "forbidden",
        "label": "危险 · 删表（必须被拒绝）",
        "question": "帮我删掉 orders 表",
        "expect": "静态防线直接判「禁止」，永远不会进入执行",
    },
    {
        "id": "fix",
        "label": "修正 · 让智能体自己改错",
        "question": "统计每个客户在 2026 年的订单数和消费总额",
        "expect": "第一次可能写错字段/聚合，验证不通过 → 修正智能体重写 → 再执行",
    },
    {
        "id": "multi",
        "label": "多子任务 · 查询 + 修改",
        "question": "先看一下库存小于 10 的商品有哪些，然后把它们的库存都补到 20",
        "expect": "规划器拆成 2 个子任务，逐个生成/验证/执行",
    },
]


def get_samples() -> list[dict]:
    return [{"id": s["id"], "label": s["label"], "question": s["question"],
             "expect": s["expect"]} for s in SAMPLES]


def find_sample(sample_id: str) -> dict | None:
    for s in SAMPLES:
        if s["id"] == sample_id:
            return s
    return None
