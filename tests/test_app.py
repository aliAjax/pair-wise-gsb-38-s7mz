import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from app import STATIC
from app import DomainError
from subtitle_db import Database, ROOT
from validation import cue_digest


def make_db():
    tmp = tempfile.TemporaryDirectory()
    db = Database(Path(tmp.name) / "test.db")
    return tmp, db


class SubtitleQCFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        from subtitle_db import seed_demo
        seed = seed_demo(self.db)
        self.project, self.version = seed["project"], seed["version"]
        self.db.assign(self.version, "alice", {"user": "bob", "role": "translator"}, "owner")
        self.db.assign(self.version, "alice", {"user": "carol", "role": "reviewer"}, "owner")

    def tearDown(self):
        self.tmp.cleanup()

    def _cue(self, index=1, start=1000, end=3000, text="seal 海豹在冰面", rev=0, cue_id=None):
        payload = {"cue_index": index, "start_ms": start, "end_ms": end,
                   "text": text, "expected_revision": rev}
        if cue_id is not None:
            payload["cue_id"] = cue_id
        return self.db.save_cue(self.version, "bob", payload)

    def test_full_review_lock_delivery_and_overwrite_protection(self):
        cue = self._cue()
        self.assertEqual(cue["version_revision"], 1)
        comment = self.db.add_comment(self.version, "carol", {"cue_id": cue["id"], "time_ms": 1200, "body": "术语正确，请确认冻结时间"}, "reviewer")
        self.assertEqual(comment["time_ms"], 1200)
        self.db.submit(self.version, "bob")
        # 整版批准前必须有对应当前内容的逐句签署
        batch = self.db.batch_approve_cues(self.version, "carol", {"batch_id": "b1"})
        self.assertEqual(batch["signed"], [1])
        approved = self.db.review(self.version, "carol", {"decision": "approve", "comment": "通过"}, "reviewer")
        self.assertEqual(approved["status"], "approved")
        self.db.lock(self.version, "alice")
        delivery = self.db.deliver(self.version, "alice")
        self.assertEqual(len(delivery["snapshot_hash"]), 64)
        manifest = json.loads(delivery["manifest"])
        self.assertEqual(manifest["cue_approvals"][0]["signatures"][0]["reviewer"], "carol")
        with self.assertRaisesRegex(DomainError, "只有草稿"):
            self.db.save_cue(self.version, "bob", {"cue_id": cue["id"], "cue_index": 1, "start_ms": 1000, "end_ms": 2500, "text": "海豹", "expected_revision": 1})

    def test_revision_overlap_glossary_and_permissions(self):
        first = self._cue(text="海豹")
        with self.assertRaisesRegex(DomainError, "其他成员修改"):
            self.db.save_cue(self.version, "bob", {"cue_id": first["id"], "cue_index": 1, "start_ms": 1000, "end_ms": 2500, "text": "海豹", "expected_revision": 0})
        with self.assertRaisesRegex(DomainError, "重叠"):
            self._cue(index=2, start=2500, end=4000, text="另一句", rev=1)
        with self.assertRaisesRegex(DomainError, "禁用译法"):
            self._cue(index=2, start=3500, end=4000, text="密封装置", rev=1)
        with self.assertRaisesRegex(DomainError, "权限"):
            self.db.save_cue(self.version, "carol", {"cue_index": 2, "start_ms": 3500, "end_ms": 4000, "text": "海豹", "expected_revision": 1})


class PerCueSignatureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        from subtitle_db import seed_demo
        seed = seed_demo(self.db)
        self.version = seed["version"]
        self.db.assign(self.version, "alice", {"user": "bob", "role": "translator"}, "owner")
        self.db.assign(self.version, "alice", {"user": "carol", "role": "reviewer"}, "owner")
        self.c1 = self.db.save_cue(self.version, "bob", {"cue_index": 1, "start_ms": 1000, "end_ms": 3000, "text": "海豹在冰面", "expected_revision": 0})
        self.c2 = self.db.save_cue(self.version, "bob", {"cue_index": 2, "start_ms": 4000, "end_ms": 6000, "text": "第二句字幕", "expected_revision": 1})
        self.db.submit(self.version, "bob")

    def tearDown(self):
        self.tmp.cleanup()

    def _valid_count(self, cue_id, reviewer="carol"):
        with self.db.connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) c FROM cue_approvals WHERE cue_id=? AND reviewer=? AND status='valid'",
                (cue_id, reviewer),
            ).fetchone()["c"]

    def test_signature_records_digest_and_modifying_one_cue_only_invalidates_that_cue(self):
        self.db.batch_approve_cues(self.version, "carol", {"batch_id": "b1"})
        # 翻译在复核中改了第 1 句文本：只让第 1 句签署失效并记录原因
        saved = self.db.save_cue(self.version, "bob", {
            "cue_id": self.c1["id"], "cue_index": 1, "start_ms": 1000, "end_ms": 3000,
            "text": "海豹趴在冰面", "expected_revision": 2,
        })
        self.assertEqual(saved["invalidated_approvals"], 1)
        self.assertEqual(saved["invalidation_reason"], "文本已修改")
        self.assertEqual(self._valid_count(self.c1["id"]), 0)
        self.assertEqual(self._valid_count(self.c2["id"]), 1)
        view = {a["cue_id"]: a for a in self.db.list_approvals(self.version)}
        self.assertFalse(view[self.c1["id"]]["has_valid_signature"])
        self.assertEqual(view[self.c1["id"]]["invalidated"][0]["reason"], "文本已修改")
        self.assertTrue(view[self.c2["id"]]["has_valid_signature"])

    def test_timeline_change_only_invalidates_with_timeline_reason(self):
        self.db.batch_approve_cues(self.version, "carol", {"batch_id": "b1"})
        saved = self.db.save_cue(self.version, "bob", {
            "cue_id": self.c1["id"], "cue_index": 1, "start_ms": 1100, "end_ms": 3000,
            "text": "海豹在冰面", "expected_revision": 2,
        })
        self.assertEqual(saved["invalidation_reason"], "时间轴已修改")

    def test_unchanged_resave_keeps_signatures_valid(self):
        self.db.batch_approve_cues(self.version, "carol", {"batch_id": "b1"})
        self.db.save_cue(self.version, "bob", {
            "cue_id": self.c1["id"], "cue_index": 1, "start_ms": 1000, "end_ms": 3000,
            "text": "海豹在冰面", "expected_revision": 2,
        })
        self.assertEqual(self._valid_count(self.c1["id"]), 1)

    def test_approve_and_delivery_list_missing_indexes_and_stop(self):
        # 只签第 1 句：整版批准必须列出第 2 句并停住
        with self.assertRaisesRegex(DomainError, r"缺少当前内容的有效签署.*2"):
            self.db.review(self.version, "carol", {"decision": "approve"}, "reviewer")
        self.db.batch_approve_cues(self.version, "carol", {"batch_id": "b1", "cue_indexes": [1]})
        with self.assertRaisesRegex(DomainError, r"缺少当前内容的有效签署.*2"):
            self.db.review(self.version, "carol", {"decision": "approve"}, "reviewer")
        # 即便版本处于 approved（模拟旧数据/强制流转），交付也要逐句核对、列句号、不留交付记录
        with self.db.connect() as conn:
            conn.execute("UPDATE versions SET status='approved' WHERE id=?", (self.version,))
        with self.assertRaisesRegex(DomainError, r"交付已停止.*句号.*2"):
            self.db.deliver(self.version, "alice")
        with self.db.connect() as conn:
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM deliveries WHERE version_id=?", (self.version,)
            ).fetchone())
        self.assertEqual(self.db.delivery_readiness(self.version)["missing_indexes"], [2])

    def test_changed_cue_after_signature_cannot_be_approved_or_delivered(self):
        self.db.batch_approve_cues(self.version, "carol", {"batch_id": "b1"})
        self.db.save_cue(self.version, "bob", {
            "cue_id": self.c2["id"], "cue_index": 2, "start_ms": 4000, "end_ms": 6000,
            "text": "第二句字幕（修订）", "expected_revision": 2,
        })
        with self.assertRaisesRegex(DomainError, "2"):
            self.db.review(self.version, "carol", {"decision": "approve"}, "reviewer")
        # 重新签署改动句后才能通过
        again = self.db.batch_approve_cues(self.version, "carol", {"batch_id": "b2"})
        self.assertEqual(again["signed"], [2])
        approved = self.db.review(self.version, "carol", {"decision": "approve"}, "reviewer")
        self.assertEqual(approved["status"], "approved")
        delivery = self.db.deliver(self.version, "alice")
        self.assertEqual(len(delivery["snapshot_hash"]), 64)

    def test_resign_after_revert_and_no_duplicate_valid_rows(self):
        self.db.batch_approve_cues(self.version, "carol", {"batch_id": "b1"})
        self.db.save_cue(self.version, "bob", {
            "cue_id": self.c1["id"], "cue_index": 1, "start_ms": 1000, "end_ms": 3000,
            "text": "海豹趴在冰面", "expected_revision": 2,
        })
        self.db.save_cue(self.version, "bob", {
            "cue_id": self.c1["id"], "cue_index": 1, "start_ms": 1000, "end_ms": 3000,
            "text": "海豹在冰面", "expected_revision": 3,
        })
        # 改回原文：旧签署已失效，必须能重新签署，且有效行仍只有一条
        self.db.batch_approve_cues(self.version, "carol", {"batch_id": "b2", "cue_indexes": [1]})
        self.assertEqual(self._valid_count(self.c1["id"]), 1)
        with self.db.connect() as conn:
            history = conn.execute(
                "SELECT status FROM cue_approvals WHERE cue_id=? AND reviewer='carol' ORDER BY id",
                (self.c1["id"],),
            ).fetchall()
        self.assertEqual([r["status"] for r in history], ["invalidated", "valid"])

    def test_batch_is_idempotent_and_recoverable(self):
        first = self.db.batch_approve_cues(self.version, "carol", {"batch_id": "resume-1"})
        self.assertEqual(len(first["signed"]), 2)
        # 异常中断后原样重放：全部幂等跳过，不生成两份签署
        replay = self.db.batch_approve_cues(self.version, "carol", {"batch_id": "resume-1"})
        self.assertEqual(replay["signed"], [])
        self.assertEqual(sorted(replay["already_signed"]), [1, 2])
        with self.db.connect() as conn:
            total = conn.execute("SELECT COUNT(*) c FROM cue_approvals WHERE batch_id='resume-1'").fetchone()["c"]
        self.assertEqual(total, 2)

    def test_duplicate_batch_concurrent_creates_single_signature(self):
        errors = []

        def run():
            try:
                self.db.batch_approve_cues(self.version, "carol", {"batch_id": "concurrent-1", "cue_indexes": [1]})
            except Exception as exc:  # noqa: BLE001 - 序列化冲突也算失败，记录下来
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(self._valid_count(self.c1["id"]), 1)

    def test_concurrent_save_and_sign_never_leaves_stale_valid_signature(self):
        stop = threading.Event()
        sign_errors, save_errors = [], []

        def signer():
            while not stop.is_set():
                try:
                    self.db.batch_approve_cues(self.version, "carol", {})
                except DomainError as exc:
                    # 状态被退回等竞争结果可接受；数据库锁错误不可接受
                    if "locked" in str(exc) or "锁定" in str(exc):
                        sign_errors.append(exc)

        def saver():
            texts = ["海豹在冰面", "海豹在冰面上"]
            i = 0
            while not stop.is_set():
                i += 1
                try:
                    self.db.save_cue(self.version, "bob", {
                        "cue_id": self.c1["id"], "cue_index": 1, "start_ms": 1000,
                        "end_ms": 3000, "text": texts[i % 2],
                    })
                except DomainError as exc:
                    if "锁定" in str(exc) or "重叠" in str(exc):
                        save_errors.append(exc)

        t1 = threading.Thread(target=signer)
        t2 = threading.Thread(target=saver)
        t1.start(); t2.start()
        stop.wait(2.0)
        stop.set()
        t1.join(5); t2.join(5)
        self.assertEqual(sign_errors, [])
        self.assertEqual(save_errors, [])
        # 核心不变量：每条有效签署的摘要必须等于当前内容摘要
        with self.db.connect() as conn:
            cue = conn.execute("SELECT * FROM cues WHERE id=?", (self.c1["id"],)).fetchone()
            current = cue_digest(cue["cue_index"], cue["start_ms"], cue["end_ms"], cue["text"])
            rows = conn.execute(
                "SELECT content_digest FROM cue_approvals WHERE cue_id=? AND status='valid'",
                (self.c1["id"],),
            ).fetchall()
        self.assertTrue(all(r["content_digest"] == current for r in rows))
        self.assertLessEqual(len(rows), 1)


class LegacyMigrationTest(unittest.TestCase):
    """旧库（只有整版 reviews / deliveries）升级为句级签署。"""

    LEGACY_SCHEMA = """
        CREATE TABLE projects(id INTEGER PRIMARY KEY, name TEXT, source_language TEXT,
            duration_ms INTEGER, owner TEXT, media_name TEXT, media_sha256 TEXT, created_at TEXT);
        CREATE TABLE versions(id INTEGER PRIMARY KEY, project_id INTEGER, language TEXT,
            version_no INTEGER, parent_id INTEGER, status TEXT, revision INTEGER,
            created_by TEXT, created_at TEXT, updated_at TEXT);
        CREATE TABLE assignments(id INTEGER PRIMARY KEY, version_id INTEGER, user TEXT,
            role TEXT, assigned_by TEXT, created_at TEXT);
        CREATE TABLE cues(id INTEGER PRIMARY KEY, version_id INTEGER, cue_index INTEGER,
            start_ms INTEGER, end_ms INTEGER, text TEXT, updated_by TEXT, updated_at TEXT);
        CREATE TABLE comments(id INTEGER PRIMARY KEY, version_id INTEGER, cue_id INTEGER,
            user TEXT, time_ms INTEGER, body TEXT, status TEXT DEFAULT 'open', created_at TEXT);
        CREATE TABLE glossaries(id INTEGER PRIMARY KEY, project_id INTEGER, source_term TEXT,
            required_translation TEXT, forbidden_terms TEXT DEFAULT '[]', notes TEXT DEFAULT '', created_at TEXT);
        CREATE TABLE reviews(id INTEGER PRIMARY KEY, version_id INTEGER, reviewer TEXT,
            decision TEXT, comment TEXT DEFAULT '', created_at TEXT);
        CREATE TABLE deliveries(id INTEGER PRIMARY KEY, version_id INTEGER,
            supersedes_version_id INTEGER, snapshot_hash TEXT, manifest TEXT,
            delivered_by TEXT, created_at TEXT);
        CREATE TABLE audit_log(id INTEGER PRIMARY KEY, actor TEXT, action TEXT,
            entity_type TEXT, entity_id INTEGER, details TEXT, created_at TEXT);
    """

    def _legacy_db(self, path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(path)
        conn.executescript(self.LEGACY_SCHEMA)
        conn.execute("INSERT INTO projects VALUES(1,'旧项目','en',120000,'alice','m.mp4',?, '2020-01-01T00:00:00+00:00')", ("a" * 64,))
        return conn

    def test_approved_version_backfilled_from_current_content(self):
        tmp = tempfile.TemporaryDirectory()
        path = Path(tmp.name) / "legacy.db"
        conn = self._legacy_db(path)
        conn.execute("INSERT INTO versions VALUES(1,1,'zh-CN',1,NULL,'approved',3,'bob','2020-01-01T00:00:00+00:00','2020-01-02T00:00:00+00:00')")
        conn.execute("INSERT INTO cues VALUES(1,1,1,1000,3000,'旧版海豹字幕','bob','2020-01-02T00:00:00+00:00')")
        conn.execute("INSERT INTO reviews VALUES(1,1,'carol','approve','整版通过','2020-01-03T00:00:00+00:00')")
        conn.commit(); conn.close()

        db = Database(path)  # 触发升级
        with db.connect() as c:
            self.assertEqual(int(c.execute("PRAGMA user_version").fetchone()[0]), 2)
            rows = c.execute(
                "SELECT * FROM cue_approvals WHERE version_id=1 AND status='valid'"
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "legacy_review")
        self.assertEqual(rows[0]["reviewer"], "carol")
        self.assertEqual(rows[0]["content_digest"], cue_digest(1, 1000, 3000, "旧版海豹字幕"))
        self.assertTrue(db.delivery_readiness(1)["ready"])
        tmp.cleanup()

    def test_delivered_version_backfilled_from_snapshot(self):
        tmp = tempfile.TemporaryDirectory()
        path = Path(tmp.name) / "legacy2.db"
        conn = self._legacy_db(path)
        conn.execute("INSERT INTO versions VALUES(1,1,'zh-CN',1,NULL,'delivered',3,'bob','2020-01-01T00:00:00+00:00','2020-01-04T00:00:00+00:00')")
        # 当前第 1 句已被改动；第 2 句当前库里甚至不存在 —— 仍要从交付快照补齐签署
        conn.execute("INSERT INTO cues VALUES(1,1,1,1000,3000,'后来改动的文本','bob','2020-01-05T00:00:00+00:00')")
        snapshot_cues = [
            {"cue_index": 1, "start_ms": 1000, "end_ms": 3000, "text": "交付时的海豹"},
            {"cue_index": 2, "start_ms": 4000, "end_ms": 6000, "text": "交付时的第二句"},
        ]
        manifest = {"project_id": 1, "version_id": 1, "language": "zh-CN", "version_no": 1,
                    "cues": snapshot_cues, "glossary": []}
        raw = json.dumps(manifest, ensure_ascii=False, sort_keys=True)
        import hashlib
        digest = hashlib.sha256(raw.encode()).hexdigest()
        conn.execute("INSERT INTO deliveries VALUES(1,1,NULL,?,?, 'alice','2020-01-04T00:00:00+00:00')", (digest, raw))
        conn.commit(); conn.close()

        db = Database(path)
        with db.connect() as c:
            rows = c.execute(
                "SELECT cue_index,cue_id,content_digest,source FROM cue_approvals WHERE version_id=1 ORDER BY cue_index"
            ).fetchall()
        self.assertEqual([r["source"] for r in rows], ["legacy_delivery", "legacy_delivery"])
        self.assertEqual(rows[0]["content_digest"], cue_digest(1, 1000, 3000, "交付时的海豹"))
        self.assertIsNone(rows[1]["cue_id"])  # 当前库已不存在的句子按快照独立留存
        self.assertEqual(rows[1]["content_digest"], cue_digest(2, 4000, 6000, "交付时的第二句"))
        # 当前内容与交付快照不一致：当前视图如实显示第 1 句无有效签署
        self.assertEqual(db.delivery_readiness(1)["missing_indexes"], [1])
        tmp.cleanup()

    def test_fresh_database_is_version_two(self):
        tmp = tempfile.TemporaryDirectory()
        db = Database(Path(tmp.name) / "fresh.db")
        with db.connect() as c:
            self.assertEqual(int(c.execute("PRAGMA user_version").fetchone()[0]), 2)
        tmp.cleanup()


class SeparationTest(unittest.TestCase):
    def test_data_validation_and_pages_kept_separate(self):
        self.assertTrue((ROOT / "subtitle_db.py").is_file())
        self.assertTrue((ROOT / "validation.py").is_file())
        self.assertTrue((STATIC / "index.html").is_file())
        self.assertTrue((STATIC / "styles.css").is_file())
        self.assertTrue((STATIC / "app.js").is_file())
        app_py = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertNotIn("CREATE TABLE", app_py)  # 数据层不在 HTTP 文件里
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("<script>\n", html.replace("\r\n", "\n"))  # 页面逻辑独立成 js
        self.assertIn('href="/static/styles.css"', html)
        self.assertIn('src="/static/app.js"', html)


if __name__ == "__main__":
    unittest.main()
