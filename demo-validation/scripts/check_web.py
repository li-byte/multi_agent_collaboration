"""Web 链路端到端自检。

用两个**真实场景**验证（没有任何人为注入）：

  场景 A  资料覆盖了问题      → 应当一遍通过，所有分点都有原文依据
  场景 B  资料里没有被引用的内容 → 系统必须**显式声明「资料不足」**，而不是编造

用法：
    python run_server.py
    python scripts/check_web.py --base http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.sources import quote_matches  # noqa: E402

TERMINAL = {"done", "failed"}


def iter_sse(response):
    current: dict[str, str] = {}
    for line in response.iter_lines():
        if line == "":
            if current:
                yield current
                current = {}
            continue
        if ":" in line:
            key, value = line.split(":", 1)
            current[key.strip()] = value.strip()
    if current:
        yield current


def _mark(etype: str, d: dict) -> str:
    if etype == "node_end":
        return f" · {d.get('summary', '')}"
    if etype == "handoff":
        return f" · {d.get('label', '')}"
    if etype == "finding":
        return f" · [{'资料不足' if d.get('insufficient') else d.get('status')}] {str(d.get('claim'))[:32]}"
    if etype == "answer":
        ins = sum(1 for p in (d.get("points") or []) if p.get("insufficient"))
        return f" · {len(d.get('points') or [])} 个分点" + (f"（其中 {ins} 个声明资料不足）" if ins else "")
    if etype == "consistency":
        fails = [r["invariant_id"] for r in (d.get("results") or []) if r["result"] == "fail"]
        return " · 全部通过" if d.get("passed") else f" · 失败 {', '.join(fails)}"
    if etype == "review":
        return f" · {d.get('decision', '').upper()}" + ("（运行时强制）" if d.get("forced_by_runtime") else "")
    return ""


def run_scenario(client, question: str, materials: list[str], label: str) -> dict:
    created = client.post("/api/runs", json={"question": question, "materials": materials}).json()
    run_id = created["global_task_id"]
    s: dict = {"label": label, "run_id": run_id, "chunk_count": created["chunk_count"],
               "seen": [], "rounds": set(), "vetoes": 0, "forced": False,
               "cites": [], "handoff_labels": [], "last_passed": None, "all_fails": []}

    print(f"      POST /api/runs → {run_id}（资料切成 {s['chunk_count']} 段）")
    with client.stream("GET", f"/api/runs/{run_id}/stream") as resp:
        started = False
        for raw in iter_sse(resp):
            if not started:
                client.post(f"/api/runs/{run_id}/start")
                started = True
            et = raw.get("event", "")
            pl = json.loads(raw.get("data") or "{}")
            d = pl.get("payload") or {}
            s["seen"].append(et)
            s["rounds"].add(pl.get("round", 0))
            s["cites"].extend(pl.get("citations") or [])
            if et == "handoff" and d.get("label"):
                s["handoff_labels"].append(d["label"])
            if et == "review" and d.get("decision") == "veto":
                s["vetoes"] += 1
                s["forced"] = s["forced"] or bool(d.get("forced_by_runtime"))
            if et == "done":
                s["final_status"] = d.get("status")
            if et == "consistency":
                s["last_passed"] = bool(d.get("passed"))
                s["all_fails"].extend(r["invariant_id"] for r in (d.get("results") or [])
                                      if r["result"] == "fail")
            print(f"      [{pl.get('seq'):>3}] r{pl.get('round')} {pl.get('role', ''):<10} "
                  f"{et:<12}{_mark(et, d)}")
            if et in TERMINAL:
                break

    d = client.get(f"/api/runs/{run_id}").json()
    s["detail"] = d
    content = {c["chunk_id"]: c["content"] for c in d["chunks"]}
    s["bad_cites"] = [c for c in s["cites"]
                      if c.get("source_id") not in content
                      or not quote_matches(c.get("quote", ""), content[c["source_id"]])]
    answers = [e for e in d["events"] if e["event_type"] == "answer"]
    points = (answers[-1]["payload"].get("points") if answers else []) or []
    s["points"] = points
    planned: set[str] = set()
    for e in d["events"]:
        if e["event_type"] == "node_end" and e["role"] == "planner":
            planned = {t["sub_task_id"] for t in (e["payload"].get("sub_tasks") or [])}
    s["planned"] = planned
    s["insufficient"] = sum(1 for p in points if p.get("insufficient"))
    return s


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=float, default=900.0)
    args = parser.parse_args()

    timeout = httpx.Timeout(20.0, read=args.timeout)
    checks: list[tuple[str, bool]] = []

    with httpx.Client(base_url=args.base, timeout=timeout) as client:
        health = client.get("/api/health").json()
        print(f"[1/5] /api/health → pg={health['pg']} llm={health['llm']} "
              f"checkpointer={health['checkpointer']}")

        # 没资料必须被拒绝
        bad = client.post("/api/runs", json={"question": "一个没有资料的问题", "materials": []})
        rejected = bad.status_code == 400 and "need_material" in bad.text
        print(f"[2/5] 无资料建任务 → HTTP {bad.status_code} "
              f"({'正确拒绝，前端据此追问用户' if rejected else '× 期望 400 need_material'})")
        checks.append(("没给资料时被正确拒绝（400 need_material）", rejected))

        samples = client.get("/api/samples").json()
        sample = client.get(f"/api/samples/{samples[0]['id']}").json()

        print(f"[3/5] 场景 A · 资料覆盖问题 → {sample['label']}")
        a = run_scenario(client, sample["question"], [sample["material"]], "A")
        checks += [
            ("A · 四个智能体都执行过", a["seen"].count("node_end") >= 4),
            ("A · 有结论 / 答案 / 校验 / 评审事件",
             all(k in a["seen"] for k in ("finding", "answer", "consistency", "review"))),
            ("A · 交接事件带箭头文案（时序图用）", len(a["handoff_labels"]) >= 2),
            ("A · 所有引用逐字命中资料原文", not a["bad_cites"]),
            ("A · 每个分点都有依据或声明资料不足",
             bool(a["points"]) and all((p.get("citations") or p.get("insufficient")) for p in a["points"])),
            ("A · 答案覆盖了全部子问题",
             bool(a["planned"]) and a["planned"] == {p["sub_task_id"] for p in a["points"]}),
            ("A · 资料充足时没有出现「资料不足」", a["insufficient"] == 0),
            ("A · 最终一致性校验全部通过", a["last_passed"] is True),
            ("A · 最终状态为 done", a.get("final_status") == "done"),
        ]

        print("\n[4/5] 场景 B · 资料里没有被引用的内容（不该编造，应声明资料不足）")
        b = run_scenario(client, "这份资料能回答我的问题吗？", ["嗯。"], "B")
        checks += [
            ("B · 资料答不上时也正常结束（状态 done）", b.get("final_status") == "done"),
            ("B · 没有编造任何引用", not b["cites"] and not b["bad_cites"]),
            ("B · 分点显式声明了「资料不足」", b["insufficient"] > 0),
            ("B · 覆盖了全部子问题",
             bool(b["planned"]) and b["planned"] == {p["sub_task_id"] for p in b["points"]}),
            ("B · 最终一致性校验全部通过", b["last_passed"] is True),
        ]

    print("\n[5/5] 校验：")
    ok = True
    for name, passed in checks:
        print(f"      {'✓' if passed else '✕'} {name}")
        ok = ok and passed

    print(f"\n场景 A：{max(a['rounds'] or {0}) + 1} 轮，引用 {len(a['cites'])} 条"
          f"{'（全部逐字命中）' if not a['bad_cites'] else '（有编造！）'}"
          f"，否决 {a['vetoes']} 次")
    print(f"场景 B：{max(b['rounds'] or {0}) + 1} 轮，引用 {len(b['cites'])} 条"
          f"，声明资料不足 {b['insufficient']} 个分点")
    if a["vetoes"] or b["vetoes"]:
        print("本次运行触发过否决回流（真实行为，非注入）")
    else:
        print("本次运行一遍通过，没有触发否决回流（是否回流取决于模型真实表现）")
    print("Web 链路自检" + ("通过。" if ok else "失败。"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
