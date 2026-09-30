"""Subtitle localization quality-control and delivery service."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "subtitle_qc.db"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Signatures bind to this summary of a cue. Any post-confirmation edit to the
# text or the timeline changes the digest, so only the touched cue is
# invalidated instead of the whole version being implicitly re-signed.
def cue_digests(cue_index: int, start_ms: int, end_ms: int, text: str) -> dict[str, str]:
    text_digest = hashlib.sha256(f"text:{text}".encode("utf-8")).hexdigest()
    timeline_digest = hashlib.sha256(
        f"timeline:{cue_index}:{start_ms}:{end_ms}".encode("utf-8")
    ).hexdigest()
    content_digest = hashlib.sha256(
        f"{text_digest}|{timeline_digest}".encode("utf-8")
    ).hexdigest()
    return {"text": text_digest, "timeline": timeline_digest, "content": content_digest}


def invalidation_reason(signature: sqlite3.Row, cue_index: int, start_ms: int,
                        end_ms: int, text: str) -> str:
    digests = cue_digests(cue_index, start_ms, end_ms, text)
    changes: list[str] = []
    if signature["text_digest"] != digests["text"]:
        changes.append("文本在签署后被修改")
    if signature["signed_cue_index"] != cue_index:
        changes.append("句号在签署后被调整")
    if signature["signed_start_ms"] != start_ms or signature["signed_end_ms"] != end_ms:
        changes.append("时间轴在签署后被调整")
    return "；".join(changes) or "字幕在签署后被修改"


class DomainError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class Database:
    def __init__(self, path: str | os.PathLike[str] = DEFAULT_DB):
        self.path = str(path)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
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
                CREATE TABLE IF NOT EXISTS cue_signatures (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
                    cue_id INTEGER NOT NULL REFERENCES cues(id) ON DELETE CASCADE,
                    reviewer TEXT NOT NULL,
                    signed_cue_index INTEGER NOT NULL,
                    signed_start_ms INTEGER NOT NULL,
                    signed_end_ms INTEGER NOT NULL,
                    signed_text TEXT NOT NULL,
                    text_digest TEXT NOT NULL,
                    timeline_digest TEXT NOT NULL,
                    content_digest TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'valid'
                        CHECK(status IN ('valid','invalid')),
                    invalid_reason TEXT NOT NULL DEFAULT '',
                    signed_at TEXT NOT NULL,
                    invalidated_at TEXT,
                    UNIQUE(version_id,cue_id)
                );
                CREATE TABLE IF NOT EXISTS sign_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
                    batch_key TEXT NOT NULL,
                    reviewer TEXT NOT NULL,
                    requested TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'completed'
                        CHECK(status IN ('completed')),
                    created_at TEXT NOT NULL,
                    UNIQUE(version_id,batch_key)
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
                """
            )
            self._upgrade(conn)

    # Legacy databases only held a version-wide approval (reviews row +
    # approved/locked status). Promote those approvals into per-cue
    # signatures built from current content; for already delivered versions
    # rebuild the signed summary from the delivery snapshot instead, so the
    # signatures describe exactly what left the door.
    def _upgrade(self, conn: sqlite3.Connection) -> None:
        conn.execute("PRAGMA user_version=2")
        approved_statuses = ("approved", "locked", "delivered", "superseded")
        versions = conn.execute(
            f"SELECT id,status FROM versions WHERE status IN ({','.join('?' for _ in approved_statuses)})",
            approved_statuses,
        ).fetchall()
        now = utcnow()
        for version in versions:
            version_id = int(version["id"])
            if conn.execute(
                "SELECT 1 FROM cue_signatures WHERE version_id=? LIMIT 1", (version_id,)
            ).fetchone():
                continue
            review = conn.execute(
                "SELECT reviewer,created_at FROM reviews WHERE version_id=? AND decision='approve' ORDER BY id DESC LIMIT 1",
                (version_id,),
            ).fetchone()
            reviewer = review["reviewer"] if review else "legacy-reviewer"
            signed_at = review["created_at"] if review else now
            rows: list[tuple[Any, ...]] = []
            delivery = conn.execute(
                "SELECT manifest,created_at FROM deliveries WHERE version_id=?", (version_id,)
            ).fetchone()
            if delivery:
                manifest = json.loads(delivery["manifest"])
                for cue in manifest.get("cues", []):
                    # The manifest stores content but no cue id; reconnect it to
                    # the live cue by cue index. Legacy versions could not be
                    # edited after approval, so the index still points to it.
                    live = conn.execute(
                        "SELECT id FROM cues WHERE version_id=? AND cue_index=?",
                        (version_id, int(cue["cue_index"])),
                    ).fetchone()
                    if not live:
                        continue
                    digests = cue_digests(int(cue["cue_index"]), int(cue["start_ms"]),
                                          int(cue["end_ms"]), cue["text"])
                    rows.append((
                        version_id, int(live["id"]), reviewer,
                        int(cue["cue_index"]), int(cue["start_ms"]), int(cue["end_ms"]),
                        cue["text"], digests["text"], digests["timeline"], digests["content"],
                        signed_at,
                    ))
            else:
                for cue in conn.execute("SELECT * FROM cues WHERE version_id=?", (version_id,)).fetchall():
                    digests = cue_digests(int(cue["cue_index"]), int(cue["start_ms"]),
                                          int(cue["end_ms"]), cue["text"])
                    rows.append((
                        version_id, int(cue["id"]), reviewer,
                        int(cue["cue_index"]), int(cue["start_ms"]), int(cue["end_ms"]),
                        cue["text"], digests["text"], digests["timeline"], digests["content"],
                        signed_at,
                    ))
            for row in rows:
                conn.execute(
                    """INSERT OR IGNORE INTO cue_signatures(
                        version_id,cue_id,reviewer,signed_cue_index,signed_start_ms,signed_end_ms,
                        signed_text,text_digest,timeline_digest,content_digest,
                        status,invalid_reason,signed_at,invalidated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,'valid','',?,NULL)""",
                    row,
                )
            if rows:
                self._audit(conn, reviewer, "signatures.backfilled", "version", version_id,
                            {"source": "delivery_snapshot" if delivery else "current_content",
                             "count": len(rows)})

    def _audit(self, conn: sqlite3.Connection, actor: str, action: str, entity_type: str,
               entity_id: int | None, details: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO audit_log(actor,action,entity_type,entity_id,details,created_at) VALUES(?,?,?,?,?,?)",
            (actor, action, entity_type, entity_id, json.dumps(details, ensure_ascii=False), utcnow()),
        )

    def create_project(self, actor: str, payload: dict[str, Any], role: str = "owner") -> dict[str, Any]:
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
            next_no = int(conn.execute("SELECT COALESCE(MAX(version_no),0)+1 value FROM versions WHERE project_id=? AND language=?", (project_id, language)).fetchone()["value"])
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
            version = conn.execute("SELECT v.*,p.owner FROM versions v JOIN projects p ON p.id=v.project_id WHERE v.id=?", (version_id,)).fetchone()
            if not version:
                raise DomainError("版本不存在", 404)
            if actor != version["owner"] and role != "admin":
                raise DomainError("只有项目负责人可以分配人员", 403)
            conn.execute("INSERT OR IGNORE INTO assignments(version_id,user,role,assigned_by,created_at) VALUES(?,?,?,?,?)", (version_id, user, assignment_role, actor, utcnow()))
            self._audit(conn, actor, "assignment.saved", "version", version_id, {"user": user, "role": assignment_role})
        return {"version_id": version_id, "user": user, "role": assignment_role}

    def _version(self, conn: sqlite3.Connection, version_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT v.*,p.owner,p.duration_ms FROM versions v JOIN projects p ON p.id=v.project_id WHERE v.id=?", (version_id,)).fetchone()
        if not row:
            raise DomainError("字幕版本不存在", 404)
        return row

    def _can_edit(self, conn: sqlite3.Connection, version: sqlite3.Row, actor: str) -> bool:
        if actor == version["owner"]:
            return True
        return bool(conn.execute("SELECT 1 FROM assignments WHERE version_id=? AND user=? AND role IN ('translator','timeline')", (version["id"], actor)).fetchone())

    def _validate_glossary(self, conn: sqlite3.Connection, project_id: int, text: str) -> None:
        for row in conn.execute("SELECT * FROM glossaries WHERE project_id=?", (project_id,)):
            forbidden = json.loads(row["forbidden_terms"])
            for term in forbidden:
                if term and term in text:
                    raise DomainError(f"字幕包含禁用译法: {term}")
            # The glossary is enforced only when the corresponding source term
            # appears in the localized cue. This keeps it useful without making
            # every cue repeat every glossary word.
            if row["source_term"] in text and row["required_translation"] not in text:
                raise DomainError(f"术语 {row['source_term']} 必须使用指定译法 {row['required_translation']}")

    def _invalidate_signature(self, conn: sqlite3.Connection, cue_id: int,
                              cue_index: int, start_ms: int, end_ms: int, text: str) -> bool:
        signature = conn.execute(
            "SELECT * FROM cue_signatures WHERE cue_id=? AND status='valid'", (cue_id,)
        ).fetchone()
        if not signature:
            return False
        digests = cue_digests(cue_index, start_ms, end_ms, text)
        if signature["content_digest"] == digests["content"]:
            return False
        reason = invalidation_reason(signature, cue_index, start_ms, end_ms, text)
        conn.execute(
            "UPDATE cue_signatures SET status='invalid',invalid_reason=?,invalidated_at=? WHERE id=?",
            (reason, utcnow(), signature["id"]),
        )
        return True

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
            try:
                cue_index = int(payload.get("cue_index"))
                start_ms = int(payload.get("start_ms"))
                end_ms = int(payload.get("end_ms"))
            except (TypeError, ValueError) as exc:
                raise DomainError("字幕序号和时间必须是整数") from exc
            text = str(payload.get("text", "")).strip()
            if cue_index < 0 or start_ms < 0 or end_ms <= start_ms or end_ms > int(version["duration_ms"]) or not text:
                raise DomainError("字幕时间、序号或内容不合法")
            self._validate_glossary(conn, int(version["project_id"]), text)
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
            index_owner = conn.execute("SELECT * FROM cues WHERE version_id=? AND cue_index=? AND id<>?", (version_id, cue_index, int(cue_id or -1))).fetchone()
            if index_owner:
                raise DomainError("字幕序号已被使用", 409)
            invalidated = False
            invalid_reason = ""
            if existing:
                conn.execute("UPDATE cues SET cue_index=?,start_ms=?,end_ms=?,text=?,updated_by=?,updated_at=? WHERE id=?", (cue_index, start_ms, end_ms, text, actor, utcnow(), existing["id"]))
                saved_id = existing["id"]
                if self._invalidate_signature(conn, saved_id, cue_index, start_ms, end_ms, text):
                    invalidated = True
                    invalid_reason = conn.execute(
                        "SELECT invalid_reason FROM cue_signatures WHERE cue_id=?", (saved_id,)
                    ).fetchone()["invalid_reason"]
            else:
                cur = conn.execute("INSERT INTO cues(version_id,cue_index,start_ms,end_ms,text,updated_by,updated_at) VALUES(?,?,?,?,?,?,?)", (version_id, cue_index, start_ms, end_ms, text, actor, utcnow()))
                saved_id = cur.lastrowid
            revision = int(version["revision"]) + 1
            conn.execute("UPDATE versions SET revision=?,updated_at=? WHERE id=?", (revision, utcnow(), version_id))
            self._audit(conn, actor, "cue.saved", "version", version_id,
                        {"cue_id": saved_id, "revision": revision,
                         "signature_invalidated": invalidated, "invalid_reason": invalid_reason})
        return dict(conn.execute("SELECT * FROM cues WHERE id=?", (saved_id,)).fetchone()) | {
            "version_revision": revision,
            "signature_status": "invalid" if invalidated else "none",
            "invalid_reason": invalid_reason,
        }

    def add_comment(self, version_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        body = str(payload.get("body", "")).strip()
        try:
            time_ms = int(payload.get("time_ms"))
        except (TypeError, ValueError) as exc:
            raise DomainError("评论时间必须是毫秒整数") from exc
        with self.connect() as conn:
            version = self._version(conn, version_id)
            allowed = actor == version["owner"] or conn.execute("SELECT 1 FROM assignments WHERE version_id=? AND user=?", (version_id, actor)).fetchone()
            if not allowed:
                raise DomainError("只有项目成员可以评论", 403)
            if not body or time_ms < 0 or time_ms > int(version["duration_ms"]):
                raise DomainError("评论内容或时间点不合法")
            cue_id = payload.get("cue_id")
            if cue_id is not None and not conn.execute("SELECT 1 FROM cues WHERE id=? AND version_id=?", (int(cue_id), version_id)).fetchone():
                raise DomainError("评论关联的字幕不存在", 404)
            cur = conn.execute("INSERT INTO comments(version_id,cue_id,user,time_ms,body,created_at) VALUES(?,?,?,?,?,?)", (version_id, cue_id, actor, time_ms, body, utcnow()))
            self._audit(conn, actor, "comment.added", "version", version_id, {"comment_id": cur.lastrowid, "time_ms": time_ms})
        return {"id": int(cur.lastrowid), "version_id": version_id, "cue_id": cue_id, "user": actor, "time_ms": time_ms, "body": body, "status": "open"}

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

    def _require_reviewer(self, conn: sqlite3.Connection, version: sqlite3.Row, actor: str) -> None:
        assigned = conn.execute(
            "SELECT 1 FROM assignments WHERE version_id=? AND user=? AND role='reviewer'",
            (version["id"], actor),
        ).fetchone()
        if not assigned and actor != version["owner"]:
            raise DomainError("没有该版本的复核权限", 403)
        if actor == version["created_by"]:
            raise DomainError("创建人不能复核自己的版本", 403)

    def _missing_valid_signatures(self, conn: sqlite3.Connection, version_id: int) -> list[dict[str, int]]:
        cues = conn.execute(
            "SELECT id,cue_index FROM cues WHERE version_id=? ORDER BY cue_index", (version_id,)
        ).fetchall()
        missing: list[dict[str, int]] = []
        for cue in cues:
            signature = conn.execute(
                "SELECT content_digest FROM cue_signatures WHERE version_id=? AND cue_id=? AND status='valid'",
                (version_id, cue["id"]),
            ).fetchone()
            current = conn.execute(
                "SELECT cue_index,start_ms,end_ms,text FROM cues WHERE id=?", (cue["id"],)
            ).fetchone()
            digests = cue_digests(int(current["cue_index"]), int(current["start_ms"]),
                                  int(current["end_ms"]), current["text"])
            if not signature or signature["content_digest"] != digests["content"]:
                missing.append({"cue_id": int(cue["id"]), "cue_index": int(cue["cue_index"])})
        return missing

    def sign_cues(self, version_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            version = self._version(conn, version_id)
            if version["status"] != "review":
                raise DomainError("只有复核中的版本可以逐句签署", 409)
            self._require_reviewer(conn, version, actor)

            cue_ids = payload.get("cue_ids")
            all_cues = bool(payload.get("all"))
            if not all_cues:
                if not isinstance(cue_ids, list) or not cue_ids:
                    raise DomainError("请指定要签署的字幕 cue_ids，或提交 all=true")
                try:
                    target_ids = [int(cid) for cid in cue_ids]
                except (TypeError, ValueError) as exc:
                    raise DomainError("字幕 ID 必须是整数") from exc
                if len(set(target_ids)) != len(target_ids):
                    raise DomainError("同一次确认不能重复包含同一句字幕")
            else:
                target_ids = [
                    int(r["id"]) for r in conn.execute(
                        "SELECT id FROM cues WHERE version_id=? ORDER BY cue_index", (version_id,)
                    ).fetchall()
                ]
                if not target_ids:
                    raise DomainError("空版本没有可签署的字幕", 409)

            batch_key = payload.get("batch_key")
            if batch_key is not None:
                batch_key = str(batch_key).strip() or None
            if batch_key:
                # Resumable batch: a stable key means a crashed client can
                # re-post the same list. Re-running the upsert never yields a
                # second signature because (version_id,cue_id) is unique.
                conn.execute(
                    "INSERT OR IGNORE INTO sign_batches(version_id,batch_key,reviewer,requested,status,created_at) VALUES(?,?,?,?, 'completed', ?)",
                    (version_id, batch_key, actor,
                     json.dumps(target_ids, ensure_ascii=False), utcnow()),
                )

            now = utcnow()
            signed: list[int] = []
            already: list[int] = []
            for cue_id in target_ids:
                cue = conn.execute(
                    "SELECT * FROM cues WHERE id=? AND version_id=?", (cue_id, version_id)
                ).fetchone()
                if not cue:
                    raise DomainError(f"字幕 {cue_id} 不属于该版本", 404)
                digests = cue_digests(int(cue["cue_index"]), int(cue["start_ms"]),
                                      int(cue["end_ms"]), cue["text"])
                existing = conn.execute(
                    "SELECT * FROM cue_signatures WHERE version_id=? AND cue_id=?",
                    (version_id, cue_id),
                ).fetchone()
                if existing:
                    if existing["status"] == "valid" and existing["content_digest"] == digests["content"]:
                        already.append(int(cue["cue_index"]))
                        continue
                    conn.execute(
                        """UPDATE cue_signatures SET reviewer=?,signed_cue_index=?,signed_start_ms=?,
                           signed_end_ms=?,signed_text=?,text_digest=?,timeline_digest=?,content_digest=?,
                           status='valid',invalid_reason='',signed_at=?,invalidated_at=NULL WHERE id=?""",
                        (actor, cue["cue_index"], cue["start_ms"], cue["end_ms"], cue["text"],
                         digests["text"], digests["timeline"], digests["content"], now, existing["id"]),
                    )
                else:
                    conn.execute(
                        """INSERT INTO cue_signatures(version_id,cue_id,reviewer,signed_cue_index,
                           signed_start_ms,signed_end_ms,signed_text,text_digest,timeline_digest,
                           content_digest,status,invalid_reason,signed_at,invalidated_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?, 'valid', '', ?, NULL)""",
                        (version_id, cue_id, actor, cue["cue_index"], cue["start_ms"], cue["end_ms"],
                         cue["text"], digests["text"], digests["timeline"], digests["content"], now),
                    )
                signed.append(int(cue["cue_index"]))
            self._audit(conn, actor, "cues.signed", "version", version_id,
                        {"signed": signed, "already_signed": already, "batch_key": batch_key})
            missing = self._missing_valid_signatures(conn, version_id)
        return {
            "version_id": version_id,
            "signed_cue_indexes": signed,
            "already_signed_cue_indexes": already,
            "remaining_cue_indexes": [m["cue_index"] for m in missing],
            "batch_key": batch_key,
        }

    def review(self, version_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        decision = str(payload.get("decision", "")).strip()
        if decision not in {"approve", "reject"}:
            raise DomainError("复核决定必须是 approve 或 reject")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            version = self._version(conn, version_id)
            if version["status"] != "review":
                raise DomainError("版本当前不在复核阶段", 409)
            self._require_reviewer(conn, version, actor)
            if decision == "approve":
                # Gate the version-wide approval on current per-cue summaries.
                # Because save and approve both take an immediate write lock,
                # a translator saving in parallel is serialized with this
                # check: an edit either lands first and its cue shows up here,
                # or waits until approval commits. A signature can never cover
                # stale content that still reaches delivery.
                missing = self._missing_valid_signatures(conn, version_id)
                if missing:
                    raise DomainError(
                        "以下句号缺少与当前内容一致的有效签署，无法整版通过: "
                        + ", ".join(str(m["cue_index"]) for m in missing), 409
                    )
            conn.execute("INSERT INTO reviews(version_id,reviewer,decision,comment,created_at) VALUES(?,?,?,?,?)", (version_id, actor, decision, str(payload.get("comment", "")), utcnow()))
            status = "approved" if decision == "approve" else "draft"
            conn.execute("UPDATE versions SET status=?,updated_at=? WHERE id=?", (status, utcnow(), version_id))
            self._audit(conn, actor, f"version.{decision}", "version", version_id, {"comment": payload.get("comment", "")})
        return dict(conn.execute("SELECT * FROM versions WHERE id=?", (version_id,)).fetchone())

    def lock(self, version_id: int, actor: str, role: str = "viewer") -> dict[str, Any]:
        with self.connect() as conn:
            version = self._version(conn, version_id)
            if version["status"] != "approved":
                raise DomainError("只有已批准版本可以锁定", 409)
            if actor != version["owner"] and role != "admin":
                raise DomainError("只有项目负责人可以锁定版本", 403)
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
            # Per-cue delivery gate: recompute the current summary for every
            # cue and demand a valid signature bound to that exact summary.
            missing: list[int] = []
            cue_rows = conn.execute(
                "SELECT * FROM cues WHERE version_id=? ORDER BY cue_index", (version_id,)
            ).fetchall()
            for cue in cue_rows:
                digests = cue_digests(int(cue["cue_index"]), int(cue["start_ms"]),
                                      int(cue["end_ms"]), cue["text"])
                signature = conn.execute(
                    "SELECT content_digest FROM cue_signatures WHERE version_id=? AND cue_id=? AND status='valid'",
                    (version_id, cue["id"]),
                ).fetchone()
                if not signature or signature["content_digest"] != digests["content"]:
                    missing.append(int(cue["cue_index"]))
            if missing:
                raise DomainError(
                    "以下句号缺少与当前内容一致的有效签署，已停止交付: "
                    + ", ".join(str(i) for i in missing), 409
                )
            cues: list[dict[str, Any]] = []
            for cue in cue_rows:
                digests = cue_digests(int(cue["cue_index"]), int(cue["start_ms"]),
                                      int(cue["end_ms"]), cue["text"])
                cues.append({
                    "cue_id": cue["id"], "cue_index": cue["cue_index"],
                    "start_ms": cue["start_ms"], "end_ms": cue["end_ms"], "text": cue["text"],
                    "content_digest": digests["content"],
                    "text_digest": digests["text"], "timeline_digest": digests["timeline"],
                })
            glossary = [dict(r) for r in conn.execute("SELECT source_term,required_translation,forbidden_terms FROM glossaries WHERE project_id=? ORDER BY source_term", (version["project_id"],))]
            manifest = {"project_id": version["project_id"], "version_id": version_id, "language": version["language"], "version_no": version["version_no"], "cues": cues, "glossary": glossary}
            snapshot_hash = hashlib.sha256(json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            previous = conn.execute("SELECT id FROM deliveries WHERE version_id IN (SELECT id FROM versions WHERE project_id=? AND language=? AND id<>?) ORDER BY id DESC LIMIT 1", (version["project_id"], version["language"], version_id)).fetchone()
            if previous:
                conn.execute("UPDATE versions SET status='superseded',updated_at=? WHERE id=(SELECT version_id FROM deliveries WHERE id=?)", (utcnow(), previous["id"]))
            cur = conn.execute(
                "INSERT INTO deliveries(version_id,supersedes_version_id,snapshot_hash,manifest,delivered_by,created_at) VALUES(?,?,?,?,?,?)",
                (version_id, previous["id"] if previous else None, snapshot_hash, json.dumps(manifest, ensure_ascii=False, sort_keys=True), actor, utcnow()),
            )
            conn.execute("UPDATE versions SET status='delivered',updated_at=? WHERE id=?", (utcnow(), version_id))
            self._audit(conn, actor, "version.delivered", "version", version_id, {"snapshot_hash": snapshot_hash})
        return dict(conn.execute("SELECT * FROM deliveries WHERE id=?", (cur.lastrowid,)).fetchone())

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
            return [dict(r) for r in conn.execute("SELECT * FROM cues WHERE version_id=? ORDER BY cue_index", (version_id,)).fetchall()]

    def list_signatures(self, version_id: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT s.id,s.version_id,s.cue_id,s.reviewer,s.status,s.invalid_reason,
                       s.signed_at,s.invalidated_at,s.signed_cue_index,s.signed_start_ms,
                       s.signed_end_ms,s.signed_text,s.text_digest,s.timeline_digest,s.content_digest,
                       c.cue_index AS current_cue_index,c.start_ms AS current_start_ms,
                       c.end_ms AS current_end_ms,c.text AS current_text
                FROM cue_signatures s
                JOIN cues c ON c.id=s.cue_id
                WHERE s.version_id=?
                ORDER BY c.cue_index
                """,
                (version_id,),
            ).fetchall()
            result: list[dict[str, Any]] = []
            for r in rows:
                current_digests = cue_digests(int(r["current_cue_index"]), int(r["current_start_ms"]),
                                              int(r["current_end_ms"]), r["current_text"])
                matches = r["status"] == "valid" and r["content_digest"] == current_digests["content"]
                item = dict(r)
                item.pop("current_text", None)
                item["matches_current"] = matches
                if r["status"] == "valid" and not matches:
                    # Defensive: a valid row that no longer matches gets a
                    # readable reason so the page can always explain itself.
                    item["invalid_reason"] = invalidation_reason(
                        r, int(r["current_cue_index"]), int(r["current_start_ms"]),
                        int(r["current_end_ms"]), r["current_text"]
                    )
                result.append(item)
            return result

    def list_comments(self, version_id: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM comments WHERE version_id=? ORDER BY id", (version_id,)).fetchall()]

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


class Handler(BaseHTTPRequestHandler):
    db: Database
    server_version = "SubtitleQC/1.0"

    def _send(self, payload: Any, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _html(self) -> None:
        data = (ROOT / "static" / "index.html").read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            raise DomainError("请求体不是合法 JSON") from exc

    def _auth(self) -> tuple[str, str]:
        return self.headers.get("X-User", "anonymous"), self.headers.get("X-Role", "viewer")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path in {"/", "/index.html"}:
                return self._html()
            if parsed.path == "/api/health":
                return self._send({"ok": True})
            if parsed.path == "/api/projects":
                return self._send({"projects": self.db.list_projects()})
            if parsed.path == "/api/versions":
                return self._send({"versions": self.db.list_versions()})
            if parsed.path == "/api/deliveries":
                return self._send({"deliveries": self.db.list_deliveries()})
            if parsed.path == "/api/audit":
                return self._send({"audit": self.db.audit()})
            parts = [p for p in parsed.path.split("/") if p]
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "cues":
                return self._send({"cues": self.db.list_cues(int(parts[2]))})
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "comments":
                return self._send({"comments": self.db.list_comments(int(parts[2]))})
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "signatures":
                return self._send({"signatures": self.db.list_signatures(int(parts[2]))})
            raise DomainError("接口不存在", 404)
        except (ValueError, DomainError) as exc:
            self._send({"error": str(exc)}, getattr(exc, "status", 400))

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            actor, role = self._auth()
            body = self._body()
            parts = [p for p in parsed.path.split("/") if p]
            if parts == ["api", "projects"]:
                return self._send(self.db.create_project(actor, body, role), 201)
            if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "versions":
                return self._send(self.db.create_version(int(parts[2]), actor, body, role), 201)
            if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "glossary":
                return self._send(self.db.set_glossary(int(parts[2]), actor, body, role), 201)
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "assignments":
                return self._send(self.db.assign(int(parts[2]), actor, body, role), 201)
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "cues":
                return self._send(self.db.save_cue(int(parts[2]), actor, body, role), 201)
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "comments":
                return self._send(self.db.add_comment(int(parts[2]), actor, body, role), 201)
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "signatures":
                return self._send(self.db.sign_cues(int(parts[2]), actor, body, role), 201)
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] in {"submit", "lock", "deliver"}:
                version_id = int(parts[2])
                if parts[3] == "submit":
                    return self._send(self.db.submit(version_id, actor, role))
                if parts[3] == "lock":
                    return self._send(self.db.lock(version_id, actor, role))
                return self._send(self.db.deliver(version_id, actor, role))
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "review":
                return self._send(self.db.review(int(parts[2]), actor, body, role))
            raise DomainError("接口不存在", 404)
        except (ValueError, TypeError, DomainError) as exc:
            self._send({"error": str(exc)}, getattr(exc, "status", 400))

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[subtitle] {self.address_string()} - {fmt % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="字幕本地化质检与交付服务")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8009")))
    parser.add_argument("--db", default=os.getenv("SUBTITLE_DB", str(DEFAULT_DB)))
    parser.add_argument("--init", action="store_true", help="创建数据库和示例项目")
    args = parser.parse_args()
    db = Database(args.db)
    if args.init:
        seed = seed_demo(db)
        print(f"initialized database at {args.db}; project={seed['project']} version={seed['version']}")
        return
    Handler.db = db
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"subtitle-qc listening on http://127.0.0.1:{args.port} (db={args.db})")
    server.serve_forever()


if __name__ == "__main__":
    main()
