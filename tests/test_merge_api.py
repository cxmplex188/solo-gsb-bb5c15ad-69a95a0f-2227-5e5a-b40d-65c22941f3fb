"""API tests for the merge-import flow (preview read-only, confirm transactional)."""
import os
import sys
import tempfile
import unittest

_tmp = tempfile.mkdtemp(prefix="calib_merge_test_")
os.environ["CALIB_DATA_DIR"] = _tmp

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app import database as db  # noqa: E402
from app.main import app  # noqa: E402

db.init_db()
client = TestClient(app)
SID = "merge-session"

HEADER = "选手,评委,项目,分数\n"
INITIAL = (
    HEADER
    + "C1,JA,P1,8.0\n"   # -> changed to 8.5
    + "C1,JB,P1,7.0\n"   # -> unchanged
    + "C2,JA,P1,9.0\n"   # -> removed (only in session)
    + "C2,JB,P1,6.0\n"   # -> changed to 6.5, had no decision
)
MERGED = (
    HEADER
    + "C1,JA,P1,8.5\n"
    + "C1,JB,P1,7.0\n"
    + "C2,JB,P1,6.5\n"
    + "C3,JA,P1,5.0\n"   # -> added
    + "C3,JB,P1,4.0\n"   # -> added
)


def upload(csv_text):
    return client.post(
        f"/api/sessions/{SID}/upload?scope=judge",
        files={"file": ("s.csv", csv_text.encode("utf-8-sig"), "text/csv")},
    )


def preview(csv_text):
    return client.post(
        f"/api/sessions/{SID}/merge/preview",
        files={"file": ("m.csv", csv_text.encode("utf-8-sig"), "text/csv")},
    )


def confirm(payload):
    return client.post(f"/api/sessions/{SID}/merge/confirm", json=payload)


def score_map(analysis):
    return {
        (b["contestant"], s["judge"], ib["item"]): s
        for b in analysis["contestants"]
        for ib in b["items"]
        for s in ib["scores"]
    }


class TestMergeAPI(unittest.TestCase):
    def test_01_preview_readonly_and_classification(self):
        self.assertEqual(upload(INITIAL).status_code, 200)
        # Put a decision on C1/JA/P1 (changed row) and C1/JB/P1 (unchanged row).
        analysis = client.get(f"/api/sessions/{SID}/analysis").json()
        id_of = {(b["contestant"], s["judge"], ib["item"]): s["id"]
                 for b in analysis["contestants"] for ib in b["items"] for s in ib["scores"]}
        client.patch(f"/api/sessions/{SID}/scores/{id_of[('C1', 'JA', 'P1')]}/decision",
                     json={"use_raw": True, "reason": "人工保留甲"})
        client.patch(f"/api/sessions/{SID}/scores/{id_of[('C1', 'JB', 'P1')]}/decision",
                     json={"use_raw": True, "reason": "校准乙"})

        r = preview(MERGED)
        self.assertEqual(r.status_code, 200, r.text)
        p = r.json()
        self.assertEqual(p["counts"], {"changed": 2, "added": 2, "removed": 1, "unchanged": 1})
        self.assertEqual(p["will_modify"], 5)
        self.assertEqual(p["total_current"], 4)
        self.assertEqual(p["total_incoming"], 5)
        # Only C1/JA/P1 loses a decision; C2/JB changed row had none.
        self.assertEqual(p["decisions_cleared"], 1)
        rows = {(x["contestant"], x["judge"], x["item"]): x for x in p["rows"]}
        ch = rows[("C1", "JA", "P1")]
        self.assertEqual(ch["old_score"], 8.0)
        self.assertEqual(ch["new_score"], 8.5)
        self.assertEqual(ch["decision_action"], "clear")
        self.assertTrue(ch["has_decision"])
        self.assertTrue(ch["old_use_raw"])
        self.assertEqual(ch["old_reason"], "人工保留甲")
        un = rows[("C1", "JB", "P1")]
        self.assertEqual(un["status"], "unchanged")
        self.assertEqual(un["decision_action"], "keep")
        rm = rows[("C2", "JA", "P1")]
        self.assertEqual(rm["status"], "removed")
        self.assertIsNone(rm["new_score"])
        self.assertEqual(rm["decision_action"], "none")
        ad = rows[("C3", "JA", "P1")]
        self.assertEqual(ad["status"], "added")
        self.assertIsNone(ad["old_score"])
        # Payload needed for confirm is echoed back.
        self.assertEqual(len(p["records"]), 5)
        self.assertEqual(len(p["basis"]), 4)
        # 基线对「变更/删除」行记录旧裁决状态；未变行不记录。
        basis = {
            (b["contestant"], b["judge"], b["item"]): b for b in p["basis"]
        }
        self.assertEqual(basis[("C1", "JA", "P1")]["decision"],
                         {"has_decision": True, "use_raw": True, "reason": "人工保留甲"})
        # C2/JB 是变更行但本无裁决：快照记录「不存在」，防止预览后补裁决被静默清除。
        self.assertEqual(basis[("C2", "JB", "P1")]["decision"],
                         {"has_decision": False, "use_raw": False, "reason": ""})
        # C2/JA 删除行无裁决，同样带存在性快照。
        self.assertEqual(basis[("C2", "JA", "P1")]["decision"],
                         {"has_decision": False, "use_raw": False, "reason": ""})
        # 未变行（虽有裁决）不在核对范围。
        self.assertNotIn("decision", basis[("C1", "JB", "P1")])
        # Bucket order: changed -> added -> removed -> unchanged.
        statuses = [x["status"] for x in p["rows"]]
        self.assertEqual(statuses, sorted(statuses, key={
            "changed": 0, "added": 1, "removed": 2, "unchanged": 3,
        }.__getitem__))

        # Preview must not touch scores or decisions.
        again = client.get(f"/api/sessions/{SID}/analysis").json()
        self.assertEqual(again["counts"]["scores"], 4)
        self.assertEqual(again["counts"]["decisions"], 2)

    def test_02_preview_bad_csv_is_400(self):
        r = client.post(
            f"/api/sessions/{SID}/merge/preview",
            files={"file": ("m.csv", "选手,评委,分数\nA,J,9".encode(), "text/csv")},
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("项目", r.json()["detail"])
        # Duplicate triple inside the merge file -> 400.
        dup = HEADER + "A,J,P,1\nA,J,P,2\n"
        self.assertEqual(preview(dup).status_code, 400)

    def test_03_confirm_stale_basis_conflicts(self):
        p = preview(MERGED).json()
        stale_basis = p["basis"][:-1]  # simulate data changed since preview
        r = confirm({"records": p["records"], "basis": stale_basis})
        self.assertEqual(r.status_code, 409)
        self.assertIn("重新预览", r.json()["detail"])
        # Nothing was applied.
        self.assertEqual(
            client.get(f"/api/sessions/{SID}/analysis").json()["counts"]["scores"], 4
        )

    def test_04_confirm_validation_errors(self):
        p = preview(MERGED).json()
        # Not a list -> pydantic 422.
        r = confirm({"records": "nope", "basis": p["basis"]})
        self.assertEqual(r.status_code, 422)
        # Empty list -> pydantic 422 (min_length=1).
        r = confirm({"records": [], "basis": p["basis"]})
        self.assertEqual(r.status_code, 422)
        # Malformed record -> 400 from normalize.
        bad = [dict(p["records"][0])]
        bad[0]["score"] = "不是数字"
        r = confirm({"records": bad, "basis": p["basis"]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("不是数字", r.json()["detail"])
        # Duplicate triple inside records -> 400.
        r = confirm({"records": p["records"] + [p["records"][0]], "basis": p["basis"]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("重复", r.json()["detail"])

    def test_04b_stale_decisions_conflict_without_writing(self):
        """预览后新增/修改将清除的旧裁决时，旧预览确认必须 409 且不写入。"""
        sid = "merge-stale-decisions"

        def upload_sid(csv_text):
            return client.post(
                f"/api/sessions/{sid}/upload?scope=judge",
                files={"file": ("s.csv", csv_text.encode("utf-8-sig"), "text/csv")},
            )

        def preview_sid(csv_text):
            return client.post(
                f"/api/sessions/{sid}/merge/preview",
                files={"file": ("m.csv", csv_text.encode("utf-8-sig"), "text/csv")},
            )

        def confirm_sid(payload):
            return client.post(f"/api/sessions/{sid}/merge/confirm", json=payload)

        def ids():
            a = client.get(f"/api/sessions/{sid}/analysis").json()
            return {
                (b["contestant"], s["judge"], ib["item"]): s["id"]
                for b in a["contestants"] for ib in b["items"] for s in ib["scores"]
            }

        upload_sid(INITIAL)
        id_of = ids()
        # C1/JA（变更行）在预览前已有裁决。
        client.patch(f"/api/sessions/{sid}/scores/{id_of[('C1', 'JA', 'P1')]}/decision",
                     json={"use_raw": True, "reason": "人工保留甲"})

        def assert_unchanged():
            a = client.get(f"/api/sessions/{sid}/analysis").json()
            self.assertEqual(a["counts"]["scores"], 4)
            m = score_map(a)
            self.assertEqual(m[("C1", "JA", "P1")]["raw"], 8.0)
            self.assertIn(("C2", "JA", "P1"), m)
            self.assertNotIn(("C3", "JA", "P1"), m)

        # 情形一：预览后给「本无裁决的变更行」补裁决。
        p = preview_sid(MERGED).json()
        client.patch(f"/api/sessions/{sid}/scores/{id_of[('C2', 'JB', 'P1')]}/decision",
                     json={"use_raw": False, "reason": "预览后补的裁决"})
        r = confirm_sid({"records": p["records"], "basis": p["basis"]})
        self.assertEqual(r.status_code, 409)
        self.assertIn("重新预览", r.json()["detail"])
        assert_unchanged()
        # 新补的裁决也不能丢。
        m = score_map(client.get(f"/api/sessions/{sid}/analysis").json())
        self.assertEqual(m[("C2", "JB", "P1")]["reason"], "预览后补的裁决")

        # 情形二：预览后改动将清除的旧裁决（改理由 / 改采用原分 / 删除）。
        client.post(f"/api/sessions/{sid}/decisions/reset")
        client.patch(f"/api/sessions/{sid}/scores/{id_of[('C1', 'JA', 'P1')]}/decision",
                     json={"use_raw": True, "reason": "人工保留甲"})
        p = preview_sid(MERGED).json()
        client.patch(f"/api/sessions/{sid}/scores/{id_of[('C1', 'JA', 'P1')]}/decision",
                     json={"use_raw": False, "reason": "预览后改的理由"})
        r = confirm_sid({"records": p["records"], "basis": p["basis"]})
        self.assertEqual(r.status_code, 409)
        self.assertIn("重新预览", r.json()["detail"])
        assert_unchanged()
        m = score_map(client.get(f"/api/sessions/{sid}/analysis").json())
        self.assertEqual(m[("C1", "JA", "P1")]["reason"], "预览后改的理由")
        self.assertFalse(m[("C1", "JA", "P1")]["use_raw"])

        # 情形三：未变行的裁决在预览后变化不阻止确认（合并本就保留它）；
        # 重新预览（基线已含最新裁决状态）后确认成功，清除数量与预览一致。
        client.patch(f"/api/sessions/{sid}/scores/{id_of[('C1', 'JB', 'P1')]}/decision",
                     json={"use_raw": True, "reason": "未变行新理由"})
        p = preview_sid(MERGED).json()
        self.assertEqual(p["decisions_cleared"], 1)
        # 确认前再改一次未变行裁决：仍可提交，且该裁决原样保留为最新值。
        client.patch(f"/api/sessions/{sid}/scores/{id_of[('C1', 'JB', 'P1')]}/decision",
                     json={"use_raw": True, "reason": "未变行最新理由"})
        r = confirm_sid({"records": p["records"], "basis": p["basis"]})
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data["merge_applied"]["decisions_cleared"], 1)
        self.assertEqual(data["counts"]["scores"], 5)
        # C1/JA 的旧裁决按预览清除；未变行 C1/JB 裁决保留为最新值。
        self.assertEqual(data["counts"]["decisions"], 1)
        m = score_map(data)
        self.assertEqual(m[("C1", "JA", "P1")]["reason"], "")
        self.assertFalse(m[("C1", "JA", "P1")]["use_raw"])
        self.assertEqual(m[("C1", "JB", "P1")]["reason"], "未变行最新理由")

    def test_04c_basis_decision_snapshot_shape_errors_are_400(self):
        p = preview(MERGED).json()
        bad_basis = [dict(b) for b in p["basis"]]
        bad_basis[0]["decision"] = "nope"
        r = confirm({"records": p["records"], "basis": bad_basis})
        self.assertEqual(r.status_code, 400)
        self.assertIn("重新预览", r.json()["detail"])

    def test_04d_tampered_records_conflicts_without_writing(self):
        # 确认时篡改 records，使某条变更变回未变（少清一条旧裁决）→ 409。
        p = preview(MERGED).json()
        tampered = [dict(r) for r in p["records"]]
        for row in tampered:
            if (row["contestant"], row["judge"], row["item"]) == ("C1", "JA", "P1"):
                row["score"] = 8.0  # 与旧分相同 → 重新计算时变为 unchanged
        r = confirm({"records": tampered, "basis": p["basis"]})
        self.assertEqual(r.status_code, 409)
        self.assertIn("重新预览", r.json()["detail"])
        self.assertEqual(
            client.get(f"/api/sessions/{SID}/analysis").json()["counts"]["scores"], 4
        )

    def test_05_confirm_applies_in_transaction(self):
        p = preview(MERGED).json()
        r = confirm({"records": p["records"], "basis": p["basis"]})
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        applied = data["merge_applied"]
        self.assertEqual(applied["counts"],
                         {"changed": 2, "added": 2, "removed": 1, "unchanged": 1})
        self.assertEqual(applied["decisions_cleared"], 1)
        self.assertEqual(applied["rules_touched"], 0)
        # Refreshed analysis returned in the same response.
        self.assertEqual(data["counts"]["scores"], 5)
        # C1/JA/P1 decision cleared; C1/JB/P1 unchanged decision kept.
        self.assertEqual(data["counts"]["decisions"], 1)

        m = score_map(data)
        self.assertEqual(m[("C1", "JA", "P1")]["raw"], 8.5)
        self.assertFalse(m[("C1", "JA", "P1")]["use_raw"])
        self.assertEqual(m[("C1", "JA", "P1")]["reason"], "")
        self.assertEqual(m[("C2", "JB", "P1")]["raw"], 6.5)
        self.assertNotIn(("C2", "JA", "P1"), m)  # removed row is gone
        self.assertIn(("C3", "JA", "P1"), m)
        self.assertIn(("C3", "JB", "P1"), m)
        keep = m[("C1", "JB", "P1")]
        self.assertTrue(keep["use_raw"])
        self.assertEqual(keep["reason"], "校准乙")
        self.assertEqual(keep["raw"], 7.0)

    def test_06_unchanged_rows_keep_ids_and_remerge_is_all_unchanged(self):
        # Re-preview the just-merged state with the same file: nothing to do.
        p = preview(MERGED).json()
        self.assertEqual(p["counts"], {"changed": 0, "added": 0, "removed": 0, "unchanged": 5})
        self.assertEqual(p["will_modify"], 0)
        self.assertEqual(p["decisions_cleared"], 0)
        # Confirming a no-op merge still succeeds and changes nothing.
        r = confirm({"records": p["records"], "basis": p["basis"]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["counts"]["scores"], 5)
        self.assertEqual(r.json()["counts"]["decisions"], 1)

    def test_07_batch_rules_are_not_touched_by_merge(self):
        analysis = client.get(f"/api/sessions/{SID}/analysis").json()
        judge = analysis["judges"][0]
        client.put(f"/api/sessions/{SID}/rules", json={"rules": [
            {"name": "合并后规则保留", "judge": judge, "use_raw": True, "priority": 5},
        ]})
        next_csv = HEADER + "C1,JA,P1,9.9\nC1,JB,P1,7.0\nC2,JB,P1,6.5\nC3,JA,P1,5.0\nC3,JB,P1,4.0\n"
        p = preview(next_csv).json()
        self.assertEqual(p["counts"]["changed"], 1)
        r = confirm({"records": p["records"], "basis": p["basis"]})
        self.assertEqual(r.status_code, 200, r.text)
        rules = client.get(f"/api/sessions/{SID}/rules").json()["rules"]
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["name"], "合并后规则保留")
        self.assertEqual(rules[0]["judge"], judge)

    def test_08_preview_on_brand_new_session_is_all_added_and_creates_nothing(self):
        fresh = "brand-new-merge-session"
        r = client.post(
            f"/api/sessions/{fresh}/merge/preview?scope=judge_item",
            files={"file": ("m.csv", MERGED.encode(), "text/csv")},
        )
        self.assertEqual(r.status_code, 200, r.text)
        p = r.json()
        self.assertFalse(p["session_existed"])
        self.assertEqual(p["scope"], "judge_item")
        self.assertEqual(p["counts"]["added"], 5)
        self.assertEqual(p["total_current"], 0)
        # Preview must not create the session.
        self.assertEqual(
            client.get(f"/api/sessions/{fresh}/analysis").json()["counts"]["scores"], 0
        )
        # Confirm into the empty session creates it with the chosen scope.
        r = client.post(f"/api/sessions/{fresh}/merge/confirm",
                        json={"records": p["records"], "basis": [], "scope": "judge_item"})
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data["counts"]["scores"], 5)
        self.assertEqual(data["scope"], "judge_item")

    def test_09_cancel_is_nothing(self):
        # Issuing a preview and walking away (no confirm) leaves data intact.
        before = client.get(f"/api/sessions/{SID}/analysis").json()
        p = preview(HEADER + "C1,JA,P1,1.0\n").json()
        self.assertEqual(p["will_modify"], 5)
        after = client.get(f"/api/sessions/{SID}/analysis").json()
        self.assertEqual(before["counts"], after["counts"])
        self.assertEqual(score_map(after)[("C1", "JA", "P1")]["raw"], 9.9)

    def test_10_confirm_rolls_back_on_failure(self):
        before = client.get(f"/api/sessions/{SID}/analysis").json()
        p = preview(HEADER + "C1,JA,P1,2.2\n").json()

        original_merge = db.merge_scores

        def boom(conn, session_id, rows):
            # Simulate a failure after the UPDATE/DELETE statements would run:
            # the surrounding transaction must roll every change back.
            conn.execute("UPDATE scores SET score = -999 WHERE session_id = ?", (session_id,))
            raise RuntimeError("simulated mid-transaction failure")

        db.merge_scores = boom
        quiet_client = TestClient(app, raise_server_exceptions=False)
        try:
            r = quiet_client.post(
                f"/api/sessions/{SID}/merge/confirm",
                json={"records": p["records"], "basis": p["basis"]},
            )
        finally:
            db.merge_scores = original_merge
        self.assertEqual(r.status_code, 500)

        after = client.get(f"/api/sessions/{SID}/analysis").json()
        self.assertEqual(after["counts"], before["counts"])
        m = score_map(after)
        self.assertEqual(m[("C1", "JA", "P1")]["raw"], 9.9)  # no phantom -999
        self.assertEqual(len(m), before["counts"]["scores"])  # no added/removed leaked


if __name__ == "__main__":
    unittest.main()
