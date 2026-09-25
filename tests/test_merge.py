"""Tests for merge-import diffing: added/unchanged/changed/removed classification."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.merge import (
    AFFECTED_STATUSES,
    DECISION_CLEAR,
    DECISION_KEEP,
    DECISION_NONE,
    STATUS_ADDED,
    STATUS_CHANGED,
    STATUS_REMOVED,
    STATUS_UNCHANGED,
    MergeValidationError,
    basis_decisions_match,
    basis_matches,
    basis_shape_valid,
    build_merge_plan,
    current_basis,
    normalize_incoming_records,
)


def cur(rows):
    # rows: (id, contestant, judge, item, score)
    return [
        {"id": i, "contestant": c, "judge": j, "item": it, "score": s}
        for i, (c, j, it, s) in enumerate(rows, start=1)
    ]


def inc(rows):
    # rows: (contestant, judge, item, score)
    return [
        {"contestant": c, "judge": j, "item": it, "score": s}
        for c, j, it, s in rows
    ]


def by_key(plan):
    return {(r["contestant"], r["judge"], r["item"]): r for r in plan["rows"]}


class TestBuildMergePlan(unittest.TestCase):
    def test_classifies_all_four_statuses(self):
        current = cur([
            ("C1", "JA", "P1", 8.0),   # changed
            ("C1", "JB", "P1", 7.0),   # unchanged
            ("C2", "JA", "P1", 9.0),   # removed
        ])
        incoming = inc([
            ("C1", "JA", "P1", 8.5),
            ("C1", "JB", "P1", 7.0),
            ("C3", "JA", "P1", 6.0),   # added
        ])
        plan = build_merge_plan(current, incoming, {})
        self.assertEqual(plan["counts"], {
            STATUS_CHANGED: 1, STATUS_ADDED: 1, STATUS_REMOVED: 1, STATUS_UNCHANGED: 1,
        })
        self.assertEqual(plan["will_modify"], 3)
        self.assertEqual(plan["total_current"], 3)
        self.assertEqual(plan["total_incoming"], 3)
        rows = by_key(plan)
        ch = rows[("C1", "JA", "P1")]
        self.assertEqual(ch["status"], STATUS_CHANGED)
        self.assertEqual(ch["old_score"], 8.0)
        self.assertEqual(ch["new_score"], 8.5)
        self.assertEqual(ch["score_id"], 1)
        self.assertEqual(rows[("C1", "JB", "P1")]["status"], STATUS_UNCHANGED)
        rm = rows[("C2", "JA", "P1")]
        self.assertEqual(rm["status"], STATUS_REMOVED)
        self.assertIsNone(rm["new_score"])
        self.assertEqual(rm["old_score"], 9.0)
        ad = rows[("C3", "JA", "P1")]
        self.assertEqual(ad["status"], STATUS_ADDED)
        self.assertIsNone(ad["old_score"])
        self.assertEqual(ad["new_score"], 6.0)
        self.assertIsNone(ad["score_id"])

    def test_row_order_buckets_then_preserves_inner_order(self):
        current = cur([
            ("U1", "J", "P", 1.0),   # unchanged (current order 1)
            ("X1", "J", "P", 1.0),   # changed   (current order 2)
            ("R1", "J", "P", 1.0),   # removed   (current order 3)
            ("X2", "J", "P", 2.0),   # changed   (current order 4)
            ("U2", "J", "P", 3.0),   # unchanged (current order 5)
        ])
        incoming = inc([
            ("X1", "J", "P", 9.0),
            ("X2", "J", "P", 8.0),
            ("A1", "J", "P", 5.0),   # added first
            ("A2", "J", "P", 5.0),   # added second
            ("U1", "J", "P", 1.0),
            ("U2", "J", "P", 3.0),
        ])
        plan = build_merge_plan(current, incoming, {})
        statuses = [r["status"] for r in plan["rows"]]
        self.assertEqual(
            statuses,
            [STATUS_CHANGED, STATUS_CHANGED,
             STATUS_ADDED, STATUS_ADDED,
             STATUS_REMOVED,
             STATUS_UNCHANGED, STATUS_UNCHANGED],
        )
        # Within buckets: current order for existing rows...
        changed_ids = [r["score_id"] for r in plan["rows"] if r["status"] == STATUS_CHANGED]
        self.assertEqual(changed_ids, [2, 4])
        # ...and incoming file order for added rows.
        added_names = [r["contestant"] for r in plan["rows"] if r["status"] == STATUS_ADDED]
        self.assertEqual(added_names, ["A1", "A2"])

    def test_decisions_clear_keep_none_actions(self):
        current = cur([
            ("C1", "JA", "P1", 8.0),   # changed + had decision -> clear
            ("C2", "JA", "P1", 7.0),   # removed + had decision -> clear
            ("C3", "JA", "P1", 6.0),   # changed, no decision  -> none
            ("C4", "JA", "P1", 5.0),   # unchanged + decision -> keep
            ("C5", "JA", "P1", 4.0),   # unchanged, no decision -> none
        ])
        decisions = {
            1: (True, "人工保留"),
            2: (False, "校准理由"),
            4: (True, ""),
        }
        incoming = inc([
            ("C1", "JA", "P1", 8.8),
            ("C3", "JA", "P1", 6.6),
            ("C4", "JA", "P1", 5.0),
            ("C5", "JA", "P1", 4.0),
        ])
        plan = build_merge_plan(current, incoming, decisions)
        self.assertEqual(plan["decisions_cleared"], 2)
        rows = by_key(plan)
        self.assertEqual(rows[("C1", "JA", "P1")]["decision_action"], DECISION_CLEAR)
        self.assertTrue(rows[("C1", "JA", "P1")]["has_decision"])
        self.assertTrue(rows[("C1", "JA", "P1")]["old_use_raw"])
        self.assertEqual(rows[("C1", "JA", "P1")]["old_reason"], "人工保留")
        self.assertEqual(rows[("C2", "JA", "P1")]["decision_action"], DECISION_CLEAR)
        self.assertEqual(rows[("C3", "JA", "P1")]["decision_action"], DECISION_NONE)
        self.assertEqual(rows[("C4", "JA", "P1")]["decision_action"], DECISION_KEEP)
        keep = rows[("C4", "JA", "P1")]
        self.assertTrue(keep["has_decision"])
        self.assertEqual(rows[("C5", "JA", "P1")]["decision_action"], DECISION_NONE)
        # Added rows never carry an old decision.
        extra = inc([("C9", "JA", "P1", 1.0)])
        plan2 = build_merge_plan(current, incoming + extra, decisions)
        added = next(r for r in plan2["rows"] if r["status"] == STATUS_ADDED)
        self.assertEqual(added["decision_action"], DECISION_NONE)
        self.assertFalse(added["has_decision"])

    def test_identical_files_all_unchanged(self):
        data = cur([("A", "J", "P", 5.0), ("B", "J", "P", 6.0)])
        incoming = inc([("A", "J", "P", 5.0), ("B", "J", "P", 6.0)])
        plan = build_merge_plan(data, incoming, {})
        self.assertEqual(plan["counts"][STATUS_UNCHANGED], 2)
        self.assertEqual(plan["will_modify"], 0)
        self.assertEqual(plan["decisions_cleared"], 0)

    def test_empty_current_all_added_and_empty_incoming_all_removed(self):
        data = cur([("A", "J", "P", 5.0)])
        all_added = build_merge_plan([], inc([("A", "J", "P", 5.0)]), {})
        self.assertEqual(all_added["counts"][STATUS_ADDED], 1)
        all_removed = build_merge_plan(data, [], {})
        self.assertEqual(all_removed["counts"][STATUS_REMOVED], 1)


def _plan_and_basis(rows, incoming_rows, decisions):
    current = cur(rows)
    plan = build_merge_plan(current, inc(incoming_rows), decisions)
    return current, plan, current_basis(current, plan["rows"])


class TestBasisDecisionSnapshots(unittest.TestCase):
    def setUp(self):
        self.rows = [
            ("C1", "JA", "P1", 8.0),   # id 1: changed, has decision
            ("C2", "JA", "P1", 7.0),   # id 2: removed, has decision
            ("C3", "JA", "P1", 6.0),   # id 3: changed, no decision
            ("C4", "JA", "P1", 5.0),   # id 4: unchanged, has decision
            ("C5", "JA", "P1", 4.0),   # id 5: unchanged, no decision
        ]
        self.incoming = [
            ("C1", "JA", "P1", 8.8),
            ("C3", "JA", "P1", 6.6),
            ("C4", "JA", "P1", 5.0),
            ("C5", "JA", "P1", 4.0),
        ]
        self.decisions = {1: (True, "人工保留"), 2: (False, "校准理由"), 4: (True, "")}

    def test_snapshot_only_for_affected_rows(self):
        _, plan, basis = _plan_and_basis(self.rows, self.incoming, self.decisions)
        by_triple = {(b["contestant"], b["judge"], b["item"]): b for b in basis}
        # 变更/删除行带裁决快照（存在性、采用原分、理由）。
        self.assertEqual(by_triple[("C1", "JA", "P1")]["decision"],
                         {"has_decision": True, "use_raw": True, "reason": "人工保留"})
        self.assertEqual(by_triple[("C2", "JA", "P1")]["decision"],
                         {"has_decision": True, "use_raw": False, "reason": "校准理由"})
        # 变更但本无裁决：同样记录存在性，供确认时发现「预览后新增裁决」。
        self.assertEqual(by_triple[("C3", "JA", "P1")]["decision"],
                         {"has_decision": False, "use_raw": False, "reason": ""})
        # 未变行不记录裁决快照：其裁决在合并中原样保留。
        self.assertNotIn("decision", by_triple[("C4", "JA", "P1")])
        self.assertNotIn("decision", by_triple[("C5", "JA", "P1")])
        self.assertEqual(
            sum("decision" in b for b in basis),
            sum(1 for r in plan["rows"] if r["status"] in AFFECTED_STATUSES),
        )

    def test_basis_matches_when_decisions_unchanged(self):
        current, plan, basis = _plan_and_basis(self.rows, self.incoming, self.decisions)
        self.assertTrue(basis_matches(basis, current))
        self.assertTrue(basis_decisions_match(basis, plan["rows"], self.decisions))

    def test_added_decision_after_preview_conflicts(self):
        # C3/JA 在预览时没有裁决，预览后被人工补上裁决 → 409 情形。
        _, plan, basis = _plan_and_basis(self.rows, self.incoming, self.decisions)
        self.assertFalse(basis_decisions_match(
            basis, plan["rows"], {**self.decisions, 3: (False, "预览后补的裁决")}
        ))

    def test_changed_decision_after_preview_conflicts(self):
        _, plan, basis = _plan_and_basis(self.rows, self.incoming, self.decisions)
        # 改 use_raw。
        self.assertFalse(basis_decisions_match(
            basis, plan["rows"], {**self.decisions, 1: (False, "人工保留")}
        ))
        # 改理由。
        self.assertFalse(basis_decisions_match(
            basis, plan["rows"], {**self.decisions, 1: (True, "改过的理由")}
        ))
        # 删除裁决。
        self.assertFalse(basis_decisions_match(
            basis, plan["rows"], {k: v for k, v in self.decisions.items() if k != 1}
        ))

    def test_decision_change_on_unchanged_row_does_not_conflict(self):
        # C4 是未变行，其裁决预览后变化不阻止确认（合并本就保留它）。
        _, plan, basis = _plan_and_basis(self.rows, self.incoming, self.decisions)
        changed = {**self.decisions, 4: (False, "未变行改了理由")}
        self.assertTrue(basis_decisions_match(basis, plan["rows"], changed))
        del changed[4]
        self.assertTrue(basis_decisions_match(basis, plan["rows"], changed))

    def test_tampered_records_change_affected_set(self):
        # 确认时回传的 records 被改：C1 不再变更，会少清一条旧裁决。
        current, plan, basis = _plan_and_basis(self.rows, self.incoming, self.decisions)
        tampered = [
            {"contestant": c, "judge": j, "item": it, "score": s}
            for c, j, it, s in self.incoming
            if (c, j, it) != ("C1", "JA", "P1")
        ]
        tampered.append({"contestant": "C1", "judge": "JA", "item": "P1", "score": 8.0})
        plan2 = build_merge_plan(current, tampered, self.decisions)
        self.assertTrue(basis_matches(basis, current))
        # 评分基线仍一致，但受影响集合（裁决清除集合）已变 → 冲突。
        self.assertFalse(basis_decisions_match(basis, plan2["rows"], self.decisions))

    def test_missing_snapshot_for_affected_row_conflicts(self):
        _, plan, basis = _plan_and_basis(self.rows, self.incoming, self.decisions)
        stripped = [
            {k: v for k, v in b.items() if k != "decision"} if b["contestant"] == "C3"
            else dict(b)
            for b in basis
        ]
        self.assertFalse(basis_decisions_match(stripped, plan["rows"], self.decisions))


class TestBasisShape(unittest.TestCase):
    def _basis(self, **decision_kw):
        item = {"contestant": "A", "judge": "J", "item": "P", "score": 9.0}
        if decision_kw:
            item["decision"] = decision_kw
        return [item]

    def test_plain_and_snapshot_shapes_ok(self):
        self.assertTrue(basis_shape_valid([]))
        self.assertTrue(basis_shape_valid(self._basis()))
        self.assertTrue(basis_shape_valid(
            self._basis(has_decision=True, use_raw=False, reason="理由")
        ))
        self.assertTrue(basis_shape_valid(
            self._basis(has_decision=False, use_raw=False, reason="")
        ))

    def test_bad_shapes_rejected(self):
        good = self._basis(has_decision=True, use_raw=True, reason="r")[0]
        self.assertFalse(basis_shape_valid("nope"))
        self.assertFalse(basis_shape_valid(["nope"]))
        self.assertFalse(basis_shape_valid([{"contestant": "A"}]))
        self.assertFalse(basis_shape_valid([{**good, "score": "9"}]))
        self.assertFalse(basis_shape_valid([{**good, "score": True}]))
        self.assertFalse(basis_shape_valid([{**good, "decision": "nope"}]))
        self.assertFalse(basis_shape_valid([{**good, "decision": {}}]))
        self.assertFalse(basis_shape_valid(
            [{**good, "decision": {"has_decision": 1, "use_raw": False, "reason": ""}}]
        ))
        self.assertFalse(basis_shape_valid(
            [{**good, "decision": {"has_decision": True, "use_raw": "yes", "reason": ""}}]
        ))
        self.assertFalse(basis_shape_valid(
            [{**good, "decision": {"has_decision": True, "use_raw": False, "reason": 1}}]
        ))


class TestBasis(unittest.TestCase):
    def setUp(self):
        self.current = cur([("A", "J", "P", 5.0), ("B", "J", "P", 6.0)])

    def test_roundtrip_matches(self):
        basis = current_basis(self.current)
        self.assertTrue(basis_matches(basis, self.current))
        # Order matters: shuffled basis does not match.
        self.assertFalse(basis_matches(list(reversed(basis)), self.current))

    def test_score_change_detected(self):
        basis = current_basis(self.current)
        basis[1]["score"] = 6.5
        self.assertFalse(basis_matches(basis, self.current))

    def test_length_and_key_mismatch(self):
        basis = current_basis(self.current)
        self.assertFalse(basis_matches(basis + [{"contestant": "C", "judge": "J", "item": "P", "score": 1}], self.current))
        bad = [dict(b) for b in basis]
        bad[0]["judge"] = "JX"
        self.assertFalse(basis_matches(bad, self.current))
        # Malformed basis entries do not raise but never match.
        self.assertFalse(basis_matches([{"contestant": "A"}], self.current))
        self.assertFalse(basis_matches(["nope"], self.current))


class TestNormalizeIncomingRecords(unittest.TestCase):
    def test_accepts_valid_records(self):
        out = normalize_incoming_records([
            {"contestant": " A ", "judge": "J", "item": "P", "score": "8.5"},
            {"contestant": "B", "judge": "J", "item": "P", "score": 9},
        ])
        self.assertEqual(out[0]["contestant"], "A")
        self.assertEqual(out[0]["score"], 8.5)
        self.assertEqual(out[1]["score"], 9.0)

    def test_rejects_bad_shapes(self):
        with self.assertRaises(MergeValidationError):
            normalize_incoming_records("nope")
        with self.assertRaises(MergeValidationError):
            normalize_incoming_records(["nope"])
        with self.assertRaises(MergeValidationError):
            normalize_incoming_records([])

    def test_rejects_missing_fields_and_bad_numbers(self):
        cases = [
            [{"judge": "J", "item": "P", "score": 1}],          # missing contestant
            [{"contestant": "A", "judge": " ", "item": "P", "score": 1}],  # blank judge
            [{"contestant": "A", "judge": "J", "item": "P", "score": ""}],
            [{"contestant": "A", "judge": "J", "item": "P", "score": "好"}],
            [{"contestant": "A", "judge": "J", "item": "P", "score": float("inf")}],
            [{"contestant": "A", "judge": "J", "item": "P"}],  # score missing
        ]
        for payload in cases:
            with self.assertRaises(MergeValidationError):
                normalize_incoming_records(payload)

    def test_rejects_boolean_score_and_duplicate_triple(self):
        with self.assertRaises(MergeValidationError):
            normalize_incoming_records([{"contestant": "A", "judge": "J", "item": "P", "score": True}])
        with self.assertRaises(MergeValidationError) as ctx:
            normalize_incoming_records([
                {"contestant": "A", "judge": "J", "item": "P", "score": 1},
                {"contestant": "A", "judge": "J", "item": "P", "score": 2},
            ])
        self.assertIn("重复", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
