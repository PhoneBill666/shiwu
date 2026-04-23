"""长期记忆存储：原始对话日志 + 记忆条目。"""

import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from memory.memory_policy import (
    KIND_ALIASES,
    SUPPORTED_MEMORY_KINDS,
    build_description,
    fix_legacy_kind,
    infer_explicit,
    is_low_value_memory_content,
)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DEFAULT_LOG_FILE = DATA_DIR / "memory_log.jsonl"
DEFAULT_MEMORIES_FILE = DATA_DIR / "memories.json"
DEFAULT_TOMBSTONES_FILE = DATA_DIR / "memory_tombstones.json"


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def _normalize_text_for_key(text: str) -> str:
    lowered = text.lower().strip()
    return re.sub(r"[\s\.,!?;:，。！？；：、\"'`“”‘’（）()\[\]{}<>《》/\\|_-]+", "", lowered)


def build_canonical_key(kind: str, content: str) -> str:
    return f"{kind}:{_normalize_text_for_key(content)[:120]}"


def _normalize_confidence(value: Any, source: str) -> float:
    if isinstance(value, (int, float)):
        return max(0.0, min(1.0, float(value)))
    if source in {"user_command", "manual", "manual_cleanup"}:
        return 0.95
    return 0.80


def _normalize_explicit(value: Any, source: str) -> bool:
    if isinstance(value, bool):
        return value
    return infer_explicit(source)


def _normalize_kind(raw: Any) -> str:
    if not isinstance(raw, str):
        return "other"
    kind = KIND_ALIASES.get(raw, raw)
    if kind in SUPPORTED_MEMORY_KINDS:
        return kind
    return "other"


class MemoryStore:
    def __init__(
        self,
        log_file: Path = DEFAULT_LOG_FILE,
        memories_file: Path = DEFAULT_MEMORIES_FILE,
        tombstones_file: Path = DEFAULT_TOMBSTONES_FILE,
    ):
        self.log_file = log_file
        self.memories_file = memories_file
        self.tombstones_file = tombstones_file
        self.memories: list[dict] = []
        self.tombstones: list[dict] = []
        self._load_memories()
        self._load_tombstones()

    # ---- 原始对话日志 (JSONL append-only) ----

    def append_log(self, role: str, content: str):
        """追加一条消息到对话日志。"""
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp": _now_iso(),
            "role": role,
            "content": content,
        }
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # ---- 长期记忆条目 ----

    def add_memory(
        self,
        kind: str,
        content: str,
        source: str = "conversation",
        confidence: float | None = None,
        description: str | None = None,
        explicit: bool | None = None,
    ) -> dict:
        """添加一条长期记忆（若重复则返回现有记忆并刷新 last_used_at）。"""
        kind = _normalize_kind(kind)
        content = content.strip()
        if not content:
            raise ValueError("memory content cannot be empty")

        key = build_canonical_key(kind, content)
        existing = self.find_by_canonical_key(key)
        now = _now_iso()
        if existing:
            existing["last_used_at"] = now
            existing["updated_at"] = now
            self._save_memories()
            return existing

        item = {
            "id": uuid.uuid4().hex[:8],
            "kind": kind,
            "content": content,
            "created_at": now,
            "last_used_at": now,
            "updated_at": now,
            "source": source,
            "confidence": _normalize_confidence(confidence, source),
            "canonical_key": key,
            "description": description if isinstance(description, str) and description.strip() else build_description(kind, content),
            "explicit": _normalize_explicit(explicit, source),
        }
        self.memories.append(item)
        self._save_memories()
        return item

    def remove_memory(self, memory_id: str) -> bool:
        """按 id 删除一条记忆。"""
        removed_item = next((m for m in self.memories if m["id"] == memory_id), None)
        before = len(self.memories)
        self.memories = [m for m in self.memories if m["id"] != memory_id]
        if len(self.memories) < before:
            self._save_memories()
            if removed_item:
                self.add_tombstone(
                    kind=str(removed_item.get("kind", "other")),
                    content=str(removed_item.get("content", "")),
                    source="forget_command",
                )
            return True
        return False

    def touch_memory(self, memory_id: str):
        """更新记忆的 last_used_at。"""
        for m in self.memories:
            if m["id"] == memory_id:
                now = _now_iso()
                m["last_used_at"] = now
                if "updated_at" not in m:
                    m["updated_at"] = now
                self._save_memories()
                break

    def find_by_canonical_key(self, canonical_key: str) -> dict | None:
        for m in self.memories:
            if m.get("canonical_key") == canonical_key:
                return m
        return None

    def is_tombstoned(self, canonical_key: str) -> bool:
        for item in self.tombstones:
            if item.get("canonical_key") == canonical_key:
                return True
        return False

    def add_tombstone(self, kind: str, content: str, source: str = "manual") -> dict:
        norm_kind = _normalize_kind(kind)
        key = build_canonical_key(norm_kind, content)
        now = _now_iso()
        existing = next((m for m in self.tombstones if m.get("canonical_key") == key), None)
        if existing:
            existing["updated_at"] = now
            existing["source"] = source
            self._save_tombstones()
            return existing

        item = {
            "id": uuid.uuid4().hex[:8],
            "kind": norm_kind,
            "canonical_key": key,
            "source": source,
            "created_at": now,
            "updated_at": now,
        }
        self.tombstones.append(item)
        self._save_tombstones()
        return item

    def display(self, limit: int = 20) -> str:
        """显示记忆条目列表。"""
        if not self.memories:
            return "暂无长期记忆。"
        lines = [f"共 {len(self.memories)} 条长期记忆："]
        shown = self.memories[-limit:]
        for m in shown:
            kind = m["kind"]
            content = m["content"][:50] + ("..." if len(m["content"]) > 50 else "")
            conf = f"{m.get('confidence', 0.0):.2f}"
            exp = "manual" if m.get("explicit") else "auto"
            lines.append(f"  [{m['id']}] ({kind}, {exp}, conf={conf}) {content}")
        if len(self.memories) > limit:
            lines.append(f"  ... 还有 {len(self.memories) - limit} 条未显示")
        return "\n".join(lines)

    def iter_logs(self, limit: int | None = None) -> list[dict]:
        """读取原始对话日志（为后续再提炼预留）。"""
        if not self.log_file.exists():
            return []
        rows: list[dict] = []
        with open(self.log_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    rows.append(obj)
        if limit is not None and limit > 0:
            return rows[-limit:]
        return rows

    # ---- 持久化 ----

    def _load_memories(self):
        changed = False
        if self.memories_file.exists():
            try:
                data = json.loads(self.memories_file.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    normalized: list[dict] = []
                    seen_ids: set[str] = set()
                    seen_keys: set[str] = set()
                    for raw in data:
                        item = self._normalize_memory_item(raw)
                        if not item:
                            changed = True
                            continue
                        if isinstance(raw, dict):
                            if raw.get("kind") != item["kind"]:
                                changed = True
                            if raw.get("canonical_key") != item["canonical_key"]:
                                changed = True
                            if raw.get("description") != item["description"]:
                                changed = True
                            if raw.get("explicit") != item["explicit"]:
                                changed = True
                            if raw.get("updated_at") != item["updated_at"]:
                                changed = True
                        else:
                            changed = True
                        if item["id"] in seen_ids:
                            changed = True
                            continue
                        # canonical 去重：保留 first seen
                        ckey = item.get("canonical_key")
                        if ckey and ckey in seen_keys:
                            changed = True
                            continue
                        seen_ids.add(item["id"])
                        if ckey:
                            seen_keys.add(ckey)
                        normalized.append(item)
                    self.memories = normalized
            except (json.JSONDecodeError, OSError):
                pass
        if changed:
            self._save_memories()

    def _save_memories(self):
        self.memories_file.parent.mkdir(parents=True, exist_ok=True)
        self.memories_file.write_text(
            json.dumps(self.memories, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _load_tombstones(self):
        if not self.tombstones_file.exists():
            self.tombstones = []
            return
        try:
            data = json.loads(self.tombstones_file.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                self.tombstones = []
                return
            normalized: list[dict] = []
            seen: set[str] = set()
            now = _now_iso()
            for raw in data:
                if not isinstance(raw, dict):
                    continue
                ckey = raw.get("canonical_key")
                if not isinstance(ckey, str) or not ckey.strip():
                    continue
                ckey = ckey.strip()
                if ckey in seen:
                    continue
                seen.add(ckey)
                kind = raw.get("kind")
                if not isinstance(kind, str):
                    kind = ckey.split(":", 1)[0] if ":" in ckey else "other"
                kind = _normalize_kind(kind)
                source = raw.get("source") if isinstance(raw.get("source"), str) else "legacy"
                created_at = raw.get("created_at") if isinstance(raw.get("created_at"), str) else now
                updated_at = raw.get("updated_at") if isinstance(raw.get("updated_at"), str) else created_at
                tid = raw.get("id")
                if not isinstance(tid, str) or not tid.strip():
                    tid = uuid.uuid4().hex[:8]
                normalized.append(
                    {
                        "id": tid,
                        "kind": kind,
                        "canonical_key": ckey,
                        "source": source,
                        "created_at": created_at,
                        "updated_at": updated_at,
                    }
                )
            self.tombstones = normalized
            self._save_tombstones()
        except (json.JSONDecodeError, OSError):
            self.tombstones = []

    def _save_tombstones(self):
        self.tombstones_file.parent.mkdir(parents=True, exist_ok=True)
        self.tombstones_file.write_text(
            json.dumps(self.tombstones, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _normalize_memory_item(self, raw: Any) -> dict | None:
        if not isinstance(raw, dict):
            return None

        content = raw.get("content")
        if not isinstance(content, str):
            return None
        content = content.strip()
        if not content:
            return None

        source = raw.get("source")
        if not isinstance(source, str):
            source = "legacy"

        kind = _normalize_kind(raw.get("kind"))
        kind = fix_legacy_kind(kind, content)
        if is_low_value_memory_content(content):
            return None
        now = _now_iso()
        created_at = raw.get("created_at") if isinstance(raw.get("created_at"), str) else now
        last_used_at = raw.get("last_used_at") if isinstance(raw.get("last_used_at"), str) else created_at
        updated_at = raw.get("updated_at") if isinstance(raw.get("updated_at"), str) else last_used_at

        memory_id = raw.get("id")
        if not isinstance(memory_id, str) or not memory_id.strip():
            memory_id = uuid.uuid4().hex[:8]

        computed_key = build_canonical_key(kind, content)
        canonical_key = raw.get("canonical_key")
        if not isinstance(canonical_key, str) or not canonical_key.strip():
            canonical_key = computed_key
        elif not canonical_key.startswith(f"{kind}:"):
            canonical_key = computed_key

        confidence = _normalize_confidence(raw.get("confidence"), source)
        explicit = _normalize_explicit(raw.get("explicit"), source)
        description_raw = raw.get("description")
        description = (
            description_raw.strip()
            if isinstance(description_raw, str) and description_raw.strip()
            else build_description(kind, content)
        )

        return {
            "id": memory_id,
            "kind": kind,
            "content": content,
            "created_at": created_at,
            "last_used_at": last_used_at,
            "updated_at": updated_at,
            "source": source,
            "confidence": confidence,
            "canonical_key": canonical_key,
            "description": description,
            "explicit": explicit,
        }
