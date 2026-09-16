"""证明两库隔离 + 权限兜底是真的有效。

这是本项目的核心安全主张，所以要拿实测说话，而不是"设计上应该可以"：

  · cs_v1 里，受限角色能做 DML，做不了任何 DDL
  · agent_sql 里，受限角色连项目自己的系统表都读不到

用法：
    python scripts/check_isolation.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg  # noqa: E402

from app.config import get_settings  # noqa: E402

# (说明, SQL, 期望能否成功)
CS_V1_CASES: list[tuple[str, str, bool]] = [
    ("读业务表",       "SELECT count(*) FROM orders", True),
    ("插入业务数据",   "INSERT INTO customers (name, city) VALUES ('隔离测试', '北京')", True),
    ("修改业务数据",   "UPDATE customers SET level = '普通' WHERE name = '隔离测试'", True),
    ("删除业务数据",   "DELETE FROM customers WHERE name = '隔离测试'", True),
    ("删表",           "DROP TABLE orders", False),
    ("清空表",         "TRUNCATE TABLE orders", False),
    ("改表结构",       "ALTER TABLE orders ADD COLUMN hacker INT", False),
    ("建表",           "CREATE TABLE evil (id INT)", False),
    ("建索引",         "CREATE INDEX evil_idx ON orders(status)", False),
    ("删库",           "DROP DATABASE cs_v1", False),
]

# 这几条实测「不抛异常但也不生效」—— PostgreSQL 对无授权者的 GRANT/REVOKE
# 可能只发 warning。所以不能用「抛不抛异常」判断，必须看**权限有没有变**。
ESCALATION_ATTEMPTS: list[str] = [
    "GRANT SELECT ON orders TO PUBLIC",
    "GRANT ALL ON orders TO PUBLIC",
    "GRANT SELECT ON orders TO postgres",
    "REVOKE SELECT ON orders FROM agent_sql_runner",
    "ALTER TABLE orders OWNER TO agent_sql_runner",
]

AGENT_SQL_CASES: list[tuple[str, str, bool]] = [
    ("读项目系统表",   "SELECT * FROM agent_run", False),
    ("读消耗明细表",   "SELECT * FROM llm_call", False),
    ("改项目系统表",   "UPDATE agent_task SET status = 'hacked'", False),
    ("删审计表",       "DROP TABLE sql_audit", False),
]


def _try(cur, sql: str) -> tuple[bool, str]:
    try:
        cur.execute(sql)
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, str(exc).strip().splitlines()[0][:90]


def run_cases(dsn: str, cases, title: str) -> int:
    print(f"\n【{title}】")
    bad = 0
    with psycopg.connect(dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT current_user AS u, current_database() AS d")
            user, db = cur.fetchone()
            print(f"  身份：{user} @ {db}")
            for label, sql, want_ok in cases:
                ok, err = _try(cur, sql)
                good = ok == want_ok
                if not good:
                    bad += 1
                mark = "✓" if good else "✕"
                allow = "允许" if want_ok else "拒绝"
                got = "成功" if ok else "被拒"
                tail = "" if ok else f"  ← {err}"
                print(f"  {mark} {label:<12} 期望{allow} 实际{got}{tail}")
    return bad


def privilege_snapshot(dsn: str) -> list:
    """从 admin 侧看真实授权情况（受限角色自己看不到别人的授权）。"""
    with psycopg.connect(dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT grantee, table_name, privilege_type
                  FROM information_schema.role_table_grants
                 WHERE table_schema = 'public'
                 ORDER BY 1, 2, 3
            """)
            return cur.fetchall()


def check_escalation(s) -> int:
    """试各种提权手段，断言**权限快照一模一样**。"""
    print(f"\n【提权尝试（{s.pg_db_biz}）】")
    before = privilege_snapshot(s.biz_admin_dsn)
    with psycopg.connect(s.biz_runner_dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            for sql in ESCALATION_ATTEMPTS:
                try:
                    cur.execute(sql)
                    print(f"  · 执行未报错：{sql}")
                except Exception as exc:  # noqa: BLE001
                    print(f"  · 被拒：{sql}  ← {str(exc).strip().splitlines()[0][:60]}")
    after = privilege_snapshot(s.biz_admin_dsn)
    if before == after:
        print("  ✓ 权限快照前后完全一致 —— 提权没有生效")
        return 0
    print("  ✕ 权限被改动了！")
    for row in set(after) - set(before):
        print(f"      新增：{row}")
    for row in set(before) - set(after):
        print(f"      丢失：{row}")
    return 1


def main() -> int:
    s = get_settings()
    print("两库隔离 & 权限兜底 实测")
    print(f"  受限角色：{s.runner_user}")

    bad = run_cases(s.biz_runner_dsn, CS_V1_CASES, f"{s.pg_db_biz}（智能体执行库）")
    bad += check_escalation(s)
    bad += run_cases(s.runner_ledger_dsn, AGENT_SQL_CASES,
                     f"{s.pg_db_ledger}（项目记录库）")

    print()
    if bad:
        print(f"× 有 {bad} 项与预期不符 —— 隔离没做到位，必须修")
        return 1
    print("✓ 全部符合预期：")
    print("    · 业务库：能查能改能删，**做不了任何 DDL**，也改不动授权")
    print("    · 记录库：连项目自己的系统表都读不到")
    print("  → 即使应用层的 sql_guard 被绕过，数据库层也执行不了危险操作")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
