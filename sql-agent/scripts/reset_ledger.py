"""只清空**项目自己的账本**（agent_sql），绝不碰 cs_v1 的业务数据与结构。

和 `init_db.py --reset` 的区别：
    init_db.py --reset   → 建角色 + 重建两个库的表 + 灌种子数据（**会动 cs_v1**）
    reset_ledger.py      → 只把 agent_run / agent_task / agent_event / sql_audit /
                            agent_memory / llm_call 清空

用途：把演示用的会话列表归零，但保留业务库里现有的数据。

用法：
    python scripts/reset_ledger.py            # 清空前先报数
    python scripts/reset_ledger.py --yes      # 不问，直接清
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 导入 app 的任何一个子模块都会先执行 app/__init__.py，它会把
# Windows 的事件循环策略设成 SelectorEventLoop（psycopg 异步依赖它），
# 所以这里不需要再单独 `import app`。
from app.config import get_settings  # noqa: E402
from app.ledger import Ledger  # noqa: E402

TABLES = ("llm_call", "agent_memory", "sql_audit", "agent_event", "agent_task",
          "agent_run")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="跳过确认")
    args = ap.parse_args()

    settings = get_settings()
    led = Ledger(settings.ledger_dsn)
    await led.open()
    try:
        before = {}
        for t in TABLES:
            row = await led.fetchone(f"SELECT count(*) AS n FROM {t}")
            before[t] = row["n"] if row else 0
        print("将清空 agent_sql（项目账本）中的：")
        for t in TABLES:
            print(f"  {t:<14} {before[t]:>5} 行")
        print("\n不会触碰 cs_v1 的任何数据与结构。")

        if not args.yes and sys.stdin.isatty():
            if input("\n确认清空？输入 yes 继续：").strip().lower() != "yes":
                print("已取消。")
                return 1
        elif not args.yes:
            print("\n（非交互环境，直接执行；用 --yes 可静默跳过此提示）")

        dropped = await led.reset()
        print(f"\n✓ 已清空：{'、'.join(dropped)}")

        # reset() 是 DROP TABLE，所以必须把账本表重新建回来，
        # 否则清空之后服务起不来（还要等下一次 migrate 才补上）。
        n = await led.apply_schema(settings.ledger_schema_path.read_text(encoding="utf-8"))
        print(f"✓ 已重建账本表：{n} 条语句")
        return 0
    finally:
        await led.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
