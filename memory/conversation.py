import json
from datetime import datetime
from pathlib import Path

from storage_paths import SESSION_FILE, prepare_storage_layout

MAX_TURNS = 10
DEFAULT_SESSION_FILE = SESSION_FILE


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


class Conversation:
    """管理对话历史：添加消息、裁剪上下文、重置、持久化。"""

    def __init__(self, max_turns: int = MAX_TURNS, session_file: Path = DEFAULT_SESSION_FILE):
        prepare_storage_layout()
        self.max_turns = max_turns
        self.session_file = session_file
        self.history: list[dict] = []
        self._load()

    # ---- 消息操作 ----

    def add_user(self, content: str):
        self.history.append({"role": "user", "content": content, "timestamp": _now_iso()})
        self._save()

    def add_assistant(self, content: str):
        self.history.append({"role": "assistant", "content": content, "timestamp": _now_iso()})
        self._save()

    def reset(self):
        """清空对话历史并删除 session 文件。"""
        self.history.clear()
        if self.session_file.exists():
            self.session_file.unlink()

    # ---- 构造 messages ----

    def get_trimmed_history(self) -> list[dict]:
        """返回裁剪后的对话历史（不含 system prompt）。"""
        raw = self.history if len(self.history) <= self.max_turns * 2 else self.history[-(self.max_turns * 2):]
        return [{"role": item.get("role", "user"), "content": item.get("content", "")} for item in raw]

    def build_recent_timeline(self, limit: int = 6) -> str:
        recent = self.history[-limit:] if len(self.history) >= limit else self.history
        if not recent:
            return ""
        lines = [
            "【最近会话时间线】以下是当前 session 的最近消息，带时间戳。",
            "它们只代表短期上下文，不代表长期事实。",
        ]
        for msg in recent:
            role = "用户" if msg.get("role") == "user" else "助手"
            text = str(msg.get("content", "")).replace("\n", " ").strip()
            if len(text) > 80:
                text = text[:80] + "..."
            timestamp = msg.get("timestamp")
            if isinstance(timestamp, str) and timestamp:
                stamp = timestamp[:19].replace("T", " ")
            else:
                stamp = "时间未知"
            lines.append(f"- {stamp} [{role}] {text}")
        return "\n".join(lines)

    # ---- 持久化 ----

    def _save(self):
        self.session_file.parent.mkdir(parents=True, exist_ok=True)
        self.session_file.write_text(
            json.dumps(self.history, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _load(self):
        if self.session_file.exists():
            try:
                data = json.loads(self.session_file.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    fallback_timestamp = datetime.fromtimestamp(self.session_file.stat().st_mtime).astimezone().isoformat()
                    normalized: list[dict] = []
                    changed = False
                    for item in data:
                        if not isinstance(item, dict):
                            changed = True
                            continue
                        role = item.get("role")
                        content = item.get("content")
                        if role not in {"user", "assistant"} or not isinstance(content, str):
                            changed = True
                            continue
                        normalized_item = {"role": role, "content": content}
                        timestamp = item.get("timestamp")
                        if isinstance(timestamp, str) and timestamp:
                            normalized_item["timestamp"] = timestamp
                        else:
                            normalized_item["timestamp"] = fallback_timestamp
                            changed = True
                        normalized.append(normalized_item)
                    self.history = normalized
                    if changed:
                        self._save()
                    print(f"已恢复上次对话（{len(data)} 条消息）")
            except (json.JSONDecodeError, OSError):
                pass

    # ---- 摘要 ----

    def summary(self) -> str:
        if not self.history:
            return "当前没有对话历史。"
        total = len(self.history)
        turns = total // 2
        lines = [f"共 {turns} 轮对话（{total} 条消息）"]
        recent = self.history[-6:] if len(self.history) >= 6 else self.history
        lines.append("--- 最近对话 ---")
        for msg in recent:
            role = "用户" if msg["role"] == "user" else "助手"
            text = msg["content"][:60] + ("..." if len(msg["content"]) > 60 else "")
            lines.append(f"  [{role}] {text}")
        return "\n".join(lines)
