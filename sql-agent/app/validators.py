"""运行时一致性不变量（确定性推导，不看模型脸色）。

沿用冻结项目的思路，但换成本场景该守的边界：

  S1  每条 SQL 都要有意图说明，且对应到某个子任务
  S2  SQL 涉及的表必须在**授权范围内**（来自受限角色的 schema 快照）
  S3  SQL 必须经过校验才能执行（不许跳过）
  S4  静态防线判定禁止的，永远不进执行
  S5  需确认的操作，必须有**用户确认记录**才能执行
  S6  执行过的 SQL 必须留下结果（成功或失败都要有）
  S7  每个子任务都要有对应的 SQL（覆盖率）
  S8  所有执行记录都带幂等键（防「确认后重复执行」）

只要有一条 fail，汇总审查器不得 approve —— 硬门禁，模型说了不算。
"""

from __future__ import annotations

from .router import MODE_CN
from .state import ConsistencyReport, GuardResult

TITLE: dict[str, str] = {
    "S1_sql_has_intent": "每条 SQL 都要有意图说明并对应到子任务",
    "S2_tables_authorized": "SQL 只能访问被授权的表",
    "S3_validated_before_exec": "SQL 必须经过校验才能执行",
    "S4_forbidden_never_executed": "静态判定禁止的语句永远不能被执行",
    "S5_confirm_recorded": "需确认的操作必须有用户确认记录",
    "S6_exec_result_recorded": "执行过的 SQL 必须有结果留痕",
    "S7_intent_coverage": "每个子任务都要有对应的 SQL",
    "S8_idempotent_side_effect": "所有执行记录都要带幂等键",
}

ORDER = list(TITLE.keys())


def _r(gid: str, ok: bool, detail: str = "", offenders: list[str] | None = None) -> GuardResult:
    return GuardResult(guard_id=gid, title=TITLE[gid], result="pass" if ok else "fail",
                       detail=detail, offenders=list(offenders or []))


def validate(
    *,
    allowed_tables: set[str],
    intents: list[dict],
    history: list[dict],
    tasks: list[dict],
    version: int,
    round_no: int,
    refusal: str = "",
    mode: str = "execute",
    blocked: str = "",
) -> ConsistencyReport:
    results: list[GuardResult] = []

    # 「不执行」是用户明确要求的模式，不是漏做 —— 覆盖率要按**该模式的要求**来判，
    # 否则系统会被自己的不变量判失败，那就荒唐了。
    executes = (mode or "execute") == "execute"

    # ---- S1 每条 SQL 都要有意图说明 ----
    # 注意：**没有 SQL 时这条是空真**（vacuously true）。
    # 之前的写法 `not bool(history) is False and not bad1` 在 history 为空时算出 False，
    # 于是「打招呼」和「被拒绝」这两种本来就没有 SQL 的情况被自己判成了失败 —— 已修。
    bad1 = [h.get("sub_task_id", "?") for h in history
            if not (h.get("intent") or "").strip() or not h.get("sub_task_id")]
    results.append(_r("S1_sql_has_intent", not bad1,
                      f"{len(history)} 条 SQL 都带了意图说明" if history and not bad1
                      else ("没有 SQL 需要检查" if not history
                            else f"这些 SQL 缺意图说明：{', '.join(bad1)}"), bad1))

    # ---- S2 表必须在授权范围内 ----
    offenders: list[str] = []
    for h in history:
        for t in (h.get("tables") or []):
            if t.lower() not in {x.lower() for x in allowed_tables}:
                offenders.append(f"{h.get('sub_task_id')} → {t}")
    results.append(_r("S2_tables_authorized", not offenders,
                      "SQL 只访问了被授权的表" if not offenders
                      else f"访问了未授权的表：{'；'.join(offenders)}", offenders))

    # ---- S3 必须校验过才能执行 ----
    unvalidated = [h.get("sub_task_id", "?") for h in history
                   if h.get("executed") and
                   (not h.get("validated") or h.get("validated_passed") is not True)]
    results.append(_r("S3_validated_before_exec", not unvalidated,
                      "所有执行过的 SQL 都经过校验" if not unvalidated
                      else f"这些 SQL 没校验就执行了：{', '.join(unvalidated)}", unvalidated))

    # ---- S4 禁止语句永不执行 ----
    executed_forbidden = [h.get("sub_task_id", "?") for h in history
                          if h.get("executed") and h.get("risk_level") == "禁止"]
    results.append(_r("S4_forbidden_never_executed", not executed_forbidden,
                      "没有任何禁止级语句被执行" if not executed_forbidden
                      else f"禁止级语句被执行了：{', '.join(executed_forbidden)}", executed_forbidden))

    # ---- S5 需确认的必须有确认记录 ----
    unconfirmed = [h.get("sub_task_id", "?") for h in history
                   if h.get("executed") and h.get("need_confirm") and not h.get("confirmed_by")]
    results.append(_r("S5_confirm_recorded", not unconfirmed,
                      "需确认的操作都有用户确认记录" if not unconfirmed
                      else f"未确认就执行了：{', '.join(unconfirmed)}", unconfirmed))

    # ---- S6 执行过就要有结果留痕 ----
    lost = [h.get("sub_task_id", "?") for h in history
            if h.get("executed") and h.get("exec_ok") is None]
    results.append(_r("S6_exec_result_recorded", not lost,
                      "执行结果都落了账" if not lost else f"缺结果留痕：{', '.join(lost)}", lost))

    # ---- S7 子任务覆盖率 ----
    planned = [t.get("sub_task_id", "?") for t in intents]
    if not executes:
        # chat（聊天）模式本来就**不该**执行任何东西。
        # 这里反过来查「有没有偷偷执行」—— 那才是真正的不一致。
        sneaked = [h.get("sub_task_id", "?") for h in history if h.get("executed")]
        results.append(_r("S7_intent_coverage", not sneaked,
                          f"模式为 {MODE_CN.get(mode, mode)}，本就不该执行任何语句，"
                          f"确认确实一条都没执行"
                          if not sneaked
                          else f"用户明确说了不执行，却有语句被执行了：{', '.join(sneaked)}",
                          sneaked))
    else:
        done = {h.get("sub_task_id") for h in history
                if h.get("executed") and h.get("exec_ok") is True
                and h.get("kind") != "cancelled"}
        missing = [sid for sid in planned if sid not in done]
        if refusal:
            # 规划器判定请求超出允许范围 —— 本来就不该有子任务，这是正确行为
            results.append(_r("S7_intent_coverage", True,
                              "规划器判定请求超出允许范围并拒绝，未产生任何数据操作（预期行为）"))
        elif blocked:
            # 静态防线**主动拦下**了这条 SQL（越权表 / 禁止语句）——
            # 没执行正是它该有的结果。再判一次「没落地」等于自己跟自己打架，
            # 还会把报告写得自相矛盾（一边说拦得对，一边说子任务全没落地）。
            results.append(_r("S7_intent_coverage", True,
                              f"静态防线主动拦下，未执行是预期行为：{blocked[:120]}"))
        elif not planned:
            results.append(_r("S7_intent_coverage", False, "规划器没有拆出任何子任务"))
        else:
            results.append(_r("S7_intent_coverage", not missing,
                              f"{len(planned)} 个子任务全部执行成功"
                              if not missing else f"这些子任务没有落地：{', '.join(missing)}", missing))

    # ---- S8 幂等键 ----
    no_key = [t.get("sub_task_id", "?") for t in tasks if not t.get("idempotency_key")]
    results.append(_r("S8_idempotent_side_effect", not no_key,
                      "所有执行记录都带幂等键" if not no_key
                      else f"缺少幂等键：{', '.join(no_key)}", no_key))

    return ConsistencyReport(passed=all(r.result == "pass" for r in results), results=results)
