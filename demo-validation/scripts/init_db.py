"""初始化数据库：建自建表 + LangGraph checkpoint 表。

用法：
    python scripts/init_db.py            # 建表（表已存在则不动）
    python scripts/init_db.py --reset    # 先删掉自建表再建（结构变更后用）
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.ledger import Ledger  # noqa: E402


async def main() -> int:
    settings = get_settings()
    reset = "--reset" in sys.argv
    print(f"目标库：{settings.dsn.replace(settings.pg_password, '***')}")

    ledger = Ledger(settings.dsn)
    try:
        await ledger.open()
    except Exception as exc:  # noqa: BLE001
        print(f"× 无法连接 PostgreSQL：{exc}")
        print("  请检查 .env 中的 PG_HOST / PG_PORT / PG_USER / PG_PASSWORD / PG_DB")
        return 1

    try:
        if reset:
            dropped = await ledger.reset()
            print(f"[0/2] 已删除旧表：{', '.join(dropped)}")

        sql = settings.schema_path.read_text(encoding="utf-8")
        count = await ledger.apply_schema(sql)
        print(f"[1/2] 自建表就绪（执行 {count} 条语句）")
        print("      agent_run / source_chunk / agent_task / finding / agent_event")

        if settings.use_checkpointer:
            try:
                from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

                async with AsyncPostgresSaver.from_conn_string(settings.dsn) as saver:
                    await saver.setup()
                print("[2/2] LangGraph checkpoint 表就绪")
            except Exception as exc:  # noqa: BLE001
                print(f"[2/2] checkpoint 初始化失败（不影响使用）：{exc}")
        else:
            print("[2/2] 已跳过 checkpoint（USE_CHECKPOINTER=off）")
    finally:
        await ledger.close()

    print("\n初始化完成。下一步：python run_server.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
