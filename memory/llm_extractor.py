"""基于 LLM 的长期记忆提取。

替代旧的关键词规则提取，让模型自己判断对话中有没有值得长期记住的信息。
参考 claude-code 的 extractMemories 思路，但简化为适合本地 9B 模型的版本。
"""

import json
import re
import sys

from core.model import Spinner
from memory.memory_store import MemoryStore, build_canonical_key

# 每次提取分析的最大消息数（最近 N 条，约 2-3 轮对话）
CONTEXT_WINDOW = 6

# 提取频率：每 N 轮对话触发一次提取（1 = 每轮都提取）
EXTRACT_EVERY_N_TURNS = 1

EXTRACTION_SYSTEM = (
    "你是一个记忆提取助手。分析对话，提取值得记住的**新**信息。\n"
    "\n"
    "## 记忆类型\n"
    "- user_identity: 用户身份、角色、背景、性格特点、能力\n"
    "- user_preference: 用户偏好、沟通风格、情绪需求、交流方式、行为要求\n"
    "- project_context: 正在做的项目、目标、计划、当前阶段（如考试周、实习期）、正在学的课程\n"
    "- technical_constraint: 技术选型、架构约束\n"
    "- assistant_identity: 对助手的定位、命名、角色设定\n"
    "\n"
    "## 提取原则\n"
    "1. 提取有持续参考价值的信息，包括：\n"
    "   - 稳定事实：身份、偏好、技术栈等\n"
    "   - 阶段性事实：当前在上什么课、正在准备什么考试、近期目标等\n"
    "   - 重要经历：考试成绩、项目进展、关键事件等\n"
    "2. 跳过的内容：纯闲聊、一次性提问（如「今天天气怎么样」）、临时调试\n"
    "3. 用户明确或隐含表达的偏好都要捕捉——尤其注意沟通风格和情绪需求\n"
    "4. 用一句精炼的中文总结，不要照搬原文\n"
    "5. 没有值得记住的内容时返回空数组\n"
    "\n"
    "## 去重规则（极其重要）\n"
    "仔细阅读下方已有记忆列表。如果对话中的信息与任何已有记忆**语义相同或高度相似**，\n"
    "即使措辞不同，也**绝对不要**重复提取。\n"
    "例如：已有「回答风格必须直接」，就不要再提取「用户希望回答风格准确直接」。\n"
    "只提取已有记忆中**完全没有覆盖**的新信息。\n"
    "\n"
    "## 墓碑规则（极其重要）\n"
    "如果下方提供了“已删除/禁止恢复的记忆”，这些内容说明用户明确不想保留。\n"
    "只要语义相同或高度相似，即使换一种说法，也**绝对不要重新提取**。\n"
    "\n"
    "## 输出格式\n"
    "只输出 JSON，不要输出任何其他文字：\n"
    '{"memories": [{"kind": "类型", "content": "一句话总结"}]}\n'
    "没有新记忆时：\n"
    '{"memories": []}\n'
)


def _build_user_prompt(
    recent_messages: list[dict],
    existing_summaries: str,
) -> str:
    parts = []
    if existing_summaries:
        parts.append(
            "## 已有记忆（以下内容已经记住了，不要重复提取语义相同的内容）\n"
            f"{existing_summaries}\n"
        )
    parts.append("## 最近对话")
    for msg in recent_messages:
        role = "用户" if msg["role"] == "user" else "助手"
        content = msg["content"]
        if len(content) > 500:
            content = content[:500] + "..."
        parts.append(f"{role}: {content}")
    parts.append("\n请提取值得长期记住的新信息（如果没有新信息则返回空数组）：")
    return "\n".join(parts)


def _get_existing_summaries(store: MemoryStore) -> str:
    """按类型分组展示所有已有记忆，不截断，让模型看到完整内容以判断重复。"""
    if not store.memories and not store.tombstones:
        return ""

    by_kind: dict[str, list[str]] = {}
    for m in store.memories:
        kind = m.get("kind", "other")
        content = m.get("content", "")
        by_kind.setdefault(kind, []).append(content)

    kind_labels = {
        "user_identity": "用户身份",
        "user_preference": "用户偏好",
        "project_context": "项目背景",
        "technical_constraint": "技术约束",
        "assistant_identity": "助手身份",
    }

    lines = []
    for kind, items in by_kind.items():
        label = kind_labels.get(kind, kind)
        lines.append(f"### {label}")
        for item in items:
            lines.append(f"- {item}")

    tombstone_items = _get_tombstone_summaries(store)
    if tombstone_items:
        lines.append("### 已删除/禁止恢复的记忆")
        for item in tombstone_items:
            lines.append(f"- {item}")
    return "\n".join(lines)


def _get_tombstone_summaries(store: MemoryStore) -> list[str]:
    items: list[str] = []
    for tombstone in store.tombstones:
        content = tombstone.get("content")
        if not isinstance(content, str) or not content.strip():
            canonical_key = tombstone.get("canonical_key", "")
            if isinstance(canonical_key, str) and ":" in canonical_key:
                content = canonical_key.split(":", 1)[1]
            else:
                continue
        kind = tombstone.get("kind", "other")
        items.append(f"({kind}) {content.strip()}")
    return items


# ---- 去重逻辑 ----

# 中文停用字（虚词、助词、连接词，去重时忽略）
_STOP_CHARS = set(
    "的了是在用要不也都会能有这那就和与对给让把被从而且或但如果因为所以虽然可以应该需要已经正在"
)
_PUNCT_RE = re.compile(
    r"[，。！？、；：\u201c\u201d\u2018\u2019（）【】《》\s.,!?;:\"'\-()\[\]{}<>/\\|_]+"
)


def _extract_keywords(text: str) -> set[str]:
    """提取关键字集合：去停用词后的中文单字 + 英文 token。"""
    text = _PUNCT_RE.sub("", text.lower())
    keywords = set()
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff" and ch not in _STOP_CHARS:
            keywords.add(ch)
    keywords.update(re.findall(r"[a-z0-9]{2,}", text))
    return keywords


def _extract_trigrams(text: str) -> set[str]:
    """提取中文三字短语（去停用词后的连续三字）。"""
    text = _PUNCT_RE.sub("", text)
    chars = [ch for ch in text if "\u4e00" <= ch <= "\u9fff" and ch not in _STOP_CHARS]
    return {chars[i] + chars[i + 1] + chars[i + 2] for i in range(len(chars) - 2)}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _is_semantically_similar(content: str, kind: str, existing_kind: str, existing_content: str) -> bool:
    content_clean = re.sub(r"\s+", "", content.lower())
    existing_clean = re.sub(r"\s+", "", existing_content.lower())

    if content_clean in existing_clean or existing_clean in content_clean:
        return True

    new_keywords = _extract_keywords(content)
    existing_keywords = _extract_keywords(existing_content)
    kw_sim = _jaccard(new_keywords, existing_keywords)
    threshold = 0.35 if existing_kind == kind else 0.50
    if kw_sim >= threshold:
        return True

    new_trigrams = _extract_trigrams(content)
    existing_trigrams = _extract_trigrams(existing_content)
    shared = new_trigrams & existing_trigrams
    return len(shared) >= 2


def _is_tombstone_match(content: str, kind: str, store: MemoryStore) -> bool:
    key = build_canonical_key(kind, content)
    if store.is_tombstoned(key):
        return True

    for tombstone in store.tombstones:
        existing_content = tombstone.get("content")
        if not isinstance(existing_content, str) or not existing_content.strip():
            canonical_key = tombstone.get("canonical_key", "")
            if isinstance(canonical_key, str) and ":" in canonical_key:
                existing_content = canonical_key.split(":", 1)[1]
            else:
                continue
        existing_kind = tombstone.get("kind", "other")
        if _is_semantically_similar(content, kind, str(existing_kind), existing_content):
            return True
    return False


def _is_duplicate(content: str, kind: str, store: MemoryStore) -> bool:
    """检查新提取的记忆是否和已有记忆语义重复。

    三层防线：
    1. canonical_key / 墓碑 精确匹配
    2. 子串包含
    3. 关键字 Jaccard ≥ 0.35 或共享三字短语 ≥ 2
    """
    if _is_tombstone_match(content, kind, store):
        return True

    key = build_canonical_key(kind, content)
    if store.find_by_canonical_key(key):
        return True

    for existing in store.memories:
        existing_content = existing.get("content", "")
        if not isinstance(existing_content, str) or not existing_content.strip():
            continue
        if _is_semantically_similar(content, kind, str(existing.get("kind", "other")), existing_content):
            return True

    return False


VALID_KINDS = {
    "user_identity",
    "user_preference",
    "project_context",
    "technical_constraint",
    "assistant_identity",
}

# 模块级计数器
_turn_counter = 0


def try_llm_extract(model, conversation_history: list[dict], store: MemoryStore) -> list[dict]:
    """用 LLM 从最近对话中提取长期记忆。"""
    global _turn_counter
    _turn_counter += 1

    if _turn_counter % EXTRACT_EVERY_N_TURNS != 0:
        return []

    if model._extracting:
        return []
    model._extracting = True

    try:
        recent = conversation_history[-CONTEXT_WINDOW:]
        if not recent:
            return []

        existing = _get_existing_summaries(store)
        messages = [
            {"role": "system", "content": EXTRACTION_SYSTEM},
            {"role": "user", "content": _build_user_prompt(recent, existing)},
        ]

        sys.stdout.write("\r💭 记忆提取中...")
        sys.stdout.flush()

        raw = model.generate_silent(messages, max_tokens=300)

        sys.stdout.write("\r\033[K")
        sys.stdout.flush()

        candidates = _parse_extraction_result(raw)
        if not candidates:
            return []

        added = []
        for item in candidates:
            kind = item.get("kind", "")
            content = item.get("content", "")

            if not isinstance(content, str) or not content.strip():
                continue
            content = content.strip()
            if len(content) < 6:
                continue
            if kind not in VALID_KINDS:
                continue
            if _is_duplicate(content, kind, store):
                continue

            saved = store.add_memory(
                kind=kind,
                content=content,
                source="llm_extract",
                confidence=0.85,
            )
            added.append(saved)

        if added:
            names = [f"({m['kind']}) {m['content'][:30]}" for m in added]
            print(f"  📝 新记忆: {'; '.join(names)}")

        return added

    except Exception:
        sys.stdout.write("\r\033[K")
        sys.stdout.flush()
        return []
    finally:
        model._extracting = False


def reextract_from_logs_llm(
    model,
    store: MemoryStore,
    log_limit: int = 200,
    batch_size: int = 8,
    max_new: int = 25,
) -> dict:
    """离线补提炼：用 LLM 从历史日志中批量提取记忆。"""
    rows = store.iter_logs(limit=log_limit)
    total_batches = (len(rows) + batch_size - 1) // batch_size
    stats = {
        "scanned_logs": len(rows),
        "batches": 0,
        "added": 0,
        "duplicates": 0,
        "tombstoned": 0,
    }

    spinner = Spinner("离线补提炼中")
    spinner.start()

    for i in range(0, len(rows), batch_size):
        if stats["added"] >= max_new:
            break

        batch = rows[i : i + batch_size]
        messages_batch = []
        for row in batch:
            role = row.get("role")
            content = row.get("content")
            if role in ("user", "assistant") and isinstance(content, str):
                messages_batch.append({"role": role, "content": content})

        if not messages_batch:
            continue

        stats["batches"] += 1
        spinner._message = f"离线补提炼中 ({stats['batches']}/{total_batches})"

        # 每批次重新获取已有记忆（因为前面批次可能新增了）
        existing = _get_existing_summaries(store)
        messages = [
            {"role": "system", "content": EXTRACTION_SYSTEM},
            {"role": "user", "content": _build_user_prompt(messages_batch, existing)},
        ]

        try:
            raw = model.generate_silent(messages, max_tokens=400)
            candidates = _parse_extraction_result(raw)

            for item in candidates:
                kind = item.get("kind", "")
                content = item.get("content", "")
                if not isinstance(content, str) or not content.strip():
                    continue
                content = content.strip()
                if len(content) < 6:
                    continue
                if kind not in VALID_KINDS:
                    continue
                if _is_tombstone_match(content, kind, store):
                    stats["tombstoned"] += 1
                    stats["duplicates"] += 1
                    continue
                if _is_duplicate(content, kind, store):
                    stats["duplicates"] += 1
                    continue

                # 暂停 spinner，输出新记忆信息，再恢复
                spinner.stop()
                print(f"  📝 新记忆: ({kind}) {content[:50]}")
                spinner = Spinner(f"离线补提炼中 ({stats['batches']}/{total_batches})")
                spinner.start()

                store.add_memory(
                    kind=kind,
                    content=content,
                    source="llm_reextract",
                    confidence=0.80,
                )
                stats["added"] += 1

                if stats["added"] >= max_new:
                    break
        except Exception:
            continue

    spinner.stop()
    return stats


def _parse_extraction_result(text: str) -> list[dict]:
    """从模型输出中解析 JSON，容错处理。"""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = text.strip()

    # 直接解析
    try:
        data = json.loads(text)
        if isinstance(data, dict) and "memories" in data:
            return data["memories"]
        return []
    except json.JSONDecodeError:
        pass

    # 从文本中提取 JSON
    match = re.search(r"\{.*\"memories\".*\}", text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group())
            if isinstance(data, dict) and "memories" in data:
                return data["memories"]
        except json.JSONDecodeError:
            pass

    # 代码块
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(1))
            if isinstance(data, dict) and "memories" in data:
                return data["memories"]
        except json.JSONDecodeError:
            pass

    return []
