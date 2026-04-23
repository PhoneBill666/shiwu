"""长期记忆提炼（在线 + 离线补提炼）。"""

from memory.memory_policy import (
    classify_kind,
    estimate_confidence,
    estimate_offline_confidence,
    is_worth_long_term_offline,
    is_worth_long_term_online,
    should_skip_offline,
    should_skip_online,
)
from memory.memory_store import MemoryStore, build_canonical_key


def _already_exists(content: str, kind: str, store: MemoryStore) -> bool:
    key = build_canonical_key(kind, content)
    if store.is_tombstoned(key):
        return True
    if store.find_by_canonical_key(key):
        return True

    for existing in store.memories:
        existing_content = existing.get("content", "")
        if existing_content == content:
            return True
        if existing.get("kind") == kind:
            if content in existing_content or existing_content in content:
                return True
    return False


def try_extract(role: str, content: str, store: MemoryStore) -> dict | None:
    """在线提炼：对每条用户输入进行保守提炼。"""
    if role != "user":
        return None
    content = content.strip()

    if should_skip_online(content):
        return None
    if not is_worth_long_term_online(content):
        return None

    kind = classify_kind(content)
    if not kind:
        return None
    if _already_exists(content, kind, store):
        return None

    return store.add_memory(
        kind=kind,
        content=content,
        source="auto_extract",
        confidence=estimate_confidence(kind, content),
    )


def reextract_from_logs(
    store: MemoryStore,
    log_limit: int = 400,
    max_new: int = 25,
) -> dict:
    """离线补提炼：从 memory_log 回扫补漏。"""
    rows = store.iter_logs(limit=log_limit)
    stats = {
        "scanned_logs": len(rows),
        "scanned_users": 0,
        "added": 0,
        "skipped": 0,
        "duplicates": 0,
        "tombstoned": 0,
    }
    added_items: list[dict] = []

    for row in rows:
        role = row.get("role")
        content = row.get("content")
        if role != "user" or not isinstance(content, str):
            continue

        stats["scanned_users"] += 1
        content = content.strip()
        if should_skip_offline(content):
            stats["skipped"] += 1
            continue
        if not (is_worth_long_term_online(content) or is_worth_long_term_offline(content)):
            stats["skipped"] += 1
            continue

        kind = classify_kind(content)
        if not kind:
            stats["skipped"] += 1
            continue
        if _already_exists(content, kind, store):
            if store.is_tombstoned(build_canonical_key(kind, content)):
                stats["tombstoned"] += 1
            stats["duplicates"] += 1
            continue

        item = store.add_memory(
            kind=kind,
            content=content,
            source="offline_reextract",
            confidence=estimate_offline_confidence(kind, content),
        )
        added_items.append(item)
        stats["added"] += 1

        if stats["added"] >= max_new:
            break

    stats["items"] = added_items
    return stats
