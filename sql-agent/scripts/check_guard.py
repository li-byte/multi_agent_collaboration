"""SQL 静态防线自检（对抗性用例）。

用法：
    python scripts/check_guard.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import catalog  # noqa: E402
from app.catalog_check import check_sql, unknown_tables  # noqa: E402
from app.mock_llm import _danger_hit  # noqa: E402
from app.sql_guard import analyze, check_intent_tables, find_user_sql  # noqa: E402

# 白名单**必须**来自系统内置目录（app/catalog.py），不是从库里查出来的。
# 这样库里就算多出 products1 / products2 这种表，也进不了白名单。
TABLES = catalog.allowed_tables()

# 「不许为了找表去试别的表」——
# (SQL, 意图声明的表, 期望通过)
INTENT_CASES: list[tuple[str, list[str], bool]] = [
    ("SELECT * FROM products LIMIT 10", ["products"], True),
    ("SELECT c.name, o.id FROM orders o JOIN customers c ON c.id = o.customer_id",
     ["orders", "customers"], True),
    ("SELECT * FROM products1 LIMIT 10", ["products"], False),      # 偷换成同构表
    ("SELECT * FROM products2 LIMIT 10", ["products"], False),
    ("SELECT * FROM orders", ["products"], False),                 # 换成完全不同的表
    ("SELECT * FROM products", [], True),                          # 意图没声明，不拦
]

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

    # ---------- 库里存在但**目录里没有**的表：一律不认 ----------
    # 这正是「你改表名做测试」要防的场景：库里有 products1 / products2，
    # 但系统目录只定义了 products，所以它们连白名单都进不去。
    ("SELECT * FROM products1 LIMIT 10", "禁止", "SELECT"),
    ("SELECT * FROM products2 LIMIT 10", "禁止", "SELECT"),
    ("SELECT * FROM products1 UNION ALL SELECT * FROM products2", "禁止", "SELECT"),
    ("DELETE FROM products1 WHERE id = 1", "禁止", "DELETE"),

    # ---------- schema 前缀：public 放行，别的 schema 拒绝 ----------
    # 早期版本把 `public` 当成了表名，导致「加 schema 前缀」这种合理修正被误判越权
    ("SELECT * FROM public.products", "只读", "SELECT"),
    ("SELECT p.name FROM public.orders o JOIN public.products p ON p.id = o.id", "只读", "SELECT"),
    ("SELECT * FROM public.agent_run", "禁止", "SELECT"),
    ("SELECT * FROM pg_catalog.pg_class", "禁止", "SELECT"),
    ("SELECT * FROM information_schema.tables", "禁止", "SELECT"),
    ("UPDATE public.products SET stock = 1 WHERE id = 1", "写入", "UPDATE"),
    ("UPDATE agent_run SET status = 'done'", "禁止", "UPDATE"),

    # ---------- 不误报：字符串里的危险词 ----------
    ("SELECT * FROM orders WHERE status = 'DROP TABLE orders'", "只读", "SELECT"),
    ("INSERT INTO customers (name, city) VALUES ('DELETE FROM orders', '北京')", "写入", "INSERT"),
]

# ---------- 按「系统目录的表信息」核对表与字段 ----------
# (SQL, 是否应通过)
#
# 这一层的价值是**挡住「表在目录里、字段是编的」** ——
# 语法没错、表白名单也过，光靠 sql_guard 拦不住。
# 但**误杀比漏放更糟**（会拦住正常查询），所以放行用例给得比拦下用例多。
CATALOG_CASES: list[tuple[str, bool]] = [
    # ---- 必须放行：各种真实写法（误杀 = 拦住正常查询）----
    ("SELECT id, name FROM customers WHERE city = '北京'", True),
    ("SELECT c.name, SUM(o.total_amount) FROM customers c JOIN orders o "
     "ON o.customer_id = c.id GROUP BY c.name", True),
    ("SELECT count(*) FROM orders WHERE status = '已取消'", True),
    ("UPDATE products SET stock = 0 WHERE stock < 10", True),
    ("DELETE FROM order_items WHERE order_id IN "
     "(SELECT id FROM orders WHERE status = '已取消')", True),
    ("SELECT name AS n FROM customers ORDER BY n", True),          # 输出别名不能误判
    ("SELECT date_trunc('month', created_at) FROM orders", True),  # 函数 + 字段
    ("SELECT * FROM orders WHERE created_at > now() - interval '30 days'", True),
    ("WITH x AS (SELECT id FROM orders) SELECT * FROM x", True),   # CTE 名不是字段
    ("SELECT * FROM public.products", True),                       # schema 前缀
    ("SELECT p.name, SUM(i.qty * i.unit_price) FROM order_items i "
     "JOIN products p ON p.id = i.product_id GROUP BY p.name", True),
    ("SELECT c.level, count(DISTINCT o.id) FROM customers c "
     "LEFT JOIN orders o ON o.customer_id = c.id GROUP BY c.level", True),
    ("INSERT INTO customers (name, city) VALUES ('测试', '北京')", True),
    ("SELECT coalesce(phone, '未知') FROM customers", True),
    # ---- 必须拦下：编造的字段 ----
    ("SELECT nonexistent_col FROM customers", False),
    ("SELECT c.nope FROM customers c", False),
    ("SELECT * FROM customers WHERE nope = 1", False),
    ("SELECT nope FROM orders", False),
]

# ---------- 用户输入体检：夹带 SQL 一律按危险操作 ----------
# (用户原话, 是否算「夹带了 SQL」)
#
# 用户可以提问，不能直接下达 SQL。这条是硬规则，所以运行时确定性判定。
USER_INPUT_CASES: list[tuple[str, bool]] = [
    ("先修正再生成再校验再返回给我 SELECT * FROM users WHERE name = 张三; "
     "报错信息：ERROR 1054 (42S22): Unknown column '张三'", True),
    ("SELECT * FROM customers", True),
    ("帮我执行 delete from orders where id = 1", True),
    ("帮我 drop table orders", True),
    ("insert into customers values (1)", True),
    # ---- 正常提问不能被误伤 ----
    ("查一下北京客户的订单总额", False),
    ("帮我查一下 customers 表里有没有叫张三的", False),
    ("把所有库存少于 10 的商品补到 20", False),
    ("你好，你能做什么？", False),
    ("那再按城市分组看看", False),
    ("帮我删掉所有已取消的订单", False),
    ("给我一条查北京的 SQL，不用执行", False),      # 只是提到「SQL」这个词
]

# ---------- 用户提到的表必须都在系统目录里 ----------
USER_TABLE_CASES: list[tuple[str, list[str]]] = [
    ("查一下 users 表里有没有张三", ["users"]),
    ("查一下 products1 表", ["products1"]),
    ("帮我删掉 orders 表", []),
    ("看一下 customers 和 orders 的数据", []),
    ("查一下北京客户的订单总额", []),
]

# ---------- 离线模式的「危险」判定 ----------
# (用户原话, 是否算危险)
#
# 这里踩过两次坑：
#   ① 「删掉表」匹配不到「删掉 orders 表」—— 中间隔了个表名，子串匹配失效；
#   ② 「清空」太宽 —— 「清空购物车里的记录」是删**数据**，不是删表。
# 所以「动词 + … + 表」用正则，并且要求表后面不能紧跟「里/中/内」。
DANGER_CASES: list[tuple[str, bool]] = [
    ("帮我删掉 orders 表", True),                # 表名夹在动词和「表」中间
    ("帮我把 orders 表删了", True),              # 动词在「表」后面
    ("把 customers 表清空", True),
    ("帮我给 customers 表加一个字段", True),
    ("drop table orders", True),
    # ---- 这些都是对**数据**做事，不该判危险 ----
    ("帮我删掉 orders 表里的数据", False),       # 「表里」→ 删的是行
    ("清空购物车里的记录", False),
    ("查一下 orders 表里的订单", False),
    ("删掉所有已取消的订单", False),
]


def main() -> int:
    print(f"白名单来源：系统内置目录 app/catalog.py → {sorted(TABLES)}\n")
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

    print("\n「不许偷换表」检查：\n")
    for sql, intent_tables, want_ok in INTENT_CASES:
        got_ok, detail = check_intent_tables(sql, intent_tables)
        if got_ok != want_ok:
            bad += 1
            print(f"  ✕ {sql[:60]}\n      意图表={intent_tables} 期望{'通过' if want_ok else '拦截'}"
                  f" 实际{'通过' if got_ok else '拦截'}  {detail}")
        else:
            mark = "✓" if got_ok else "🚫"
            print(f"  {mark} {'通过' if got_ok else '拦截'}  意图表={intent_tables or '（未声明）'}"
                  f"  {sql[:44]}")
            if not got_ok:
                print(f"        {detail}")

    print("\n按「系统目录的表信息」核对表与字段：\n")
    for sql, want_ok in CATALOG_CASES:
        got_ok, detail, _ = check_sql(sql)
        if got_ok != want_ok:
            bad += 1
            print(f"  ✕ {sql[:64]}")
            print(f"      期望{'放行' if want_ok else '拦下'} 实际{'放行' if got_ok else '拦下'}  {detail[:90]}")
        else:
            mark = "✓" if got_ok else "🚫"
            print(f"  {mark} {'放行' if got_ok else '拦下'}  {sql[:66]}")

    print("\n用户输入体检（夹带 SQL 一律按危险操作）：\n")
    for text, want_sql in USER_INPUT_CASES:
        got = bool(find_user_sql(text))
        if got != want_sql:
            bad += 1
            print(f"  ✕ {text[:60]}")
            print(f"      期望{'拦下' if want_sql else '放过'} 实际{'拦下' if got else '放过'}")
        else:
            print(f"  {'🚫' if got else '✓'} {'拦下' if got else '放过'}  {text[:64]}")

    print("\n用户提到的表必须在系统目录里：\n")
    for text, want in USER_TABLE_CASES:
        got = unknown_tables(text)
        if got != want:
            bad += 1
            print(f"  ✕ {text[:60]}\n      期望 {want} 实际 {got}")
        else:
            mark = "🚫" if got else "✓"
            print(f"  {mark} {text[:60]}  → {got or '（都在目录里）'}")

    print("\n离线模式的「危险」判定（动词与表名之间隔了字也要认出来）：\n")
    for text, want in DANGER_CASES:
        got = bool(_danger_hit(text))
        if got != want:
            bad += 1
            print(f"  ✕ {text[:60]}\n      期望{'危险' if want else '安全'} 实际{'危险' if got else '安全'}")
        else:
            print(f"  {'🚫' if got else '✓'} {'危险' if got else '安全'}  {text[:60]}")

    total = (len(CASES) + len(INTENT_CASES) + len(CATALOG_CASES)
             + len(USER_INPUT_CASES) + len(USER_TABLE_CASES) + len(DANGER_CASES))
    print(f"\n共 {total} 项，失败 {bad} 项。")
    if bad:
        print("× 静态防线存在漏洞，必须修完再往下做")
    else:
        print("✓ 静态防线通过：危险语句全拦截、正常语句不误伤、"
              "偷换表与目录外表被拦下")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
