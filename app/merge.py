"""评分数据合并导入：按「选手+评委+项目」三元组计算差异。

上传 CSV 走的是整体替换（``replace_scores``，会清空裁决与批处理规则）；
合并导入则在保留未变记录的前提下，把新文件并入当前会话：

- ``added``     新文件有、当前会话没有 → 新增
- ``unchanged`` 两边都有且分数相同    → 保持不动（裁决/规则均不受影响）
- ``changed``   两边都有但分数不同    → 原地更新分数，旧裁决自动清除
- ``removed``   仅存在于当前会话      → 删除记录，其旧裁决一并清除

差异计算是纯函数，预览不触碰数据库；确认时由
``database.merge_scores`` 在单个事务内落库。
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

# 状态固定展示顺序：变更、新增、仅当前会话（删除）、未变。
STATUS_CHANGED = "changed"
STATUS_ADDED = "added"
STATUS_REMOVED = "removed"
STATUS_UNCHANGED = "unchanged"

STATUSES = (STATUS_CHANGED, STATUS_ADDED, STATUS_REMOVED, STATUS_UNCHANGED)
STATUS_LABELS = {
    STATUS_CHANGED: "分数变更",
    STATUS_ADDED: "新增",
    STATUS_REMOVED: "仅当前会话（将删除）",
    STATUS_UNCHANGED: "未变",
}
_STATUS_ORDER = {s: i for i, s in enumerate(STATUSES)}

# 决策动作用于预览逐行展示裁决受影响方式。
DECISION_CLEAR = "clear"   # 变更/删除：旧裁决（如有）将被清除
DECISION_KEEP = "keep"     # 未变：现有裁决保持不动
DECISION_NONE = "none"     # 该记录当前没有裁决


class MergeValidationError(ValueError):
    """合并提交的 JSON 记录不合法（返回 400）。"""


def _triple(row: Mapping[str, Any]) -> tuple[str, str, str]:
    return (row["contestant"], row["judge"], row["item"])


def _decision_info(
    decisions: Mapping[int, Any], score_id: int
) -> tuple[bool, bool | None, str]:
    """Return (exists, use_raw, reason) for a current score id."""
    verdict = decisions.get(score_id)
    if verdict is None:
        return False, None, ""
    return True, bool(verdict[0]), verdict[1] or ""


def build_merge_plan(
    current: Sequence[Mapping[str, Any]],
    incoming: Sequence[Mapping[str, Any]],
    decisions: Mapping[int, Any] | None = None,
) -> dict[str, Any]:
    """对比当前记录与新文件记录，生成逐条合并计划（纯函数）。

    current: 库内评分记录（含 id/score），决定 rows 的基础排序。
    incoming: parse_scores 解析出的新记录（无 id）。
    decisions: score_id -> 裁决，用于标记哪些行会清掉旧裁决。

    返回 {"rows": [...], "counts": {...}}；rows 按
    变更→新增→仅当前会话→未变排列，组内沿用当前行顺序 / 新文件行顺序。
    """
    decisions = decisions or {}
    current_by_key: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    for rec in current:
        current_by_key[_triple(rec)] = rec
    incoming_by_key: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    for rec in incoming:
        incoming_by_key[_triple(rec)] = rec

    rows: list[dict[str, Any]] = []
    counts = {s: 0 for s in STATUSES}
    decisions_cleared = 0

    # 当前会话侧：先按当前顺序产出 变更/未变/仅当前会话。
    for rec in current:
        key = _triple(rec)
        new = incoming_by_key.get(key)
        if new is None:
            status = STATUS_REMOVED
        elif float(new["score"]) == float(rec["score"]):
            status = STATUS_UNCHANGED
        else:
            status = STATUS_CHANGED
        has_decision, old_use_raw, old_reason = _decision_info(decisions, rec["id"])
        affected = status != STATUS_UNCHANGED and has_decision
        if affected:
            decisions_cleared += 1
        if status == STATUS_CHANGED or status == STATUS_REMOVED:
            decision_action = DECISION_CLEAR if affected else DECISION_NONE
        else:
            decision_action = DECISION_KEEP if has_decision else DECISION_NONE
        rows.append(
            {
                "status": status,
                "contestant": rec["contestant"],
                "judge": rec["judge"],
                "item": rec["item"],
                "score_id": rec["id"],
                "old_score": rec["score"],
                "new_score": None if new is None else float(new["score"]),
                "incoming_order": None,
                "has_decision": has_decision,
                "old_use_raw": old_use_raw,
                "old_reason": old_reason,
                "decision_action": decision_action,
            }
        )
        counts[status] += 1

    # 新增侧：沿用新文件中的出现顺序。
    for order, rec in enumerate(incoming):
        if _triple(rec) in current_by_key:
            continue
        rows.append(
            {
                "status": STATUS_ADDED,
                "contestant": rec["contestant"],
                "judge": rec["judge"],
                "item": rec["item"],
                "score_id": None,
                "old_score": None,
                "new_score": float(rec["score"]),
                "incoming_order": order,
                "has_decision": False,
                "decision_action": DECISION_NONE,
            }
        )
        counts[STATUS_ADDED] += 1

    # 稳定排序：只按状态分桶，桶内保持插入顺序
    # （当前侧=库内行序，新增侧=新文件行序）。
    rows.sort(key=lambda r: _STATUS_ORDER[r["status"]])

    return {
        "rows": rows,
        "counts": counts,
        "total_current": len(current),
        "total_incoming": len(incoming),
        "decisions_cleared": decisions_cleared,
        "will_modify": counts[STATUS_CHANGED]
        + counts[STATUS_ADDED]
        + counts[STATUS_REMOVED],
    }


def current_basis(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """生成确认时回传的「预览基线」快照。"""
    return [
        {
            "contestant": r["contestant"],
            "judge": r["judge"],
            "item": r["item"],
            "score": float(r["score"]),
        }
        for r in records
    ]


def basis_matches(
    basis: Sequence[Mapping[str, Any]],
    current: Sequence[Mapping[str, Any]],
) -> bool:
    """核对客户端回传的基线是否仍与库内数据一致。"""
    if len(basis) != len(current):
        return False
    for b, rec in zip(basis, current):
        try:
            if (
                b["contestant"] != rec["contestant"]
                or b["judge"] != rec["judge"]
                or b["item"] != rec["item"]
                or float(b["score"]) != float(rec["score"])
            ):
                return False
        except (KeyError, TypeError, ValueError):
            return False
    return True


def normalize_incoming_records(raw: Any) -> list[dict[str, Any]]:
    """校验确认请求里回传的新记录（与 parse_scores 同样严格）。"""
    if not isinstance(raw, list):
        raise MergeValidationError("records 必须是数组。")
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for i, row in enumerate(raw, start=1):
        where = f"第 {i} 条记录"
        if not isinstance(row, Mapping):
            raise MergeValidationError(f"{where}必须是对象。")

        def text(field: str, label: str) -> str:
            value = row.get(field)
            if not isinstance(value, str) or not value.strip():
                raise MergeValidationError(f"{where}的「{label}」不能为空。")
            return value.strip()

        contestant = text("contestant", "选手")
        judge = text("judge", "评委")
        item = text("item", "项目")
        score_raw = row.get("score")
        if isinstance(score_raw, bool) or not isinstance(score_raw, (int, float, str)):
            raise MergeValidationError(f"{where}的分数必须是数字。")
        score_text = str(score_raw).strip()
        if not score_text:
            raise MergeValidationError(f"{where}的「分数」不能为空。")
        try:
            score = float(score_text)
        except ValueError:
            raise MergeValidationError(f"{where}的分数「{score_text}」不是数字。")
        if score != score or score in (float("inf"), float("-inf")):
            raise MergeValidationError(f"{where}的分数无效。")

        triple = (contestant, judge, item)
        if triple in seen:
            raise MergeValidationError(
                f"{where}存在重复评分（选手={contestant}，评委={judge}，项目={item}）。"
            )
        seen.add(triple)
        records.append(
            {"contestant": contestant, "judge": judge, "item": item, "score": score}
        )

    if not records:
        raise MergeValidationError("合并导入至少需要一条评分记录。")
    return records
