"""数据层：项目、字幕、句级签署、复核状态机与确定性交付。

复核签署绑定到“每一句字幕确认当时的内容摘要”：
- 字幕任何字段被改动，只让该句的旧签署失效并记录原因；
- 保存、签署、整版批准、交付都在 BEGIN IMMEDIATE 事务里按当前摘要判断，
  翻译保存和复核批准并发时必须排队，不可能签署到旧内容后仍交付；
- 交付逐句核对当前摘要，任一句缺少有效签署就列出句号并停住；
- 批量签署用批次号幂等，异常中断后重复提交不会产生两份签署；
- 旧库（user_version=1）升级时：整版通过记录按当前内容补成句级签署，
  已经交付的版本从交付快照补齐。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from validation import (
    DomainError,
    cue_digest,
    enforce_glossary,
    format_cue_indexes,
    invalidation_reason,
    parse_cue,
)

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "subtitle_qc.db"

SCHEMA_VERSION = 2


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str | os.PathLike[str] = DEFAULT_DB):
        self.path = str(path)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    source_language TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL CHECK(duration_ms > 0),
                    owner TEXT NOT NULL,
                    media_name TEXT NOT NULL,
                    media_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER NOT NULL REFERENCES projects(id),
                    language TEXT NOT NULL,
                    version_no INTEGER NOT NULL,
                    parent_id INTEGER REFERENCES versions(id),
                    status TEXT NOT NULL DEFAULT 'draft',
                    revision INTEGER NOT NULL DEFAULT 0,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(project_id,language,version_no)
                );
                CREATE TABLE IF NOT EXISTS assignments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
                    user TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('translator','timeline','reviewer')),
                    assigned_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(version_id,user,role)
                );
                CREATE TABLE IF NOT EXISTS cues (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
                    cue_index INTEGER NOT NULL,
                    start_ms INTEGER NOT NULL CHECK(start_ms >= 0),
                    end_ms INTEGER NOT NULL CHECK(end_ms > start_ms),
                    text TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(version_id,cue_index)
                );
                CREATE TABLE IF NOT EXISTS comments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
                    cue_id INTEGER REFERENCES cues(id) ON DELETE SET NULL,
                    user TEXT NOT NULL,
                    time_ms INTEGER NOT NULL CHECK(time_ms >= 0),
                    body TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS glossaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    source_term TEXT NOT NULL,
                    required_translation TEXT NOT NULL,
                    forbidden_terms TEXT NOT NULL DEFAULT '[]',
                    notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(project_id,source_term)
                );
                CREATE TABLE IF NOT EXISTS reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
                    reviewer TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    comment TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version_id INTEGER NOT NULL UNIQUE REFERENCES versions(id),
                    supersedes_version_id INTEGER REFERENCES versions(id),
                    snapshot_hash TEXT NOT NULL,
                    manifest TEXT NOT NULL,
                    delivered_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS approval_batches (
                    id TEXT PRIMARY KEY,
                    version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
                    reviewer TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'completed',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cue_approvals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
                    cue_id INTEGER REFERENCES cues(id) ON DELETE CASCADE,
                    batch_id TEXT REFERENCES approval_batches(id) ON DELETE SET NULL,
                    cue_index INTEGER NOT NULL,
                    reviewer TEXT NOT NULL,
                    content_digest TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'valid'
                        CHECK(status IN ('valid','invalidated')),
                    invalidated_reason TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'batch'
                        CHECK(source IN ('batch','legacy_review','legacy_delivery')),
                    signed_at TEXT NOT NULL,
                    invalidated_at TEXT,
                    UNIQUE(batch_id,cue_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS uniq_valid_cue_approval
                    ON cue_approvals(version_id,cue_id,content_digest,reviewer)
                    WHERE status='valid';
                CREATE INDEX IF NOT EXISTS idx_cue_approvals_lookup
                    ON cue_approvals(version_id,cue_id,status);
                """
            )
            self._migrate(conn)

    # ------------------------------------------------------------------ 迁移
    def _migrate(self, conn: sqlite3.Connection) -> None:
        """旧库升级到句级签署模型。

        user_version=1 的库只有整版 reviews/deliveries；升级时：
        - 已交付版本：按交付快照里每句当时的内容补句级签署（source=legacy_delivery）；
        - 其余整版通过记录：按当前 cues 内容补签（source=legacy_review）。
        """
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version >= SCHEMA_VERSION:
            return
        if version == 1 or self._looks_legacy(conn):
            delivered_ids = {
                int(r["version_id"]) for r in conn.execute("SELECT version_id FROM deliveries")
            }
            # 已交付的从交付快照补齐，快照记录的就是真正交付出去的内容。
            for delivery in conn.execute("SELECT * FROM deliveries ORDER BY id"):
                self._backfill_from_delivery(conn, delivery)
            # 整版通过但尚未交付的，按当前内容补成句级签署。
            approved_rows = conn.execute(
                """SELECT r.* FROM reviews r
                   WHERE r.decision='approve'
                     AND r.id IN (SELECT MAX(id) FROM reviews GROUP BY version_id)
                   ORDER BY r.id""",
            ).fetchall()
            for review_row in approved_rows:
                if int(review_row["version_id"]) in delivered_ids:
                    continue
                self._backfill_from_current_cues(
                    conn, int(review_row["version_id"]), review_row["reviewer"],
                    review_row["created_at"], "legacy_review",
                )
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @staticmethod
    def _looks_legacy(conn: sqlite3.Connection) -> bool:
        """全新空库 user_version 为 0 且没有数据；有整版复核或交付记录才算需要补数据的旧库。"""
        return bool(
            conn.execute("SELECT 1 FROM reviews LIMIT 1").fetchone()
            or conn.execute("SELECT 1 FROM deliveries LIMIT 1").fetchone()
        )

    def _insert_approval(
        self, conn: sqlite3.Connection, *, version_id: int, cue_id: int | None,
        cue_index: int, reviewer: str, digest: str, signed_at: str,
        source: str, batch_id: str | None = None,
    ) -> bool:
        """补签一条，已存在同摘要的有效签署则跳过。返回是否新插入。

        历史失效行只保留作审计痕迹，不阻止按同样内容重新补签。
        """
        existing = conn.execute(
            "SELECT id FROM cue_approvals WHERE version_id=? AND cue_id IS ? AND content_digest=? AND reviewer=? AND status='valid'",
            (version_id, cue_id, digest, reviewer),
        ).fetchone()
        if existing:
            return False
        conn.execute(
            """INSERT INTO cue_approvals(version_id,cue_id,batch_id,cue_index,reviewer,
                   content_digest,status,source,signed_at)
               VALUES(?,?,?,?,?,?, 'valid',?,?)""",
            (version_id, cue_id, batch_id, cue_index, reviewer, digest, source, signed_at),
        )
        return True

    def _backfill_from_delivery(self, conn: sqlite3.Connection, delivery: sqlite3.Row) -> None:
        version_id = int(delivery["version_id"])
        try:
            manifest = json.loads(delivery["manifest"])
        except (TypeError, ValueError):
            return
        snapshot_cues = manifest.get("cues", [])
        signed_at = delivery["created_at"]
        reviewer = delivery["delivered_by"]
        batch_id = f"legacy-delivery-{delivery['id']}"
        conn.execute(
            "INSERT OR IGNORE INTO approval_batches(id,version_id,reviewer,status,created_at) VALUES(?,?,?, 'completed',?)",
            (batch_id, version_id, reviewer, signed_at),
        )
        current_by_index = {
            int(r["cue_index"]): r
            for r in conn.execute("SELECT id,cue_index,start_ms,end_ms,text FROM cues WHERE version_id=?", (version_id,))
        }
        for item in snapshot_cues:
            digest = cue_digest(item["cue_index"], item["start_ms"], item["end_ms"], item["text"])
            cue_row = current_by_index.get(int(item["cue_index"]))
            # 只有当前仍是同一句且内容摘要完全一致时才关联当前 cue_id；
            # 内容已变或句子已不存在的，按快照独立留存（cue_id=NULL），历史签署不丢也不误挂。
            current_digest = (
                cue_digest(cue_row["cue_index"], cue_row["start_ms"], cue_row["end_ms"], cue_row["text"])
                if cue_row else None
            )
            cue_id = cue_row["id"] if cue_row and current_digest == digest else None
            self._insert_approval(
                conn, version_id=version_id, cue_id=cue_id, cue_index=int(item["cue_index"]),
                reviewer=reviewer, digest=digest, signed_at=signed_at,
                source="legacy_delivery", batch_id=batch_id,
            )

    def _backfill_from_current_cues(
        self, conn: sqlite3.Connection, version_id: int, reviewer: str,
        signed_at: str, source: str,
    ) -> None:
        cues = conn.execute(
            "SELECT id,cue_index,start_ms,end_ms,text FROM cues WHERE version_id=? ORDER BY cue_index",
            (version_id,),
        ).fetchall()
        if not cues:
            return
        batch_id = f"legacy-review-{version_id}"
        conn.execute(
            "INSERT OR IGNORE INTO approval_batches(id,version_id,reviewer,status,created_at) VALUES(?,?,?, 'completed',?)",
            (batch_id, version_id, reviewer, signed_at),
        )
        for row in cues:
            digest = cue_digest(row["cue_index"], row["start_ms"], row["end_ms"], row["text"])
            self._insert_approval(
                conn, version_id=version_id, cue_id=row["id"], cue_index=int(row["cue_index"]),
                reviewer=reviewer, digest=digest, signed_at=signed_at,
                source=source, batch_id=batch_id,
            )

    # ------------------------------------------------------------------ 辅助
    def _audit(self, conn: sqlite3.Connection, actor: str, action: str, entity_type: str,
               entity_id: int | None, details: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO audit_log(actor,action,entity_type,entity_id,details,created_at) VALUES(?,?,?,?,?,?)",
            (actor, action, entity_type, entity_id, json.dumps(details, ensure_ascii=False), utcnow()),
        )

    def _version(self, conn: sqlite3.Connection, version_id: int) -> sqlite3.Row:
        row = conn.execute(
            "SELECT v.*,p.owner,p.duration_ms FROM versions v JOIN projects p ON p.id=v.project_id WHERE v.id=?",
            (version_id,),
        ).fetchone()
        if not row:
            raise DomainError("字幕版本不存在", 404)
        return row

    def _can_edit(self, conn: sqlite3.Connection, version: sqlite3.Row, actor: str) -> bool:
        if actor == version["owner"]:
            return True
        return bool(conn.execute(
            "SELECT 1 FROM assignments WHERE version_id=? AND user=? AND role IN ('translator','timeline')",
            (version["id"], actor),
        ).fetchone())

    def _can_review(self, conn: sqlite3.Connection, version: sqlite3.Row, actor: str) -> bool:
        assigned = conn.execute(
            "SELECT 1 FROM assignments WHERE version_id=? AND user=? AND role='reviewer'",
            (version["id"], actor),
        ).fetchone()
        return bool(assigned) or actor == version["owner"]

    def _current_cues(self, conn: sqlite3.Connection, version_id: int) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM cues WHERE version_id=? ORDER BY cue_index", (version_id,)
        ).fetchall()

    def _missing_indexes(self, conn: sqlite3.Connection, version_id: int) -> list[int]:
        """逐句核对当前摘要，返回缺少有效签署的句号列表。"""
        missing: list[int] = []
        for cue in self._current_cues(conn, version_id):
            digest = cue_digest(cue["cue_index"], cue["start_ms"], cue["end_ms"], cue["text"])
            valid = conn.execute(
                "SELECT 1 FROM cue_approvals WHERE version_id=? AND cue_id=? AND status='valid' AND content_digest=? LIMIT 1",
                (version_id, cue["id"], digest),
            ).fetchone()
            if not valid:
                missing.append(int(cue["cue_index"]))
        return missing

    # ------------------------------------------------------------------ 项目
    def create_project(self, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        if role not in {"owner", "admin"}:
            raise DomainError("只有项目负责人可以创建项目", 403)
        name = str(payload.get("name", "")).strip()
        source_language = str(payload.get("source_language", "")).strip()
        media_name = str(payload.get("media_name", "")).strip()
        media_sha = str(payload.get("media_sha256", "")).lower()
        try:
            duration_ms = int(payload.get("duration_ms"))
        except (TypeError, ValueError) as exc:
            raise DomainError("成片时长必须是毫秒整数") from exc
        if not name or not source_language or not media_name or duration_ms <= 0 or len(media_sha) != 64:
            raise DomainError("项目名称、源语言、成片、时长或校验值不完整")
        with self.connect() as conn:
            try:
                cur = conn.execute(
                    "INSERT INTO projects(name,source_language,duration_ms,owner,media_name,media_sha256,created_at) VALUES(?,?,?,?,?,?,?)",
                    (name, source_language, duration_ms, actor, media_name, media_sha, utcnow()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("项目名称已存在", 409) from exc
            self._audit(conn, actor, "project.created", "project", cur.lastrowid, {"name": name})
            return dict(conn.execute("SELECT * FROM projects WHERE id=?", (cur.lastrowid,)).fetchone())

    def set_glossary(self, project_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        with self.connect() as conn:
            project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            if not project:
                raise DomainError("项目不存在", 404)
            if actor != project["owner"] and role != "admin":
                raise DomainError("只有项目负责人可以维护术语表", 403)
            source_term = str(payload.get("source_term", "")).strip()
            required = str(payload.get("required_translation", "")).strip()
            forbidden = payload.get("forbidden_terms", [])
            if not source_term or not required or not isinstance(forbidden, list):
                raise DomainError("术语、指定译法和禁用词格式不合法")
            conn.execute(
                """INSERT INTO glossaries(project_id,source_term,required_translation,forbidden_terms,notes,created_at)
                   VALUES(?,?,?,?,?,?) ON CONFLICT(project_id,source_term) DO UPDATE SET
                   required_translation=excluded.required_translation,forbidden_terms=excluded.forbidden_terms,notes=excluded.notes""",
                (project_id, source_term, required, json.dumps(forbidden, ensure_ascii=False), str(payload.get("notes", "")), utcnow()),
            )
            self._audit(conn, actor, "glossary.saved", "project", project_id, {"source_term": source_term})
        return {"project_id": project_id, "source_term": source_term, "required_translation": required, "forbidden_terms": forbidden}

    def create_version(self, project_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        language = str(payload.get("language", "")).strip()
        if not language:
            raise DomainError("目标语言不能为空")
        parent_id = payload.get("parent_id")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            if not project:
                raise DomainError("项目不存在", 404)
            if actor != project["owner"] and role != "admin":
                raise DomainError("只有项目负责人可以创建版本", 403)
            if parent_id is not None:
                parent = conn.execute("SELECT * FROM versions WHERE id=? AND project_id=?", (int(parent_id), project_id)).fetchone()
                if not parent or parent["language"] != language:
                    raise DomainError("父版本不存在或目标语言不一致", 409)
            next_no = int(conn.execute(
                "SELECT COALESCE(MAX(version_no),0)+1 value FROM versions WHERE project_id=? AND language=?",
                (project_id, language),
            ).fetchone()["value"])
            cur = conn.execute(
                "INSERT INTO versions(project_id,language,version_no,parent_id,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (project_id, language, next_no, parent_id, actor, utcnow(), utcnow()),
            )
            self._audit(conn, actor, "version.created", "version", cur.lastrowid, {"language": language, "version_no": next_no})
            return dict(conn.execute("SELECT * FROM versions WHERE id=?", (cur.lastrowid,)).fetchone())

    def assign(self, version_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        user = str(payload.get("user", "")).strip()
        assignment_role = str(payload.get("role", "")).strip()
        if not user or assignment_role not in {"translator", "timeline", "reviewer"}:
            raise DomainError("人员或角色不合法")
        with self.connect() as conn:
            version = conn.execute(
                "SELECT v.*,p.owner FROM versions v JOIN projects p ON p.id=v.project_id WHERE v.id=?",
                (version_id,),
            ).fetchone()
            if not version:
                raise DomainError("版本不存在", 404)
            if actor != version["owner"] and role != "admin":
                raise DomainError("只有项目负责人可以分配人员", 403)
            conn.execute(
                "INSERT OR IGNORE INTO assignments(version_id,user,role,assigned_by,created_at) VALUES(?,?,?,?,?)",
                (version_id, user, assignment_role, actor, utcnow()),
            )
            self._audit(conn, actor, "assignment.saved", "version", version_id, {"user": user, "role": assignment_role})
        return {"version_id": version_id, "user": user, "role": assignment_role}

    # ------------------------------------------------------------------ 字幕
    def save_cue(self, version_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            version = self._version(conn, version_id)
            if version["status"] not in {"draft", "review"}:
                raise DomainError("只有草稿或复核中的版本可以修改字幕", 409)
            if not self._can_edit(conn, version, actor):
                raise DomainError("没有该版本的翻译或时间轴权限", 403)
            expected = payload.get("expected_revision")
            if expected is not None and int(expected) != int(version["revision"]):
                raise DomainError("版本已被其他成员修改，请刷新后重试", 409)
            cue_index, start_ms, end_ms, text = parse_cue(payload, int(version["duration_ms"]))
            enforce_glossary(
                conn.execute("SELECT * FROM glossaries WHERE project_id=?", (version["project_id"],)).fetchall(),
                text,
            )
            cue_id = payload.get("cue_id")
            existing = None
            if cue_id is not None:
                existing = conn.execute("SELECT * FROM cues WHERE id=? AND version_id=?", (int(cue_id), version_id)).fetchone()
                if not existing:
                    raise DomainError("字幕条目不存在", 404)
            overlap = conn.execute(
                "SELECT * FROM cues WHERE version_id=? AND id<>? AND start_ms<? AND end_ms>? LIMIT 1",
                (version_id, int(cue_id or -1), end_ms, start_ms),
            ).fetchone()
            if overlap:
                raise DomainError("字幕时间轴发生重叠", 409)
            index_owner = conn.execute(
                "SELECT * FROM cues WHERE version_id=? AND cue_index=? AND id<>?",
                (version_id, cue_index, int(cue_id or -1)),
            ).fetchone()
            if index_owner:
                raise DomainError("字幕序号已被使用", 409)
            now = utcnow()
            if existing:
                # 只让真正被改动的句子失效：内容相同的重复保存不动任何签署。
                reason = invalidation_reason(existing, cue_index, start_ms, end_ms, text)
                conn.execute(
                    "UPDATE cues SET cue_index=?,start_ms=?,end_ms=?,text=?,updated_by=?,updated_at=? WHERE id=?",
                    (cue_index, start_ms, end_ms, text, actor, now, existing["id"]),
                )
                saved_id = existing["id"]
                invalidated = 0
                if reason:
                    cur = conn.execute(
                        "UPDATE cue_approvals SET status='invalidated',invalidated_reason=?,invalidated_at=? WHERE cue_id=? AND status='valid'",
                        (reason, now, saved_id),
                    )
                    invalidated = cur.rowcount
            else:
                cur = conn.execute(
                    "INSERT INTO cues(version_id,cue_index,start_ms,end_ms,text,updated_by,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (version_id, cue_index, start_ms, end_ms, text, actor, now),
                )
                saved_id = cur.lastrowid
                reason, invalidated = "", 0
            revision = int(version["revision"]) + 1
            conn.execute("UPDATE versions SET revision=?,updated_at=? WHERE id=?", (revision, now, version_id))
            self._audit(conn, actor, "cue.saved", "version", version_id, {
                "cue_id": saved_id, "revision": revision,
                "invalidated_approvals": invalidated, "reason": reason,
            })
            row = conn.execute("SELECT * FROM cues WHERE id=?", (saved_id,)).fetchone()
        return dict(row) | {"version_revision": revision, "invalidated_approvals": invalidated, "invalidation_reason": reason}

    def add_comment(self, version_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        body = str(payload.get("body", "")).strip()
        try:
            time_ms = int(payload.get("time_ms"))
        except (TypeError, ValueError) as exc:
            raise DomainError("评论时间必须是毫秒整数") from exc
        with self.connect() as conn:
            version = self._version(conn, version_id)
            allowed = actor == version["owner"] or conn.execute(
                "SELECT 1 FROM assignments WHERE version_id=? AND user=?", (version_id, actor)
            ).fetchone()
            if not allowed:
                raise DomainError("只有项目成员可以评论", 403)
            if not body or time_ms < 0 or time_ms > int(version["duration_ms"]):
                raise DomainError("评论内容或时间点不合法")
            cue_id = payload.get("cue_id")
            if cue_id is not None and not conn.execute(
                "SELECT 1 FROM cues WHERE id=? AND version_id=?", (int(cue_id), version_id)
            ).fetchone():
                raise DomainError("评论关联的字幕不存在", 404)
            cur = conn.execute(
                "INSERT INTO comments(version_id,cue_id,user,time_ms,body,created_at) VALUES(?,?,?,?,?,?)",
                (version_id, cue_id, actor, time_ms, body, utcnow()),
            )
            self._audit(conn, actor, "comment.added", "version", version_id, {"comment_id": cur.lastrowid, "time_ms": time_ms})
        return {"id": int(cur.lastrowid), "version_id": version_id, "cue_id": cue_id, "user": actor, "time_ms": time_ms, "body": body, "status": "open"}

    # ------------------------------------------------------------ 提交/复核
    def submit(self, version_id: int, actor: str, role: str = "viewer") -> dict[str, Any]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            version = self._version(conn, version_id)
            if version["status"] != "draft" or not self._can_edit(conn, version, actor):
                raise DomainError("只有草稿版本的翻译或时间轴人员可以提交复核", 409)
            if not conn.execute("SELECT 1 FROM cues WHERE version_id=?", (version_id,)).fetchone():
                raise DomainError("空版本不能提交复核", 409)
            conn.execute("UPDATE versions SET status='review',updated_at=? WHERE id=?", (utcnow(), version_id))
            self._audit(conn, actor, "version.submitted", "version", version_id, {})
            return dict(conn.execute("SELECT * FROM versions WHERE id=?", (version_id,)).fetchone())

    def batch_approve_cues(
        self, version_id: int, actor: str, payload: dict[str, Any] | None = None, role: str = "viewer",
    ) -> dict[str, Any]:
        """复核人批量逐句签署（可中断恢复）。

        - 只有复核中的版本、分配的复核人（非创建人）可以签；
        - 每条签署记下确认当时的文本+时间轴摘要；当前内容已变的句子拒绝签署；
        - batch_id 由调用方持有：中断后原样重提，已处理的句子不会生成两份签署。
        """
        payload = payload or {}
        cue_indexes = payload.get("cue_indexes")
        if cue_indexes is not None and (not isinstance(cue_indexes, list) or not cue_indexes):
            raise DomainError("请至少选择一句要签署的字幕")
        if isinstance(cue_indexes, list):
            try:
                wanted = sorted({int(i) for i in cue_indexes})
            except (TypeError, ValueError) as exc:
                raise DomainError("句号必须是整数列表") from exc
        else:
            wanted = []
        batch_id = str(payload.get("batch_id") or "").strip() or f"batch-{uuid.uuid4().hex}"
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            version = self._version(conn, version_id)
            if version["status"] != "review":
                raise DomainError("版本当前不在复核阶段，不能签署", 409)
            if not self._can_review(conn, version, actor):
                raise DomainError("没有该版本的复核权限", 403)
            if actor == version["created_by"]:
                raise DomainError("创建人不能复核自己的版本", 403)
            if not wanted:
                wanted = [int(r["cue_index"]) for r in conn.execute(
                    "SELECT cue_index FROM cues WHERE version_id=? ORDER BY cue_index", (version_id,)
                )]
                if not wanted:
                    raise DomainError("空版本没有可签署的字幕")
            now = utcnow()
            conn.execute(
                "INSERT OR IGNORE INTO approval_batches(id,version_id,reviewer,status,created_at) VALUES(?,?,?, 'completed',?)",
                (batch_id, version_id, actor, now),
            )
            # 批次号必须属于同一版本、同一复核人，防止中断恢复时串号。
            owner = conn.execute(
                "SELECT version_id,reviewer FROM approval_batches WHERE id=?", (batch_id,)
            ).fetchone()
            if owner and (int(owner["version_id"]) != version_id or owner["reviewer"] != actor):
                raise DomainError("批次号已被其他版本或复核人使用，请更换后重试", 409)
            signed: list[int] = []
            already: list[int] = []
            missing: list[int] = []
            for index in wanted:
                cue = conn.execute(
                    "SELECT * FROM cues WHERE version_id=? AND cue_index=?", (version_id, index)
                ).fetchone()
                if not cue:
                    missing.append(index)
                    continue
                # 该批次处理过这句（任意摘要）：中断重放时跳过，绝不产生两份签署。
                if conn.execute(
                    "SELECT 1 FROM cue_approvals WHERE batch_id=? AND cue_id=?",
                    (batch_id, cue["id"]),
                ).fetchone():
                    already.append(index)
                    continue
                digest = cue_digest(cue["cue_index"], cue["start_ms"], cue["end_ms"], cue["text"])
                # 只认“当前摘要”的有效签署；旧行一旦失效，即使改回原文也必须重新签署。
                if conn.execute(
                    "SELECT 1 FROM cue_approvals WHERE version_id=? AND cue_id=? AND content_digest=? AND reviewer=? AND status='valid'",
                    (version_id, cue["id"], digest, actor),
                ).fetchone():
                    already.append(index)
                    continue
                conn.execute(
                    """INSERT INTO cue_approvals(version_id,cue_id,batch_id,cue_index,reviewer,
                           content_digest,status,source,signed_at)
                       VALUES(?,?,?,?,?,?, 'valid','batch',?)""",
                    (version_id, cue["id"], batch_id, index, actor, digest, now),
                )
                signed.append(index)
            self._audit(conn, actor, "cues.batch_approved", "version", version_id, {
                "batch_id": batch_id, "signed": signed, "already": already, "missing": missing,
            })
        return {
            "version_id": version_id, "batch_id": batch_id, "reviewer": actor,
            "signed": signed, "already_signed": already, "missing_indexes": missing,
        }

    def review(self, version_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        """整版复核决定。批准时在同一事务内逐句核对当前摘要。"""
        decision = str(payload.get("decision", "")).strip()
        if decision not in {"approve", "reject"}:
            raise DomainError("复核决定必须是 approve 或 reject")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            version = self._version(conn, version_id)
            if version["status"] != "review":
                raise DomainError("版本当前不在复核阶段", 409)
            if not self._can_review(conn, version, actor):
                raise DomainError("没有该版本的复核权限", 403)
            if actor == version["created_by"]:
                raise DomainError("创建人不能复核自己的版本", 403)
            comment = str(payload.get("comment", ""))
            conn.execute(
                "INSERT INTO reviews(version_id,reviewer,decision,comment,created_at) VALUES(?,?,?,?,?)",
                (version_id, actor, decision, comment, utcnow()),
            )
            now = utcnow()
            if decision == "approve":
                # 与签署/保存共用 IMMEDIATE 事务：此刻看到的摘要就是将被批准的摘要。
                missing = self._missing_indexes(conn, version_id)
                if missing:
                    raise DomainError(
                        f"以下句号缺少当前内容的有效签署，不能整版通过：{format_cue_indexes(missing)}", 409,
                    )
                conn.execute("UPDATE versions SET status='approved',updated_at=? WHERE id=?", (now, version_id))
            else:
                conn.execute("UPDATE versions SET status='draft',updated_at=? WHERE id=?", (now, version_id))
            self._audit(conn, actor, f"version.{decision}", "version", version_id, {"comment": comment})
            return dict(conn.execute("SELECT * FROM versions WHERE id=?", (version_id,)).fetchone())

    def lock(self, version_id: int, actor: str, role: str = "viewer") -> dict[str, Any]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            version = self._version(conn, version_id)
            if version["status"] != "approved":
                raise DomainError("只有已批准版本可以锁定", 409)
            if actor != version["owner"] and role != "admin":
                raise DomainError("只有项目负责人可以锁定版本", 403)
            # 锁定是批准之后的屏障：批准后若有任何写入（正常状态下不允许），
            # 逐句复核仍然能挡住签署与当前内容不一致的情况。
            missing = self._missing_indexes(conn, version_id)
            if missing:
                raise DomainError(
                    f"以下句号缺少当前内容的有效签署，不能锁定：{format_cue_indexes(missing)}", 409,
                )
            conn.execute("UPDATE versions SET status='locked',updated_at=? WHERE id=?", (utcnow(), version_id))
            self._audit(conn, actor, "version.locked", "version", version_id, {})
            return dict(conn.execute("SELECT * FROM versions WHERE id=?", (version_id,)).fetchone())

    def deliver(self, version_id: int, actor: str, role: str = "viewer") -> dict[str, Any]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            version = self._version(conn, version_id)
            if actor != version["owner"] and role != "admin":
                raise DomainError("只有项目负责人可以交付", 403)
            if version["status"] not in {"approved", "locked"}:
                raise DomainError("只有批准或锁定版本可以交付", 409)
            if conn.execute("SELECT 1 FROM deliveries WHERE version_id=?", (version_id,)).fetchone():
                raise DomainError("该版本已经交付，不能用新内容覆盖", 409)
            # 交付前逐句核对当前摘要；缺少有效签署时列出句号并停住，不生成任何交付记录。
            missing = self._missing_indexes(conn, version_id)
            if missing:
                self._audit(conn, actor, "delivery.blocked", "version", version_id, {"missing": missing})
                raise DomainError(
                    f"交付已停止：以下句号缺少当前内容的有效签署：{format_cue_indexes(missing)}", 409,
                )
            cues = [dict(r) for r in conn.execute(
                "SELECT cue_index,start_ms,end_ms,text FROM cues WHERE version_id=? ORDER BY cue_index",
                (version_id,),
            )]
            glossary = [dict(r) for r in conn.execute(
                "SELECT source_term,required_translation,forbidden_terms FROM glossaries WHERE project_id=? ORDER BY source_term",
                (version["project_id"],),
            )]
            approvals = []
            for cue in cues:
                digest = cue_digest(cue["cue_index"], cue["start_ms"], cue["end_ms"], cue["text"])
                rows = conn.execute(
                    "SELECT reviewer,signed_at FROM cue_approvals WHERE version_id=? AND cue_index=? AND status='valid' AND content_digest=? ORDER BY signed_at,id",
                    (version_id, cue["cue_index"], digest),
                ).fetchall()
                approvals.append({
                    "cue_index": cue["cue_index"], "content_digest": digest,
                    "signatures": [{"reviewer": r["reviewer"], "signed_at": r["signed_at"]} for r in rows],
                })
            manifest = {
                "project_id": version["project_id"], "version_id": version_id,
                "language": version["language"], "version_no": version["version_no"],
                "cues": cues, "glossary": glossary, "cue_approvals": approvals,
            }
            snapshot_hash = hashlib.sha256(
                json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            previous = conn.execute(
                "SELECT id FROM deliveries WHERE version_id IN (SELECT id FROM versions WHERE project_id=? AND language=? AND id<>?) ORDER BY id DESC LIMIT 1",
                (version["project_id"], version["language"], version_id),
            ).fetchone()
            if previous:
                conn.execute(
                    "UPDATE versions SET status='superseded',updated_at=? WHERE id=(SELECT version_id FROM deliveries WHERE id=?)",
                    (utcnow(), previous["id"]),
                )
            cur = conn.execute(
                "INSERT INTO deliveries(version_id,supersedes_version_id,snapshot_hash,manifest,delivered_by,created_at) VALUES(?,?,?,?,?,?)",
                (version_id, previous["id"] if previous else None, snapshot_hash,
                 json.dumps(manifest, ensure_ascii=False, sort_keys=True), actor, utcnow()),
            )
            conn.execute("UPDATE versions SET status='delivered',updated_at=? WHERE id=?", (utcnow(), version_id))
            self._audit(conn, actor, "version.delivered", "version", version_id, {"snapshot_hash": snapshot_hash})
            row = conn.execute("SELECT * FROM deliveries WHERE id=?", (cur.lastrowid,)).fetchone()
        return dict(row)

    # ------------------------------------------------------------------ 查询
    def list_projects(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM projects ORDER BY id").fetchall()]

    def list_versions(self, project_id: int | None = None) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if project_id:
                rows = conn.execute("SELECT * FROM versions WHERE project_id=? ORDER BY id", (project_id,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM versions ORDER BY id").fetchall()
            return [dict(r) for r in rows]

    def list_cues(self, version_id: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM cues WHERE version_id=? ORDER BY cue_index", (version_id,)
            ).fetchall()]

    def list_comments(self, version_id: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM comments WHERE version_id=? ORDER BY id", (version_id,)
            ).fetchall()]

    def list_approvals(self, version_id: int) -> list[dict[str, Any]]:
        """句级签署视图：按句号返回每句当前摘要、有效签署和失效原因，重开页面即可看清。"""
        with self.connect() as conn:
            self._version(conn, version_id)
            result: list[dict[str, Any]] = []
            for cue in self._current_cues(conn, version_id):
                digest = cue_digest(cue["cue_index"], cue["start_ms"], cue["end_ms"], cue["text"])
                valid_rows = conn.execute(
                    "SELECT reviewer,signed_at FROM cue_approvals WHERE version_id=? AND cue_id=? AND status='valid' AND content_digest=? ORDER BY signed_at,id",
                    (version_id, cue["id"], digest),
                ).fetchall()
                bad_rows = conn.execute(
                    """SELECT reviewer,signed_at,invalidated_reason,invalidated_at
                       FROM cue_approvals WHERE version_id=? AND cue_id=? AND status='invalidated'
                       ORDER BY id""",
                    (version_id, cue["id"]),
                ).fetchall()
                result.append({
                    "cue_id": cue["id"], "cue_index": cue["cue_index"],
                    "start_ms": cue["start_ms"], "end_ms": cue["end_ms"], "text": cue["text"],
                    "current_digest": digest,
                    "valid_signatures": [
                        {"reviewer": r["reviewer"], "signed_at": r["signed_at"]} for r in valid_rows
                    ],
                    "invalidated": [{
                        "reviewer": r["reviewer"], "signed_at": r["signed_at"],
                        "reason": r["invalidated_reason"], "invalidated_at": r["invalidated_at"],
                    } for r in bad_rows],
                    "has_valid_signature": bool(valid_rows),
                })
            return result

    def delivery_readiness(self, version_id: int) -> dict[str, Any]:
        with self.connect() as conn:
            version = self._version(conn, version_id)
            cues = self._current_cues(conn, version_id)
            missing = self._missing_indexes(conn, version_id) if cues else []
            return {
                "version_id": version_id, "status": version["status"],
                "total_cues": len(cues), "missing_indexes": missing,
                "ready": bool(cues) and not missing,
            }

    def list_deliveries(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM deliveries ORDER BY id DESC").fetchall()]

    def audit(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM audit_log ORDER BY id DESC").fetchall()]


def seed_demo(db: Database) -> dict[str, int]:
    if db.list_projects():
        return {"project": int(db.list_projects()[0]["id"])}
    project = db.create_project("alice", {"name": "极地纪录片字幕", "source_language": "en", "media_name": "polar.mp4", "media_sha256": "b" * 64, "duration_ms": 120000}, "owner")
    db.set_glossary(project["id"], "alice", {"source_term": "seal", "required_translation": "海豹", "forbidden_terms": ["密封"], "notes": "动物学语境"}, "owner")
    version = db.create_version(project["id"], "alice", {"language": "zh-CN"}, "owner")
    return {"project": int(project["id"]), "version": int(version["id"])}
