import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import Database, DomainError, cue_digests, seed_demo


class SubtitleQCSignatureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.db = Database(self.db_path)
        seed = seed_demo(self.db)
        self.project, self.version = seed["project"], seed["version"]
        self.db.assign(self.version, "alice", {"user": "bob", "role": "translator"}, "owner")
        self.db.assign(self.version, "alice", {"user": "carol", "role": "reviewer"}, "owner")

    def tearDown(self):
        self.tmp.cleanup()

    def _two_cues(self):
        first = self.db.save_cue(self.version, "bob", {"cue_index": 1, "start_ms": 1000, "end_ms": 3000, "text": "seal 海豹在冰面", "expected_revision": 0})
        second = self.db.save_cue(self.version, "bob", {"cue_index": 2, "start_ms": 3500, "end_ms": 5000, "text": "另一句", "expected_revision": 1})
        return first, second

    def _sign_all(self):
        self.db.submit(self.version, "bob")
        return self.db.sign_cues(self.version, "carol", {"all": True}, "reviewer")

    def test_signature_binds_to_current_summary_and_only_edited_cue_invalidates(self):
        first, second = self._two_cues()
        self._sign_all()
        sigs = {s["cue_id"]: s for s in self.db.list_signatures(self.version)}
        self.assertTrue(sigs[first["id"]]["matches_current"])
        self.assertTrue(sigs[second["id"]]["matches_current"])

        # Translator edits only cue 1 during review.
        self.db.save_cue(self.version, "bob", {"cue_id": first["id"], "cue_index": 1, "start_ms": 1000, "end_ms": 2800, "text": "seal 海豹在冰面", "expected_revision": 2})
        sigs = {s["cue_id"]: s for s in self.db.list_signatures(self.version)}
        self.assertFalse(sigs[first["id"]]["matches_current"])
        self.assertEqual(sigs[first["id"]]["status"], "invalid")
        self.assertIn("时间轴", sigs[first["id"]]["invalid_reason"])
        # The untouched cue keeps its valid signature.
        self.assertTrue(sigs[second["id"]]["matches_current"])

        # Whole-version approval must stop because cue 1 no longer matches.
        with self.assertRaisesRegex(DomainError, "1"):
            self.db.review(self.version, "carol", {"decision": "approve"}, "reviewer")

        # Re-sign the changed cue; approval then succeeds.
        result = self.db.sign_cues(self.version, "carol", {"cue_ids": [first["id"]]}, "reviewer")
        self.assertEqual(result["remaining_cue_indexes"], [])
        approved = self.db.review(self.version, "carol", {"decision": "approve"}, "reviewer")
        self.assertEqual(approved["status"], "approved")

    def test_delivery_checks_each_cue_against_current_summary(self):
        first, second = self._two_cues()
        self._sign_all()
        # Cue 2 changes while still in review: only its signature drops.
        self.db.save_cue(self.version, "bob", {"cue_id": second["id"], "cue_index": 2, "start_ms": 3500, "end_ms": 4900, "text": "另一句改过", "expected_revision": 2})
        # Re-signing cue 1 (already valid) must not paper over the missing cue 2.
        result = self.db.sign_cues(self.version, "carol", {"cue_ids": [first["id"]]}, "reviewer")
        self.assertEqual(result["remaining_cue_indexes"], [2])
        with self.assertRaisesRegex(DomainError, r"无法整版通过.*2"):
            self.db.review(self.version, "carol", {"decision": "approve"}, "reviewer")
        # Once cue 2 is signed against the new content, the gate clears.
        self.db.sign_cues(self.version, "carol", {"cue_ids": [second["id"]]}, "reviewer")
        approved = self.db.review(self.version, "carol", {"decision": "approve"}, "reviewer")
        self.assertEqual(approved["status"], "approved")
        self.db.deliver(self.version, "alice")

    def test_resumable_batch_does_not_duplicate_signatures(self):
        first, second = self._two_cues()
        self.db.submit(self.version, "bob")
        payload = {"cue_ids": [first["id"], second["id"]], "batch_key": "batch-1"}
        first_run = self.db.sign_cues(self.version, "carol", payload, "reviewer")
        self.assertEqual(sorted(first_run["signed_cue_indexes"]), [1, 2])
        # Re-post the identical batch after an interruption: no new signatures.
        second_run = self.db.sign_cues(self.version, "carol", payload, "reviewer")
        self.assertEqual(second_run["signed_cue_indexes"], [])
        self.assertEqual(sorted(second_run["already_signed_cue_indexes"]), [1, 2])
        with sqlite3.connect(self.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM cue_signatures WHERE version_id=?", (self.version,)).fetchone()[0]
            batches = conn.execute("SELECT COUNT(*) FROM sign_batches WHERE version_id=?", (self.version,)).fetchone()[0]
        self.assertEqual(count, 2)
        self.assertEqual(batches, 1)

    def test_re_sign_invalid_cue_reuses_row_and_clears_reason(self):
        first, _ = self._two_cues()
        self._sign_all()
        self.db.save_cue(self.version, "bob", {"cue_id": first["id"], "cue_index": 1, "start_ms": 1000, "end_ms": 2600, "text": "海豹文本变了", "expected_revision": 2})
        self.db.sign_cues(self.version, "carol", {"cue_ids": [first["id"]]}, "reviewer")
        sigs = self.db.list_signatures(self.version)
        cue1 = next(s for s in sigs if s["cue_id"] == first["id"])
        self.assertEqual(cue1["status"], "valid")
        self.assertEqual(cue1["invalid_reason"], "")
        self.assertTrue(cue1["matches_current"])
        with sqlite3.connect(self.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM cue_signatures WHERE cue_id=?", (first["id"],)).fetchone()[0]
        self.assertEqual(count, 1)

    def test_full_flow_lock_delivery(self):
        first, _ = self._two_cues()
        self._sign_all()
        self.db.review(self.version, "carol", {"decision": "approve", "comment": "通过"}, "reviewer")
        self.db.lock(self.version, "alice")
        delivery = self.db.deliver(self.version, "alice")
        self.assertEqual(len(delivery["snapshot_hash"]), 64)
        manifest = json.loads(delivery["manifest"])
        for cue in manifest["cues"]:
            expected = cue_digests(cue["cue_index"], cue["start_ms"], cue["end_ms"], cue["text"])
            self.assertEqual(cue["content_digest"], expected["content"])
        # Locked versions stay frozen.
        with self.assertRaisesRegex(DomainError, "只有草稿或复核"):
            self.db.save_cue(self.version, "bob", {"cue_id": first["id"], "cue_index": 1, "start_ms": 1000, "end_ms": 2500, "text": "海豹", "expected_revision": 2})

    def test_delivery_refuses_stale_or_missing_signature(self):
        first, second = self._two_cues()
        self._sign_all()
        # Build an approved state, then tamper directly to model a signature
        # whose stored digest predates a content change (delivery must not trust it).
        self.db.review(self.version, "carol", {"decision": "approve"}, "reviewer")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE cues SET end_ms=4900 WHERE id=?", (second["id"],))
        with self.assertRaisesRegex(DomainError, r"停止交付.*2"):
            self.db.deliver(self.version, "alice")

    def test_reviewer_permissions_and_status(self):
        self._two_cues()
        with self.assertRaisesRegex(DomainError, "复核中的版本"):
            self.db.sign_cues(self.version, "carol", {"all": True}, "reviewer")
        self.db.submit(self.version, "bob")
        with self.assertRaisesRegex(DomainError, "复核权限"):
            self.db.sign_cues(self.version, "bob", {"all": True}, "translator")

    def test_legacy_approved_version_backfills_from_current_content(self):
        first, second = self._two_cues()
        # Drop the new tables' rows and emulate a legacy version-wide approval.
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM cue_signatures")
            conn.execute("DELETE FROM sign_batches")
            conn.execute("INSERT INTO reviews(version_id,reviewer,decision,comment,created_at) VALUES(?,?,?,?,?)",
                         (self.version, "carol", "approve", "legacy", "2026-01-01T00:00:00+00:00"))
            conn.execute("UPDATE versions SET status='approved' WHERE id=?", (self.version,))
        # Reopen -> migration backfills per-cue signatures from current content.
        migrated = Database(self.db_path)
        sigs = migrated.list_signatures(self.version)
        self.assertEqual({s["status"] for s in sigs}, {"valid"})
        self.assertTrue(all(s["matches_current"] for s in sigs))
        self.assertEqual({s["reviewer"] for s in sigs}, {"carol"})
        # Idempotent: a second reopen creates no extra rows.
        again = Database(self.db_path)
        with sqlite3.connect(self.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM cue_signatures").fetchone()[0]
        self.assertEqual(count, 2)
        self.assertEqual(len(again.list_signatures(self.version)), 2)

    def test_legacy_delivered_version_backfills_from_snapshot(self):
        first, second = self._two_cues()
        self._sign_all()
        self.db.review(self.version, "carol", {"decision": "approve"}, "reviewer")
        self.db.deliver(self.version, "alice")
        manifest_json = self.db.list_deliveries()[0]["manifest"]
        manifest = json.loads(manifest_json)
        delivered_texts = {c["cue_index"]: c["text"] for c in manifest["cues"]}
        # Simulate legacy: wipe cue signatures, keep delivery + status delivered.
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM cue_signatures")
            conn.execute("DELETE FROM sign_batches")
            conn.execute("INSERT INTO reviews(version_id,reviewer,decision,comment,created_at) VALUES(?,?,?,?,?)",
                         (self.version, "carol", "approve", "legacy", "2026-01-01T00:00:00+00:00"))
        migrated = Database(self.db_path)
        sigs = migrated.list_signatures(self.version)
        by_index = {s["signed_cue_index"]: s for s in sigs}
        for cue_index, text in delivered_texts.items():
            self.assertEqual(by_index[cue_index]["signed_text"], text)
        self.assertTrue(all(s["status"] == "valid" for s in sigs))


class ValidationTest(unittest.TestCase):
    def test_revision_overlap_glossary_and_permissions(self):
        tmp = tempfile.TemporaryDirectory()
        db = Database(Path(tmp.name) / "test.db")
        seed = seed_demo(db)
        version = seed["version"]
        db.assign(version, "alice", {"user": "bob", "role": "translator"}, "owner")
        first = db.save_cue(version, "bob", {"cue_index": 1, "start_ms": 1000, "end_ms": 3000, "text": "海豹", "expected_revision": 0})
        with self.assertRaisesRegex(DomainError, "其他成员修改"):
            db.save_cue(version, "bob", {"cue_id": first["id"], "cue_index": 1, "start_ms": 1000, "end_ms": 2500, "text": "海豹", "expected_revision": 0})
        with self.assertRaisesRegex(DomainError, "重叠"):
            db.save_cue(version, "bob", {"cue_index": 2, "start_ms": 2500, "end_ms": 4000, "text": "另一句", "expected_revision": 1})
        with self.assertRaisesRegex(DomainError, "禁用译法"):
            db.save_cue(version, "bob", {"cue_index": 2, "start_ms": 3500, "end_ms": 4000, "text": "密封装置", "expected_revision": 1})
        with self.assertRaisesRegex(DomainError, "权限"):
            db.save_cue(version, "carol", {"cue_index": 2, "start_ms": 3500, "end_ms": 4000, "text": "海豹", "expected_revision": 1})
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
