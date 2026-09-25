"""Tests for item weight threshold analysis (read-only pure transform)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.scoring import analyze
from app.weight_analysis import (
    WeightValidationError,
    weight_threshold_analysis,
)


def make_records(rows):
    # rows: (contestant, judge, item, score)
    return [
        {"id": i + 1, "contestant": c, "judge": j, "item": it, "score": s}
        for i, (c, j, it, s) in enumerate(rows)
    ]


# 每个项目只有一位评委，使最终均分 == 原始分，便于手算断言；
# C1: I1=7, I2=9；C2: I1=9, I2=5。
# 调整 I2 倍率 w 时：总分 C1 = 9w + 7，C2 = 5w + 9 → 交点 w = 0.5。
BASE = [
    ("C1", "J", "I1", 7.0), ("C2", "J", "I1", 9.0),
    ("C1", "J", "I2", 9.0), ("C2", "J", "I2", 5.0),
]


def run(rows=BASE, item="I2", upper=3.0, decisions=None, scope="judge"):
    return weight_threshold_analysis(
        analyze(make_records(rows), decisions or {}, scope), item, upper
    )


def kinds(entries):
    return [e["kind"] for e in entries]


def critical_ws(result):
    return [e["w"] for e in result["entries"] if e["kind"] == "critical"]


class TestWeightThreshold(unittest.TestCase):
    def test_single_crossing_and_midpoints(self):
        r = run(upper=3.0)
        # 临界点 0.5 + 两个区间中点 (0,0.5) 与 (0.5,3)，端点 0/3 无并列不展开。
        self.assertEqual(kinds(r["entries"]), ["midpoint", "critical", "midpoint"])
        crit = next(e for e in r["entries"] if e["kind"] == "critical")
        self.assertAlmostEqual(crit["w"], 0.5, places=9)
        self.assertEqual(crit["ties"], [["C1", "C2"]])
        self.assertTrue(all(row["tied"] for row in crit["ranking"]))
        self.assertEqual({row["rank"] for row in crit["ranking"]}, {1})
        # 交点总分：9*0.5+7 = 5*0.5+9 = 11.5。
        for row in crit["ranking"]:
            self.assertAlmostEqual(row["total"], 11.5, places=9)
        # 中点处的相对次序：w=0.25 时 C2 领先，w=1.75 时 C1 领先。
        mids = [e for e in r["entries"] if e["kind"] == "midpoint"]
        self.assertAlmostEqual(mids[0]["w"], 0.25, places=9)
        self.assertAlmostEqual(mids[1]["w"], 1.75, places=9)
        rank_at = lambda e, n: next(x["rank"] for x in e["ranking"] if x["contestant"] == n)
        self.assertLess(rank_at(mids[0], "C2"), rank_at(mids[0], "C1"))
        self.assertLess(rank_at(mids[1], "C1"), rank_at(mids[1], "C2"))
        # 交点标记了 boundary=None（位于开区间内部）。
        self.assertIsNone(crit["boundary"])

    def test_crossing_at_lower_boundary(self):
        # C1/C2 在 I1 相同（截距相等），I2 均分不同 → 恰在 w=0 相交。
        rows = [
            ("C1", "J", "I1", 9.0), ("C2", "J", "I1", 9.0),
            ("C1", "J", "I2", 10.0), ("C2", "J", "I2", 8.0),
        ]
        r = run(rows, upper=3.0)
        crit = [e for e in r["entries"] if e["kind"] == "critical"]
        self.assertEqual(len(crit), 1)
        self.assertAlmostEqual(crit[0]["w"], 0.0)
        self.assertEqual(crit[0]["boundary"], "start")
        # 只有 (0,3) 一个开区间中点。
        self.assertEqual(kinds(r["entries"]), ["critical", "midpoint"])

    def test_crossing_at_upper_boundary(self):
        # C1: I1=10,I2=6 → 6w+10；C2: I1=4,I2=8 → 8w+4 → 交点 w=3。
        rows = [
            ("C1", "J", "I1", 10.0), ("C2", "J", "I1", 4.0),
            ("C1", "J", "I2", 6.0), ("C2", "J", "I2", 8.0),
        ]
        r = run(rows, upper=3.0)
        crit = [e for e in r["entries"] if e["kind"] == "critical"]
        self.assertEqual(len(crit), 1)
        self.assertAlmostEqual(crit[0]["w"], 3.0)
        self.assertEqual(crit[0]["boundary"], "end")
        self.assertEqual(kinds(r["entries"]), ["midpoint", "critical"])

    def test_root_outside_interval_has_no_critical(self):
        # C1: I1=0,I2=1 → w；C2: I1=-10,I2=2 → 2w-10；交点 w=10，超出上限 3。
        rows = [
            ("C1", "J", "I1", 0.0), ("C1", "J", "I2", 1.0),
            ("C2", "J", "I1", -10.0), ("C2", "J", "I2", 2.0),
        ]
        r = run(rows, upper=3.0)
        self.assertEqual(r["summary"]["crossing_pairs"], 0)
        self.assertEqual(r["summary"]["critical_points"], 0)
        self.assertEqual(kinds(r["entries"]), ["midpoint"])

    def test_identical_lines_always_tied_no_critical(self):
        rows = [
            ("C1", "J", "I1", 7.0), ("C2", "J", "I1", 7.0),
            ("C1", "J", "I2", 9.0), ("C2", "J", "I2", 9.0),
        ]
        r = run(rows, upper=3.0)
        # 直线完全重合：任何倍率都并列，不产生交点；区间内只有一个中点。
        self.assertEqual(critical_ws(r), [])
        self.assertEqual(r["always_tied_groups"], [["C1", "C2"]])
        self.assertEqual(r["summary"]["crossing_pairs"], 0)
        self.assertEqual(len([e for e in r["entries"] if e["kind"] == "midpoint"]), 1)
        # 中点处两人竞赛名次相同（1224 并列）。
        mid = next(e for e in r["entries"] if e["kind"] == "midpoint")
        ranks = {x["contestant"]: x["rank"] for x in mid["ranking"]}
        self.assertEqual(ranks, {"C1": 1, "C2": 1})

    def test_parallel_offset_lines_no_critical(self):
        # I2 均分相同（斜率相同），I1 差 2 分 → 平行不重合，永不相交。
        rows = [
            ("C1", "J", "I1", 9.0), ("C2", "J", "I1", 7.0),
            ("C1", "J", "I2", 5.0), ("C2", "J", "I2", 5.0),
        ]
        r = run(rows, upper=3.0)
        self.assertEqual(critical_ws(r), [])
        self.assertEqual(r["always_tied_groups"], [])
        mid = next(e for e in r["entries"] if e["kind"] == "midpoint")
        # C1 在任意 w 都领先 2 分。
        self.assertLess(next(x["rank"] for x in mid["ranking"] if x["contestant"] == "C1"),
                        next(x["rank"] for x in mid["ranking"] if x["contestant"] == "C2"))

    def test_triple_tie_merges_into_one_critical(self):
        # C1: w+3，C2: 2w+2，C3: 3w+1 → w=1 时三者都等于 4（3 对交点合并为 1 个）。
        rows = []
        for c, s, b in [("C1", 1.0, 3.0), ("C2", 2.0, 2.0), ("C3", 3.0, 1.0)]:
            rows.append((c, "J", "I1", b))
            rows.append((c, "J", "I2", s))
        r = run(rows, upper=3.0)
        crit = [e for e in r["entries"] if e["kind"] == "critical"]
        self.assertEqual(len(crit), 1)
        self.assertAlmostEqual(crit[0]["w"], 1.0)
        self.assertEqual(crit[0]["ties"], [["C1", "C2", "C3"]])
        self.assertEqual(r["summary"]["crossing_pairs"], 3)  # 3 对选手对都在此相交

    def test_missing_item_zero_contribution(self):
        # C3 只有 I1=8：斜率 0、截距 8，全程水平。
        rows = BASE + [("C3", "J", "I1", 8.0)]
        r = run(rows, upper=3.0)
        self.assertEqual(r["contestants_missing_item"], ["C3"])
        c3_rows = [
            row
            for e in r["entries"]
            for row in e["ranking"]
            if row["contestant"] == "C3"
        ]
        self.assertTrue(c3_rows)
        for row in c3_rows:
            self.assertFalse(row["has_item"])
            self.assertAlmostEqual(row["total"], 8.0, places=9)  # 缺失项目 0 贡献
            self.assertAlmostEqual(row["item_avg"], 0.0, places=9)

    def test_reflects_decisions_and_scope(self):
        # 给 C2 在 I2 的原始分（id=4）加「保留原分」裁决前先构造校准分≠原分的场景。
        # 两位评委打分会产生校准差异；裁决翻转后最终均分改变 → 交点随之移动。
        rows = [
            ("C1", "J1", "I1", 7.0), ("C2", "J1", "I1", 9.0),
            ("C1", "J2", "I1", 8.0), ("C2", "J2", "I1", 8.0),
            ("C1", "J1", "I2", 9.0), ("C2", "J1", "I2", 5.0),
            ("C1", "J2", "I2", 9.0), ("C2", "J2", "I2", 5.0),
        ]
        a0 = analyze(make_records(rows), {}, "judge_item")
        before = weight_threshold_analysis(a0, "I2", 3.0)
        # 找到 C2 在 I2 上校准分与原始分不同的评分并裁决保留原分。
        target = next(
            s["id"]
            for b in a0["contestants"] if b["contestant"] == "C2"
            for ib in b["items"] if ib["item"] == "I2"
            for s in ib["scores"]
            if abs(s["calibrated"] - s["raw"]) > 1e-9
        )
        a1 = analyze(make_records(rows), {target: (True, "保留")}, "judge_item")
        after = weight_threshold_analysis(a1, "I2", 3.0)
        self.assertNotAlmostEqual(critical_ws(before)[0], critical_ws(after)[0])
        self.assertEqual(after["scope"], "judge_item")

    def test_validation_errors(self):
        a = analyze(make_records(BASE), {}, "judge")
        with self.assertRaises(WeightValidationError):
            weight_threshold_analysis(a, "不存在", 3.0)
        with self.assertRaises(WeightValidationError):
            weight_threshold_analysis(a, "I2", 0.5)
        with self.assertRaises(WeightValidationError):
            weight_threshold_analysis(a, "I2", 5.1)
        with self.assertRaises(WeightValidationError):
            weight_threshold_analysis(a, "   ", 3.0)

    def test_ranking_totals_affine(self):
        # 任意快照处总分都等于「截距 + 斜率×w」，且名次为竞赛排名（1224）。
        r = run(upper=2.0)
        analysis = analyze(make_records(BASE), {}, "judge")
        avgs = {
            b["contestant"]: next(x["eff_avg"] for x in b["items"] if x["item"] == "I2")
            for b in analysis["contestants"]
        }
        for e in r["entries"]:
            for row in e["ranking"]:
                name = row["contestant"]
                base_total = next(
                    b["eff_total"] for b in analysis["contestants"]
                    if b["contestant"] == name
                )
                expect = base_total - avgs[name] + avgs[name] * e["w"]
                self.assertAlmostEqual(row["total"], expect, places=9)

    def test_default_upper_equals_one_allowed(self):
        # 上限恰为 1 也合法（0～1）。
        r = run(upper=1.0)
        self.assertEqual(r["upper_bound"], 1.0)
        self.assertTrue(all(0.0 <= e["w"] <= 1.0 for e in r["entries"]))


if __name__ == "__main__":
    unittest.main()
