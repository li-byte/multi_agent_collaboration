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
        "expect": "第一次可能写错字段/聚合，校验不通过 → 修正智能体重写 → 再执行",
    },
    {
        "id": "multi",
        "label": "多子任务 · 查询 + 修改",
        "question": "先看一下库存小于 10 的商品有哪些，然后把它们的库存都补到 20",
        "expect": "规划器拆成 2 个子任务（第二步依赖第一步）；第一步查完写进共享记忆，"
                  "第二步的 UPDATE 直接用查出来的商品 id，而不是自己另写一遍条件",
    },
    {
        "id": "multi3",
        "label": "三步链路 · 每步都复查（看连续性）",
        "question": "先找出库存小于 10 的商品，再看这些商品都有哪些订单，最后统计涉及多少客户",
        "expect": "拆成 3 个有依赖的子任务；每做完一个回规划器复查一次（continue/revise/finish）；"
                  "每一步都能拿到上一步的具体值 —— 界面上看得到「🧠 写入共享记忆」和「🔁 复查」",
    },
    # ---- 下面三个演示「意图识别」与「危险评估」----
    {
        "id": "chat",
        "label": "意图 · 打招呼（不碰数据库）",
        "question": "你好，你能做什么？",
        "expect": "规划器判 mode=chat，直接回一句话，不进生成/执行，更不会报「处理了 0 个操作」",
    },
    {
        "id": "danger_reason",
        "label": "越界 · 改表结构（拒绝 + 给替代）",
        "question": "帮我给 customers 表加一个字段，再把它清空",
        "expect": "规划器判越界（DDL），拒绝并给出「改成删行」这类替代做法",
    },
    {
        "id": "bad_column",
        "label": "按表信息核对 · 问一个不存在的字段",
        "question": "帮我查一下客户的昵称",
        "expect": "生成器只会用目录里的字段；若写出目录外的字段，校验器按表信息拦下 → 修正器重写，不落库",
    },
]


def get_samples() -> list[dict]:
    return [{"id": s["id"], "label": s["label"], "question": s["question"],
             "expect": s["expect"]} for s in SAMPLES]
