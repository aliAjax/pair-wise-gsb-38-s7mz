"""纯领域校验：字幕摘要、字段校验、失效原因判定。

本模块不接触 SQL 与 HTTP，方便被数据层和测试单独复用。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


class DomainError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def cue_digest(cue_index: int, start_ms: int, end_ms: int, text: str) -> str:
    """一句字幕在某一时刻的“文本 + 时间轴摘要”。

    句号、起止毫秒、文本任一字段变化都会得到不同摘要；
    句级签署记录的就是确认当时的这个摘要。
    """
    payload = {
        "cue_index": int(cue_index),
        "start_ms": int(start_ms),
        "end_ms": int(end_ms),
        "text": text,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def parse_cue(payload: dict[str, Any], duration_ms: int) -> tuple[int, int, int, str]:
    """解析并校验一条字幕的提交内容，返回 (句号, 起点, 终点, 文本)。"""
    try:
        cue_index = int(payload.get("cue_index"))
        start_ms = int(payload.get("start_ms"))
        end_ms = int(payload.get("end_ms"))
    except (TypeError, ValueError) as exc:
        raise DomainError("字幕序号和时间必须是整数") from exc
    text = str(payload.get("text", "")).strip()
    if cue_index < 0 or start_ms < 0 or end_ms <= start_ms or end_ms > int(duration_ms) or not text:
        raise DomainError("字幕时间、序号或内容不合法")
    return cue_index, start_ms, end_ms, text


def invalidation_reason(old: Any, cue_index: int, start_ms: int, end_ms: int, text: str) -> str:
    """对比新旧字幕，返回该句已有签署的失效原因；内容未变则返回空串。"""
    changes: list[str] = []
    if text != old["text"]:
        changes.append("文本已修改")
    if int(start_ms) != int(old["start_ms"]) or int(end_ms) != int(old["end_ms"]):
        changes.append("时间轴已修改")
    if int(cue_index) != int(old["cue_index"]):
        changes.append("句号已调整")
    return "、".join(changes)


def format_cue_indexes(indexes: list[int]) -> str:
    return "、".join(str(i) for i in indexes)


def enforce_glossary(glossary_rows: list[Any], text: str) -> None:
    for row in glossary_rows:
        forbidden = json.loads(row["forbidden_terms"])
        for term in forbidden:
            if term and term in text:
                raise DomainError(f"字幕包含禁用译法: {term}")
        # 术语表只在对应源词出现在本地化字幕中时才强制，
        # 避免要求每条字幕都重复所有术语。
        if row["source_term"] in text and row["required_translation"] not in text:
            raise DomainError(f"术语 {row['source_term']} 必须使用指定译法 {row['required_translation']}")
