"""从长期记忆中检索与当前对话相关的条目。

策略：关键词命中优先 + 保守注入（宁缺毋滥）。
"""

import re
from datetime import datetime

from memory.memory_store import MemoryStore

MAX_INJECTED = 5
MIN_RELEVANCE_SCORE = 2

MEMORY_RECALL_HINTS = [
    "你记得",
    "你对我有什么记忆",
    "你对我的了解",
    "我是谁",
    "总结一下你对我的了解",
]

ASSISTANT_IDENTITY_HINTS = [
    "你是谁",
    "你叫什么",
    "助手名字",
    "你的名字",
]


def _tokenize(query: str) -> set[str]:
    q = query.lower()
    ascii_tokens = re.findall(r"[a-z0-9_]{2,}", q)
    cjk_chars = [ch for ch in q if "\u4e00" <= ch <= "\u9fff"]
    # 中文用双字片段降噪（单字噪音太大）
    cjk_bigrams = [cjk_chars[i] + cjk_chars[i + 1] for i in range(len(cjk_chars) - 1)]
    return set(ascii_tokens + cjk_bigrams)


def _is_memory_recall_query(query: str) -> bool:
    return any(h in query for h in MEMORY_RECALL_HINTS)


def _is_assistant_identity_query(query: str) -> bool:
    return any(h in query for h in ASSISTANT_IDENTITY_HINTS)


def _kind_boost(kind: str, query: str) -> int:
    if "我是谁" in query and kind == "user_identity":
        return 8
    if ("偏好" in query or "风格" in query) and kind == "user_preference":
        return 6
    if ("项目" in query or "在做什么" in query) and kind == "project_context":
        return 5
    if ("技术路线" in query or "约束" in query) and kind == "technical_constraint":
        return 6
    return 0


def retrieve(query: str, store: MemoryStore, limit: int = MAX_INJECTED) -> list[dict]:
    if not store.memories:
        return []

    tokens = _tokenize(query)
    recall_query = _is_memory_recall_query(query)
    assistant_identity_query = _is_assistant_identity_query(query)

    scored: list[tuple[float, dict]] = []
    for m in store.memories:
        kind = str(m.get("kind", "other"))
        if kind == "assistant_identity" and not assistant_identity_query:
            continue

        content_lower = m["content"].lower()
        desc_lower = str(m.get("description", "")).lower()
        content_hits = sum(1 for token in tokens if token in content_lower)
        desc_hits = sum(1 for token in tokens if token in desc_lower)
        conf = float(m.get("confidence", 0.80))
        explicit_boost = 0.8 if bool(m.get("explicit")) else 0.0
        score = content_hits * 3 + desc_hits * 2 + _kind_boost(kind, query) + conf + explicit_boost
        scored.append((score, m))

    scored.sort(key=lambda x: x[0], reverse=True)

    top = [m for score, m in scored[:limit] if score >= MIN_RELEVANCE_SCORE]
    if recall_query:
        # 记忆查询场景允许更宽松召回，避免只注入 1 条导致总结过窄
        relaxed = [m for score, m in scored if score >= 1][:limit]
        if len(relaxed) > len(top):
            top = relaxed
        if len(top) < min(2, limit):
            by_conf = sorted(
                (m for _, m in scored),
                key=lambda item: float(item.get("confidence", 0.0)),
                reverse=True,
            )
            selected_ids = {m["id"] for m in top}
            for item in by_conf:
                if item["id"] in selected_ids:
                    continue
                top.append(item)
                selected_ids.add(item["id"])
                if len(top) >= limit:
                    break
    if not top:
        return []

    # 更新 last_used_at（批量更新，只写一次磁盘）
    ids = {m["id"] for m in top}
    now = datetime.now().astimezone().isoformat()
    changed = False
    for m in store.memories:
        if m["id"] in ids:
            m["last_used_at"] = now
            changed = True
    if changed:
        store._save_memories()

    return top


def format_for_prompt(memories: list[dict]) -> str:
    if not memories:
        return ""
    lines = [
        "【长期记忆】以下条目是稳定长期信息。",
        "回答事实型问题时，优先使用这些长期记忆，不要从短期会话中臆造长期事实。",
    ]
    for m in memories:
        kind = m.get("kind", "other")
        desc = m.get("description")
        content = m.get("content", "")
        if isinstance(desc, str) and desc.strip():
            lines.append(f"- ({kind}) {desc} | 详情: {content}")
        else:
            lines.append(f"- ({kind}) {content}")
    return "\n".join(lines)
