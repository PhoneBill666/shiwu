import json
from pathlib import Path

MAX_TURNS = 10
DEFAULT_SESSION_FILE = Path(__file__).resolve().parent.parent / "data" / "session.json"


class Conversation:
    """管理对话历史：添加消息、裁剪上下文、重置、持久化。"""

    def __init__(self, max_turns: int = MAX_TURNS, session_file: Path = DEFAULT_SESSION_FILE):
        self.max_turns = max_turns
        self.session_file = session_file
        self.history: list[dict] = []
        self._load()

    # ---- 消息操作 ----

    def add_user(self, content: str):
        self.history.append({"role": "user", "content": content})
        self._save()

    def add_assistant(self, content: str):
        self.history.append({"role": "assistant", "content": content})
        self._save()

    def reset(self):
        """清空对话历史并删除 session 文件。"""
        self.history.clear()
        if self.session_file.exists():
            self.session_file.unlink()

    # ---- 构造 messages ----

    def get_trimmed_history(self) -> list[dict]:
        """返回裁剪后的对话历史（不含 system prompt）。"""
        if len(self.history) <= self.max_turns * 2:
            return list(self.history)
        return list(self.history[-(self.max_turns * 2):])

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
                    self.history = data
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
