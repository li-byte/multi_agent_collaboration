"""Web 链路端到端自检（真实链路，无人为注入）。

三个场景：
  A 只读查询        → 自动执行，审计里能看到 生成→验证→执行
  B 删除 + 确认     → 暂停在 confirming，用户确认后才执行
  C 删除 + 取消     → 暂停后用户取消，**一行都不改**

用法：
    python run_server.py
    python scripts/check_web.py --base http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TERMINAL = {"done", "failed"}


def wait(client, run_id: str, want: set[str], timeout: float = 180.0) -> dict:
    """轮询直到状态落进 want 或超时。"""
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        last = client.get(f"/api/runs/{run_id}").json()
        if last["run"]["status"] in want:
            return last
        time.sleep(0.4)
    return last


def run(client, question: str, decision: bool | None = None) -> dict:
    created = client.post("/api/runs", json={"question": question}).json()
    run_id = created["global_task_id"]
    client.post(f"/api/runs/{run_id}/start")
    detail = wait(client, run_id, TERMINAL | {"confirming"})

    if detail["run"]["status"] == "confirming":
        if decision is None:
            return detail
        answered = client.post(f"/api/runs/{run_id}/confirm",
                               json={"approve": decision, "by": "自检脚本"})
        assert answered.status_code == 202, answered.text
        detail = wait(client, run_id, TERMINAL)
    return detail


def stages(detail: dict) -> list[str]:
    return [a["stage"] for a in detail["audit"]]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--timeout", type=float, default=600.0)
    args = ap.parse_args()
    client = httpx.Client(base_url=args.base, timeout=httpx.Timeout(30.0, read=args.timeout))
    checks: list[tuple[str, bool]] = []

    h = client.get("/api/health").json()
    print(f"[1/6] /api/health → 记录库={h['ledger']}（{h['ledger_db']}）"
          f" 执行库={h['runner']}（{h['biz_db']}，身份 {h['runner_user']}）")
    print(f"      checkpointer={h['checkpointer']}（人工确认依赖它）")
    checks += [("记录库连通", h["ledger"] == "ok"),
               ("受限执行身份可用", h["runner"] == "ok"),
               ("checkpointer 已开启", bool(h["checkpointer"]))]

    # 权限清单：只能有 DML
    privs = {r["table_name"]: r["privs"] for r in (h.get("runner_privileges") or [])}
    only_dml = all(set(p.split(",")) <= {"SELECT", "INSERT", "UPDATE", "DELETE"} for p in privs.values())
    print(f"      受限角色权限：{privs}")
    checks.append(("受限角色只有 DML 权限", bool(privs) and only_dml))

    s = client.get("/api/schema").json()
    print(f"[2/6] /api/schema → {len(s['allowed'])} 张表：{'、'.join(s['allowed'])}")
    checks.append(("schema 快照就是白名单来源", set(s["allowed"]) == set(privs.keys())))

    # ---------- 场景 A ----------
    print("[3/6] 场景 A · 只读查询 → 应自动执行")
    a = run(client, "查一下北京客户的订单总额")
    print(f"      状态={a['run']['status']}  审计阶段={stages(a)}")
    checks += [
        ("A 正常结束", a["run"]["status"] == "done"),
        ("A 审计含 生成→验证→执行", stages(a)[:1] == ["generated"] and
         "validated" in stages(a) and "executed" in stages(a)),
        ("A 执行成功且无错误", all(not x.get("error") for x in a["audit"])),
    ]

    # ---------- 场景 B ----------
    print("[4/6] 场景 B · 删除订单 → 必须暂停等确认，确认后才执行")
    b = run(client, "删掉所有已取消的订单", decision=None)
    paused = b["run"]["status"] == "confirming"
    print(f"      暂停状态={b['run']['status']}  审计阶段={stages(b)}")
    if paused:
        client.post(f"/api/runs/{b['run']['global_task_id']}/confirm",
                    json={"approve": True, "by": "自检脚本"})
        b = wait(client, b["run"]["global_task_id"], TERMINAL)
    print(f"      确认后状态={b['run']['status']}  审计阶段={stages(b)}")
    checks += [
        ("B 删除操作被暂停等确认", paused),
        ("B 审计里留下了 await_confirm", "await_confirm" in stages(b)),
        ("B 确认后有确认人记录", any(x.get("confirmed_by") for x in b["audit"])),
        ("B 最终正常结束", b["run"]["status"] == "done"),
    ]

    # ---------- 场景 C ----------
    print("[5/6] 场景 C · 删除订单 → 用户取消 → 一行都不改")
    c = run(client, "删掉所有已取消的订单", decision=None)
    if c["run"]["status"] == "confirming":
        client.post(f"/api/runs/{c['run']['global_task_id']}/confirm",
                    json={"approve": False, "by": "自检脚本"})
        c = wait(client, c["run"]["global_task_id"], TERMINAL)
    executed = [x for x in c["audit"] if x["stage"] == "executed"]
    print(f"      状态={c['run']['status']}  审计阶段={stages(c)}  执行阶段数={len(executed)}")
    checks += [
        ("C 取消后没有真正执行", len(executed) == 0),
        ("C 最终正常结束（如实说明被取消）", c["run"]["status"] == "done"),
    ]

    # ---------- 汇总 ----------
    print("[6/6] 校验：")
    ok = True
    for name, passed in checks:
        print(f"      {'✓' if passed else '✕'} {name}")
        ok = ok and passed
    print(f"\nWeb 链路自检{'通过。' if ok else '失败。'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
