"""项目权重临界点分析（只读纯函数）。

在当前校准范围与逐项裁决形成的**项目最终均分**基础上，选定一个项目，令其
倍率 w 从 0 连续变化到上限（1～5），其余项目倍率恒为 1。每位选手的总分即
关于 w 的仿射线性函数：

    总分_c(w) = 该项目最终均分_c × w + (最终总分_c − 该项目最终均分_c)
    缺失该项目的选手：斜率为 0（按 0 贡献），截距即其最终总分。

枚举两两选手直线在闭区间 [0, 上限] 内的交点（临界倍率），合并同位置交点；
平行（含始终并列的重合直线）不产生交点。对每个临界点给出该倍率下的竞赛
名次、总分与交点处并列的选手，并在相邻临界点（含端点 0、上限）构成的开
区间中点给出名次快照。

整个计算只读，不触碰评分、裁决或会话；评分、裁决或校准范围变化后由调用
方重新计算。
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from .scoring import SCOPE_LABELS, TIE_EPS, competition_rank

WEIGHT_LOWER = 0.0
WEIGHT_UPPER_MIN = 1.0
WEIGHT_UPPER_MAX = 5.0

# 交点落在区间（含端点）外多少以内仍钳到端点，视为端点处相交。
_ROOT_TOL_REL = 1e-10
# 同位置交点的合并容差（w 尺度最大为 5，绝对容差足够吸收浮点误差）。
_CLUSTER_TOL = 1e-9
# 两斜率视为相等（平行）的相对容差。
_SLOPE_REL_EPS = 1e-12


class WeightValidationError(ValueError):
    """请求参数不合法（项目不存在、倍率上限越界等）。"""


def _max_abs(values: Sequence[float]) -> float:
    return max([1.0] + [abs(v) for v in values])


class _DSU:
    """极简并查集：用于把同位置交点合并为并列组。"""

    def __init__(self) -> None:
        self.parent: dict[Any, Any] = {}

    def find(self, x: Any) -> Any:
        self.parent.setdefault(x, x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: Any, b: Any) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # 字典序较小者作根，保证输出稳定。
            if str(ra) < str(rb):
                self.parent[rb] = ra
            else:
                self.parent[ra] = rb

    def groups(self) -> list[list]:
        out: dict[Any, list[Any]] = {}
        for x in list(self.parent):
            out.setdefault(self.find(x), []).append(x)
        return [sorted(members, key=str) for members in out.values()]


def weight_threshold_analysis(
    analysis: Mapping[str, Any],
    item: str,
    upper_bound: float,
) -> dict[str, Any]:
    """计算单个项目倍率连续变化时的全部总分相等临界点与区间中点名次。

    ``analysis`` 为 :func:`app.scoring.analyze` 的输出（只使用其中的
    ``scope``、``items`` 与每位选手的 ``eff_total`` / 逐项目 ``eff_avg``）。
    """
    if isinstance(item, str):
        item = item.strip()
    if not item:
        raise WeightValidationError("请选择要分析的项目。")
    if (
        not isinstance(upper_bound, (int, float))
        or isinstance(upper_bound, bool)
        or upper_bound != upper_bound  # NaN
    ):
        raise WeightValidationError("倍率上限必须是 1～5 之间的数字。")
    upper_bound = float(upper_bound)
    if not (WEIGHT_UPPER_MIN <= upper_bound <= WEIGHT_UPPER_MAX):
        raise WeightValidationError("倍率上限必须在 1～5 之间。")

    items = list(analysis["items"])
    if item not in items:
        avail = "、".join(items) if items else "（当前没有项目）"
        raise WeightValidationError(f"项目「{item}」不存在，可选：{avail}。")

    blocks = list(analysis["contestants"])
    names = [b["contestant"] for b in blocks]
    # 总分_c(w) = slope_c * w + intercept_c。
    slope: dict[str, float] = {}
    intercept: dict[str, float] = {}
    has_item: dict[str, bool] = {}
    for b in blocks:
        name = b["contestant"]
        ib = next((x for x in b["items"] if x["item"] == item), None)
        d = float(ib["eff_avg"]) if ib is not None else 0.0  # 缺失项目按 0 贡献
        slope[name] = d
        intercept[name] = float(b["eff_total"]) - d
        has_item[name] = ib is not None

    slope_scale = _max_abs(list(slope.values()))
    total_scale = _max_abs(
        [intercept[n] + slope[n] * w for n in names for w in (WEIGHT_LOWER, upper_bound)]
    )
    root_tol = _ROOT_TOL_REL * max(1.0, upper_bound)

    def parallel(i: str, j: str) -> bool:
        return abs(slope[i] - slope[j]) <= _SLOPE_REL_EPS * slope_scale

    # 始终并列（直线重合）：斜率相同且截距相同——这些选手在任何倍率下都并列，
    # 按需求不产生交点，仅在汇总中提示。
    always_dsu = _DSU()
    for x, i in enumerate(names):
        for j in names[x + 1:]:
            if parallel(i, j) and abs(intercept[i] - intercept[j]) <= TIE_EPS * total_scale:
                always_dsu.union(i, j)
    always_tied = sorted(
        (g for g in always_dsu.groups() if len(g) >= 2),
        key=lambda g: (-intercept[g[0]], "、".join(g)),
    )

    # 枚举所有非平行选手对的交点 w；落在闭区间（含端点容差）内才保留。
    crossings: list[tuple[float, str, str]] = []
    for x, i in enumerate(names):
        for j in names[x + 1:]:
            if parallel(i, j):
                continue
            w0 = (intercept[j] - intercept[i]) / (slope[i] - slope[j])
            if w0 < -root_tol or w0 > upper_bound + root_tol:
                continue
            w0 = min(max(w0, WEIGHT_LOWER), upper_bound)
            crossings.append((w0, i, j))
    crossings.sort(key=lambda t: (t[0], t[1], t[2]))

    # 同位置交点顺序聚类（类内距离不超过合并容差），类内用并查集合并选手。
    # 同位置的数学根只有浮点误差级差异，直接以首条交点（已钳到端点）为代表，
    # 避免平均后轻微漂移离开端点或把不同交点越并越宽。
    clusters: list[dict[str, Any]] = []
    for w0, i, j in crossings:
        w_clamped = min(max(w0, WEIGHT_LOWER), upper_bound)
        if clusters and abs(w_clamped - clusters[-1]["w"]) <= _CLUSTER_TOL:
            cluster = clusters[-1]
        else:
            cluster = {"w": w_clamped, "dsu": _DSU()}
            clusters.append(cluster)
        cluster["dsu"].union(i, j)

    def boundary_flag(w: float) -> str | None:
        if abs(w - WEIGHT_LOWER) <= _CLUSTER_TOL:
            return "start"
        if abs(w - upper_bound) <= _CLUSTER_TOL:
            return "end"
        return None

    # 临界点（带并列组）；端点 0 / 上限即使无并列也作为区间分隔点参与中点计算。
    cut_points: list[float] = [WEIGHT_LOWER, upper_bound]
    tie_at: dict[float, list[list[str]]] = {}
    for cluster in clusters:
        w = cluster["w"]
        flag = boundary_flag(w)
        if flag == "start":
            w = WEIGHT_LOWER
            cluster["w"] = w  # 与端点分隔点使用完全相同的浮点键
        elif flag == "end":
            w = upper_bound
            cluster["w"] = w
        groups = sorted(
            (g for g in cluster["dsu"].groups() if len(g) >= 2),
            key=lambda g: (
                -sum(intercept[m] + slope[m] * w for m in g) / len(g),
                "、".join(g),
            ),
        )
        if w not in cut_points:
            cut_points.append(w)
        if groups:
            tie_at.setdefault(w, [])
            for g in groups:
                if g not in tie_at[w]:
                    tie_at[w].append(g)
    cut_points = sorted(set(cut_points))

    def ranking_at(w: float, tied_members: set[str] | None = None) -> list[dict[str, Any]]:
        totals = {n: intercept[n] + slope[n] * w for n in names}
        ranks = competition_rank(totals.items())
        rows = [
            {
                "contestant": n,
                "rank": ranks[n],
                "total": totals[n],
                "item_avg": slope[n],
                "has_item": has_item[n],
                "tied": bool(tied_members and n in tied_members),
            }
            for n in names
        ]
        rows.sort(key=lambda r: (r["rank"], r["contestant"]))
        return rows

    entries: list[dict[str, Any]] = []
    critical_count = 0
    for idx, w in enumerate(cut_points):
        groups = tie_at.get(w)
        if groups:
            members = {m for g in groups for m in g}
            entries.append(
                {
                    "kind": "critical",
                    "w": w,
                    "boundary": boundary_flag(w),
                    "ties": groups,
                    "ranking": ranking_at(w, members),
                }
            )
            critical_count += 1
        if idx + 1 < len(cut_points):
            w_next = cut_points[idx + 1]
            if w_next - w > _CLUSTER_TOL:
                mid = (w + w_next) / 2.0
                entries.append(
                    {
                        "kind": "midpoint",
                        "w": mid,
                        "left": w,
                        "right": w_next,
                        "ranking": ranking_at(mid),
                    }
                )

    scope = analysis.get("scope", "")
    return {
        "scope": scope,
        "scope_label": analysis.get("scope_label") or SCOPE_LABELS.get(scope, scope),
        "item": item,
        "lower": WEIGHT_LOWER,
        "upper_bound": upper_bound,
        "contestants_missing_item": [n for n in names if not has_item[n]],
        "always_tied_groups": always_tied,
        "summary": {
            "contestants": len(names),
            "crossing_pairs": len(crossings),
            "critical_points": critical_count,
            "interval_midpoints": sum(1 for e in entries if e["kind"] == "midpoint"),
        },
        "entries": entries,
    }
