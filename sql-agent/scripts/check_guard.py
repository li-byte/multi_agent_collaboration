"""SQL 静态防线自检（对抗性用例）。

用法：
    python scripts/check_guard.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.sql_guard import analyze  # noqa: E402

TABLES = {"customers", "products", "orders", "order_items"}

# (SQL, 期望等级, 期望类型)
CASES: list[tuple[str, str, str]] = [
    # ---------- 正常放行 ----------
    ("SELECT id, name FROM customers WHERE city = '北京'", "只读", "SELECT"),
    ("SELECT c.name, SUM(o.total_amount) FROM customers c "
     "JOIN orders o ON o.customer_id = c.id GROUP BY c.name", "只读", "SELECT"),
    ("SELECT * FROM orders LIMIT 10", "只读", "SELECT"),
    ("INSERT INTO customers (name, city) VALUES ('测试', '北京')", "写入", "INSERT"),
    ("UPDATE products SET stock = 0 WHERE stock < 10", "写入", "UPDATE"),
    ("UPDATE orders SET status = '已完成' WHERE id = 3", "写入", "UPDATE"),
    ("INSERT INTO orders (customer_id, status) VALUES (1, '待付款') "
     "ON CONFLICT DO NOTHING", "写入", "INSERT"),   # DO NOTHING 不能误报
    ("INSERT INTO orders (customer_id, status) VALUES (1, 'x') "
     "ON CONFLICT (id) DO UPDATE SET status = 'y'", "写入", "INSERT"),  # DO UPDATE 不能误报
    ("DELETE FROM orders WHERE status = '已取消'", "需确认", "DELETE"),

    # ---------- 需确认（不是禁止）----------
    ("DELETE FROM orders", "需确认", "DELETE"),                       # 无 WHERE
    ("UPDATE orders SET status = 'x'", "需确认", "UPDATE"),           # 无 WHERE
    ("DELETE FROM order_items WHERE order_id IN (SELECT id FROM orders WHERE status='已取消')",
     "需确认", "DELETE"),
    # 顶层有 WHERE → 放行（哪怕子查询里也有 WHERE）
    ("UPDATE orders SET status = 'x' WHERE id IN (SELECT id FROM orders WHERE status='已取消')",
     "写入", "UPDATE"),
    # 只有子查询里有 WHERE，顶层没有 → 仍是全表更新，必须确认
    ("UPDATE products SET stock = (SELECT count(*) FROM orders)", "需确认", "UPDATE"),

    # ---------- 一律禁止：DDL ----------
    ("DROP TABLE orders", "禁止", "?"),
    ("DROP DATABASE cs_v1", "禁止", "?"),
    ("TRUNCATE TABLE orders", "禁止", "?"),
    ("ALTER TABLE orders ADD COLUMN x INT", "禁止", "?"),
    ("CREATE TABLE evil (id INT)", "禁止", "?"),
    ("CREATE INDEX idx ON orders(status)", "禁止", "?"),
    ("GRANT ALL ON orders TO agent_sql_runner", "禁止", "?"),
    ("REVOKE SELECT ON orders FROM agent_sql_runner", "禁止", "?"),
    ("COMMENT ON TABLE orders IS 'x'", "禁止", "?"),

    # ---------- 一律禁止：多语句 / 事务 ----------
    ("SELECT 1; DROP TABLE orders", "禁止", "?"),
    ("DELETE FROM orders WHERE id=1; DELETE FROM orders WHERE id=2", "禁止", "?"),
    ("BEGIN; DELETE FROM orders; COMMIT", "禁止", "?"),

    # ---------- 一律禁止：注释/大小写绕过（抹掉注释后必须还能抓到）----------
    ("DROP/**/TABLE orders", "禁止", "?"),
    ("dRoP tAbLe orders", "禁止", "?"),
    ("SELECT 1 -- \n; DROP TABLE orders", "禁止", "?"),

    # ---------- 一律禁止：文件 / 代码 / 危险函数 ----------
    ("SELECT pg_read_file('/etc/passwd')", "禁止", "SELECT"),
    ("SELECT lo_import('/etc/passwd')", "禁止", "SELECT"),
    ("DO $$ BEGIN DELETE FROM orders; END $$", "禁止", "?"),
    ("COPY orders TO '/tmp/x.csv'", "禁止", "?"),

    # ---------- 一律禁止：SELECT INTO 建表 ----------
    ("SELECT * INTO evil FROM orders", "禁止", "SELECT"),

    # ---------- 一律禁止：越权访问别的表 ----------
    ("SELECT * FROM agent_run", "禁止", "SELECT"),
    ("SELECT * FROM pg_shadow", "禁止", "SELECT"),
    ("UPDATE agent_run SET status = 'done'", "禁止", "UPDATE"),

    # ---------- 不误报：字符串里的危险词 ----------
    ("SELECT * FROM orders WHERE status = 'DROP TABLE orders'", "只读", "SELECT"),
    ("INSERT INTO customers (name, city) VALUES ('DELETE FROM orders', '北京')", "写入", "INSERT"),
]


def main() -> int:
    print("SQL 静态防线自检：\n")
    bad = 0
    for sql, want_level, want_action in CASES:
        v = analyze(sql, TABLES)
        ok = (v.level == want_level) and (want_action == "?" or v.action == want_action)
        if not ok:
            bad += 1
            print(f"  ✕ {sql[:66]}")
            print(f"      期望 {want_level}/{want_action}  实际 {v.level}/{v.action}  {v.reasons}")
        else:
            flag = {"禁止": "🚫", "需确认": "⏸", "写入": "✎", "只读": "✓"}[v.level]
            extra = f"  ← {v.reasons[0]}" if v.reasons else ""
            print(f"  {flag} {v.level:<3} {v.action:<7} {sql[:58]}{extra}")

    print(f"\n共 {len(CASES)} 项，失败 {bad} 项。")
    if bad:
        print("× 静态防线存在漏洞，必须修完再往下做")
    else:
        print("✓ 静态防线通过：危险语句全拦截，正常语句不误伤")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
