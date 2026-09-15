"""演示用的示例问题。

用法：
    python scripts/init_db.py            # 建两个库的表 + 建受限角色
    python scripts/init_db.py --reset    # 先删系统表再建
    python scripts/init_db.py --seed     # 重灌业务库种子数据
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg  # noqa: E402
from psycopg import sql  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import BizDatabase  # noqa: E402
from app.ledger import Ledger, split_statements  # noqa: E402


def ensure_role(settings) -> str:
    """受限角色是**集群级**的，建一次即可（在维护库里建）。

    注意：CREATE ROLE / GRANT 里的角色名、库名不能用参数占位符，
    必须走 psycopg 的 Identifier / Literal 安全拼装。
    """
    with psycopg.connect(settings.maintenance_dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (settings.runner_user,))
            created = cur.fetchone() is None
            if created:
                cur.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(settings.runner_user),
                    sql.Literal(settings.runner_password)))
            cur.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(settings.pg_db_biz),
                sql.Identifier(settings.runner_user)))
            cur.execute("SELECT current_user")
            admin = cur.fetchone()[0]
    return f"{'已创建' if created else '已存在'} 角色 {settings.runner_user}（由 {admin} 授权）"


async def main() -> int:
    settings = get_settings()
    reset = "--reset" in sys.argv
    seed = "--seed" in sys.argv or reset or "--all" in sys.argv

    print(f"配置：项目记录库 = {settings.pg_db_ledger} · 智能体执行库 = {settings.pg_db_biz}")
    print(f"受限执行角色 = {settings.runner_user}\n")

    # ① 建角色（集群级）
    try:
        print(f"[1/4] {ensure_role(settings)}")
    except Exception as exc:  # noqa: BLE001
        print(f"× 创建角色失败：{exc}")
        return 1

    # ② 系统表（agent_sql）
    ledger = Ledger(settings.ledger_dsn)
    try:
        await ledger.open()
    except Exception as exc:  # noqa: BLE001
        print(f"× 无法连接 {settings.pg_db_ledger}：{exc}")
        return 1
    try:
        if reset:
            dropped = await ledger.reset()
            print(f"[2/4] 已删除系统表：{', '.join(dropped)}")
        else:
            print("[2/4] 跳过删除系统表（要重建加 --reset）")
        sql = settings.ledger_schema_path.read_text(encoding="utf-8")
        n = await ledger.apply_schema(sql)
        print(f"      系统表就绪（{n} 条语句）：agent_run / agent_task / agent_event / sql_audit")
    finally:
        await ledger.close()

    # ③ 业务表（cs_v1）+ 授权
    db = BizDatabase(settings.biz_admin_dsn, settings.biz_runner_dsn,
                     settings.statement_timeout_ms)
    await db.open()
    try:
        biz_sql = settings.biz_schema_path.read_text(encoding="utf-8")
        n = await db.apply_script(biz_sql, split_statements)
        print(f"[3/4] 业务表就绪（{n} 条语句）：customers / products / orders / order_items")

        if seed:
            seed_sql = (settings.project_root + "/sql/seed.sql")
            n = await db.apply_script(Path(seed_sql).read_text(encoding="utf-8"), split_statements)
            print(f"      种子数据已重灌（{n} 条语句）")

        identity = await db.runner_identity()
        print(f"\n受限执行身份：{identity['user']} @ {identity['database']}")
        for row in identity["privileges"]:
            print(f"    {row['table_name']:<14} {row['privs']}")
    finally:
        await db.close()

    # ④ checkpoint
    if settings.use_checkpointer:
        try:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
            async with AsyncPostgresSaver.from_conn_string(settings.ledger_dsn) as saver:
                await saver.setup()
            print(f"\n[4/4] LangGraph checkpoint 就绪（{settings.pg_db_ledger}）—— 人工确认依赖它")
        except Exception as exc:  # noqa: BLE001
            print(f"\n[4/4] checkpoint 初始化失败（人工确认会不可用）：{exc}")
    else:
        print("\n[4/4] 已跳过 checkpoint（USE_CHECKPOINTER=off）")

    print("\n初始化完成。下一步：python run_server.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
