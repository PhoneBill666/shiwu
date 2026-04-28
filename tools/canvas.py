"""Canvas Calendar Feed 工具。"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx

from core.temporal_context import format_temporal_ranges, query_mentions_past_time, resolve_temporal_ranges
from storage_paths import CANVAS_CONFIG_FILE, CANVAS_HISTORY_FILE, ROOT_DIR, prepare_storage_layout

DEFAULT_CANVAS_DAYS = 14
MAX_CANVAS_DAYS = 60
DEFAULT_CANVAS_LIMIT = 12
CANVAS_TIMEOUT = 15
ENV_CANVAS_FEED_URL = "CANVAS_FEED_URL"
STATUS_PENDING = "pending"
STATUS_COMPLETED = "completed"

WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

_CANVAS_RE = re.compile(r"canvas", re.IGNORECASE)
_CANVAS_SCHEDULE_HINTS = [
    "日程",
    "calendar",
    "作业",
    "考试",
    "截止",
    "due",
    "ddl",
    "deadline",
    "quiz",
    "exam",
    "midterm",
    "final",
    "安排",
    "提醒",
    "schedule",
]
_ASSIGNMENT_HINTS = ["assignment", "homework", "submission", "project", "essay", "journal", "lab", "due", "作业", "截止", "ddl", "实验", "日志"]
_EXAM_HINTS = ["exam", "quiz", "test", "term test", "midterm", "final", "测验", "考试", "期中", "期末"]
_COMPLETE_INTENT_HINTS = ["完成了", "已经完成", "已完成", "做完了", "做完", "提交了", "交了", "finished", "done", "submitted"]
_UNDO_COMPLETE_INTENT_HINTS = ["改回未完成", "撤销完成", "取消完成", "标记为未完成", "设为未完成", "undo", "uncomplete", "unfinished", "not done"]


@dataclass
class CanvasEvent:
    uid: str
    title: str
    starts_at: datetime | None
    ends_at: datetime | None
    due_at: datetime | None
    starts_all_day: bool
    due_all_day: bool
    description: str = ""
    location: str = ""
    kind: str = "event"
    status: str = STATUS_PENDING

    @property
    def anchor_at(self) -> datetime | None:
        return self.due_at or self.starts_at or self.ends_at

    @property
    def is_all_day(self) -> bool:
        if self.due_at is not None:
            return self.due_all_day
        return self.starts_all_day


def load_canvas_config(config_file: Path = CANVAS_CONFIG_FILE) -> dict:
    prepare_storage_layout()
    env_feed_url = _load_env_canvas_feed_url()
    if env_feed_url:
        return {"feed_url": env_feed_url, "source": "env"}

    if not config_file.exists():
        return {"feed_url": None, "source": None}
    try:
        data = json.loads(config_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"feed_url": None, "source": None}
    if not isinstance(data, dict):
        return {"feed_url": None, "source": None}
    return {"feed_url": data.get("feed_url"), "source": "file"}


def save_canvas_feed_url(feed_url: str, config_file: Path = CANVAS_CONFIG_FILE) -> dict:
    prepare_storage_layout()
    cleaned = feed_url.strip()
    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Canvas Calendar Feed 链接无效，请粘贴完整的 http(s) 链接。")

    _save_canvas_feed_url_to_env(cleaned)

    payload = {"feed_url": cleaned}
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def clear_canvas_feed_url(config_file: Path = CANVAS_CONFIG_FILE) -> bool:
    prepare_storage_layout()
    removed = False
    if _remove_canvas_feed_url_from_env():
        removed = True
    if config_file.exists():
        config_file.unlink()
        removed = True
    return removed


def canvas_status(config_file: Path = CANVAS_CONFIG_FILE) -> str:
    config = load_canvas_config(config_file)
    feed_url = config.get("feed_url")
    if not isinstance(feed_url, str) or not feed_url.strip():
        return "Canvas Calendar Feed 尚未配置。可在 .env 中设置 CANVAS_FEED_URL，或使用 /canvas set-feed <链接>。"

    source = "环境变量" if config.get("source") == "env" else "本地配置文件"
    history = load_canvas_history()
    last_fetched_at = history.get("last_fetched_at")
    tail = f"，最近一次抓取: {last_fetched_at[:19].replace('T', ' ')}" if isinstance(last_fetched_at, str) else ""
    indexed_count = len(history.get("history_entries", [])) if isinstance(history.get("history_entries"), list) else 0
    return f"Canvas Calendar Feed 已配置（来源: {source}）：{_mask_feed_url(feed_url)}{tail}，已索引 {indexed_count} 条去重事件"


def normalize_canvas_days(days: int | str | None, default: int = DEFAULT_CANVAS_DAYS) -> int:
    if days is None or days == "":
        return default
    value = int(days)
    if value <= 0:
        raise ValueError("天数必须是正整数。")
    return min(value, MAX_CANVAS_DAYS)


def should_auto_check_canvas(text: str) -> bool:
    lowered = text.lower()
    if not _CANVAS_RE.search(lowered):
        return False
    return any(hint in lowered for hint in _CANVAS_SCHEDULE_HINTS)


def should_mark_canvas_completed(text: str) -> bool:
    lowered = text.lower()
    if not any(hint in lowered for hint in _COMPLETE_INTENT_HINTS):
        return False
    return _CANVAS_RE.search(lowered) is not None or any(keyword in lowered for keyword in _ASSIGNMENT_HINTS + _EXAM_HINTS)


def should_mark_canvas_pending(text: str) -> bool:
    lowered = text.lower()
    if not any(hint in lowered for hint in _UNDO_COMPLETE_INTENT_HINTS):
        return False
    return _CANVAS_RE.search(lowered) is not None or any(keyword in lowered for keyword in _ASSIGNMENT_HINTS + _EXAM_HINTS)


def get_canvas_context_for_query(query: str, days: int = DEFAULT_CANVAS_DAYS) -> str:
    ranges = resolve_temporal_ranges(query)
    if ranges and query_mentions_past_time(query):
        history_text = get_canvas_history_for_query(query)
        if history_text:
            return history_text
    return get_canvas_schedule(days=days)


def get_canvas_schedule(days: int = DEFAULT_CANVAS_DAYS, limit: int = DEFAULT_CANVAS_LIMIT) -> str:
    config = load_canvas_config()
    feed_url = config.get("feed_url")
    if not isinstance(feed_url, str) or not feed_url.strip():
        return "Canvas Calendar Feed 尚未配置。先在 .env 中设置 CANVAS_FEED_URL，或执行 /canvas set-feed <链接>。"

    try:
        normalized_days = normalize_canvas_days(days)
    except ValueError as exc:
        return str(exc)

    try:
        events = fetch_canvas_events(feed_url)
    except Exception as exc:
        return f"获取 Canvas 日程失败: {exc}"

    upcoming = select_upcoming_events(events, normalized_days, limit=limit)
    return format_canvas_schedule(upcoming, normalized_days)


def get_canvas_history_for_query(query: str, limit: int = 12, history_file: Path = CANVAS_HISTORY_FILE) -> str:
    ranges = resolve_temporal_ranges(query)
    if not ranges:
        return "没有识别到明确日期。可以试试 2026-03-31、3/31、3月31日、上周 这类写法。"

    history = load_canvas_history(history_file)
    history_entries = history.get("history_entries", [])
    if not isinstance(history_entries, list) or not history_entries:
        return "Canvas 历史缓存为空，暂时无法回看过去日期。先执行一次 /canvas 或询问 Canvas 日程，让系统抓取并缓存。"

    matched = search_canvas_history_by_ranges(ranges, history_entries, limit=limit)
    lines = [
        "【Canvas 历史记录】以下内容来自之前抓取并缓存的 Canvas calendar 数据。",
        "这些是历史日程，不代表它们是刚刚发生的。",
        format_temporal_ranges(ranges),
    ]
    if not matched:
        lines.append("- 在这些时间范围内，没有在 Canvas 历史缓存中找到匹配事件。")
        return "\n".join(line for line in lines if line)

    for item in matched:
        event = deserialize_canvas_event(item["event"])
        last_seen_at = item["last_seen_at"][:19].replace("T", " ") if item.get("last_seen_at") else "时间未知"
        lines.append(f"- {_format_event_line(event, datetime.now().astimezone())} | 最近确认时间: {last_seen_at}")
    return "\n".join(line for line in lines if line)


def mark_canvas_events_completed(query: str, history_file: Path = CANVAS_HISTORY_FILE) -> str:
    return _update_canvas_event_status(query, STATUS_COMPLETED, history_file)


def mark_canvas_events_pending(query: str, history_file: Path = CANVAS_HISTORY_FILE) -> str:
    return _update_canvas_event_status(query, STATUS_PENDING, history_file)


def _update_canvas_event_status(query: str, target_status: str, history_file: Path = CANVAS_HISTORY_FILE) -> str:
    history = load_canvas_history(history_file)
    history_entries = history.get("history_entries", [])
    if not isinstance(history_entries, list) or not history_entries:
        return "Canvas 历史缓存为空，暂时没有可标记的事件。先执行一次 /canvas。"

    matched = _find_canvas_entries_for_query(query, history_entries)
    if not matched:
        return "没有找到要标记完成的 Canvas 事件。请尽量带上课程名、作业名或日期。"

    if len(matched) > 1:
        lines = ["找到多个可能匹配的 Canvas 事件，暂时没有自动修改。请说得更具体一点，例如带上完整课程名、类型和编号："]
        for item in matched[:5]:
            event = deserialize_canvas_event(item["event"])
            lines.append(f"- {_format_event_line(event, datetime.now().astimezone())}")
        return "\n".join(lines)

    target = matched[0]
    matched_keys = {target.get("event_key")} if isinstance(target.get("event_key"), str) else set()
    updated_count = 0

    for entry in history_entries:
        if entry.get("event_key") not in matched_keys:
            continue
        raw_event = entry.get("event")
        if not isinstance(raw_event, dict):
            continue
        if _normalize_status(raw_event.get("status")) != target_status:
            updated_count += 1
        raw_event["status"] = target_status
        entry["event"] = raw_event

    latest_snapshot = history.get("latest_snapshot")
    if isinstance(latest_snapshot, dict):
        latest_events = latest_snapshot.get("events")
        if isinstance(latest_events, list):
            for raw_event in latest_events:
                if not isinstance(raw_event, dict):
                    continue
                if _event_storage_key(raw_event) in matched_keys:
                    raw_event["status"] = target_status

    _save_canvas_history(history, history_file)

    action_text = "已完成" if target_status == STATUS_COMPLETED else "未完成"
    lines = [f"已更新 {updated_count or len(matched_keys)} 个 Canvas 事件为{action_text}："]
    event = deserialize_canvas_event(target["event"])
    lines.append(f"- {_format_event_line(event, datetime.now().astimezone())}")
    return "\n".join(lines)


def fetch_canvas_events(feed_url: str) -> list[CanvasEvent]:
    response = httpx.get(feed_url, timeout=CANVAS_TIMEOUT, follow_redirects=True)
    response.raise_for_status()
    events = parse_canvas_ics(response.text)
    archive_canvas_snapshot(events)
    return events


def parse_canvas_ics(text: str) -> list[CanvasEvent]:
    lines = _unfold_ics_lines(text)
    local_tz = datetime.now().astimezone().tzinfo or timezone.utc
    events: list[CanvasEvent] = []
    current_type: str | None = None
    current_fields: dict[str, list[tuple[dict[str, str], str]]] | None = None

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        if line == "BEGIN:VEVENT":
            current_type = "VEVENT"
            current_fields = {}
            continue
        if line == "BEGIN:VTODO":
            current_type = "VTODO"
            current_fields = {}
            continue
        if line in {"END:VEVENT", "END:VTODO"}:
            if current_fields is not None:
                event = _build_canvas_event(current_fields, local_tz)
                if event is not None:
                    events.append(event)
            current_type = None
            current_fields = None
            continue
        if current_type is None or current_fields is None:
            continue

        parsed = _parse_ics_property(line)
        if parsed is None:
            continue
        name, params, value = parsed
        current_fields.setdefault(name, []).append((params, value))

    deduped: dict[str, CanvasEvent] = {}
    for event in events:
        deduped[event.uid] = event
    return list(deduped.values())


def load_canvas_history(history_file: Path = CANVAS_HISTORY_FILE) -> dict:
    prepare_storage_layout()
    if not history_file.exists():
        return _empty_canvas_history()
    try:
        data = json.loads(history_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _empty_canvas_history()
    if not isinstance(data, dict):
        return _empty_canvas_history()

    normalized, changed = _normalize_canvas_history_payload(data)
    if changed:
        _save_canvas_history(normalized, history_file)
    return normalized


def archive_canvas_snapshot(events: list[CanvasEvent], history_file: Path = CANVAS_HISTORY_FILE) -> None:
    history = load_canvas_history(history_file)
    fetched_at = datetime.now().astimezone().isoformat()
    snapshot = {
        "fetched_at": fetched_at,
        "event_count": len(events),
        "events": [serialize_canvas_event(event) for event in events],
    }
    payload = _merge_canvas_history(history, snapshot)
    _save_canvas_history(payload, history_file)


def search_canvas_history_by_ranges(ranges: list[dict[str, datetime]], history_entries: list[dict], limit: int = 12) -> list[dict]:
    matched: list[dict] = []
    for item in history_entries:
        raw_event = item.get("event")
        if not isinstance(raw_event, dict):
            continue
        event = deserialize_canvas_event(raw_event)
        anchor = event.anchor_at
        if anchor is None:
            continue
        if not any(entry["start"] <= anchor <= entry["end"] for entry in ranges):
            continue
        matched.append(item)

    fallback_dt = datetime.max.replace(tzinfo=datetime.now().astimezone().tzinfo)
    matched.sort(key=lambda item: deserialize_canvas_event(item["event"]).anchor_at or fallback_dt)
    return matched[:limit]


def _empty_canvas_history() -> dict:
    return {"last_fetched_at": None, "latest_snapshot": None, "history_entries": []}


def _normalize_canvas_history_payload(data: dict) -> tuple[dict, bool]:
    changed = False
    last_fetched_at = data.get("last_fetched_at") if isinstance(data.get("last_fetched_at"), str) else None

    latest_snapshot = data.get("latest_snapshot")
    if latest_snapshot is not None and not isinstance(latest_snapshot, dict):
        latest_snapshot = None
        changed = True

    normalized_entries: dict[str, dict] = {}
    history_entries = data.get("history_entries")
    if isinstance(history_entries, list):
        for raw_item in history_entries:
            if isinstance(raw_item, dict):
                raw_event = raw_item.get("event")
                if isinstance(raw_event, dict) and raw_event.get("status") is None:
                    changed = True
            entry = _normalize_history_entry(raw_item)
            if entry is None:
                changed = True
                continue
            key = entry["event_key"]
            if key in normalized_entries:
                changed = True
                normalized_entries[key] = _merge_history_entry(normalized_entries[key], entry)
            else:
                normalized_entries[key] = entry
    else:
        if "history_entries" in data:
            changed = True

    snapshots = data.get("snapshots")
    if isinstance(snapshots, list) and snapshots:
        changed = True
        for snapshot in snapshots:
            if not isinstance(snapshot, dict):
                continue
            latest_snapshot = _normalize_snapshot(snapshot) or latest_snapshot
            fetched_at = snapshot.get("fetched_at") if isinstance(snapshot.get("fetched_at"), str) else None
            events = snapshot.get("events")
            if not isinstance(events, list):
                continue
            for raw_event in events:
                if not isinstance(raw_event, dict):
                    continue
                event_key = _event_storage_key(raw_event)
                candidate = {
                    "event_key": event_key,
                    "first_seen_at": fetched_at,
                    "last_seen_at": fetched_at,
                    "seen_count": 1,
                    "event": raw_event,
                }
                entry = _normalize_history_entry(candidate)
                if entry is None:
                    continue
                if event_key in normalized_entries:
                    normalized_entries[event_key] = _merge_history_entry(normalized_entries[event_key], entry)
                else:
                    normalized_entries[event_key] = entry

    if latest_snapshot is None and isinstance(data.get("latest_snapshot"), dict):
        latest_snapshot = _normalize_snapshot(data.get("latest_snapshot"))
        changed = True
    elif isinstance(data.get("latest_snapshot"), dict):
        latest_events = data.get("latest_snapshot", {}).get("events")
        if isinstance(latest_events, list) and any(isinstance(item, dict) and item.get("status") is None for item in latest_events):
            changed = True

    if latest_snapshot is None and isinstance(snapshots, list) and snapshots:
        latest_snapshot = _normalize_snapshot(snapshots[-1])

    entries = list(normalized_entries.values())
    entries.sort(key=lambda item: _history_entry_sort_key(item))
    normalized = {
        "last_fetched_at": last_fetched_at,
        "latest_snapshot": latest_snapshot,
        "history_entries": entries,
    }
    return normalized, changed


def _merge_canvas_history(history: dict, snapshot: dict) -> dict:
    normalized, _ = _normalize_canvas_history_payload(history)
    fetched_at = snapshot.get("fetched_at") if isinstance(snapshot.get("fetched_at"), str) else None
    events = snapshot.get("events") if isinstance(snapshot.get("events"), list) else []

    entries_by_key = {item["event_key"]: item for item in normalized.get("history_entries", []) if isinstance(item, dict) and isinstance(item.get("event_key"), str)}
    updated_snapshot_events: list[dict] = []
    for raw_event in events:
        if not isinstance(raw_event, dict):
            continue
        normalized_event = _normalize_event_payload(raw_event)
        event_key = _event_storage_key(normalized_event)
        if event_key in entries_by_key:
            entry = entries_by_key[event_key]
            current_status = _normalize_status((entry.get("event") or {}).get("status"))
            normalized_event["status"] = current_status
            entry["last_seen_at"] = fetched_at or entry.get("last_seen_at")
            entry["seen_count"] = int(entry.get("seen_count", 1)) + 1
            entry["event"] = normalized_event
        else:
            normalized_event["status"] = _normalize_status(normalized_event.get("status"))
            entries_by_key[event_key] = {
                "event_key": event_key,
                "first_seen_at": fetched_at,
                "last_seen_at": fetched_at,
                "seen_count": 1,
                "event": normalized_event,
            }
        updated_snapshot_events.append(normalized_event)

    entries = list(entries_by_key.values())
    entries.sort(key=lambda item: _history_entry_sort_key(item))
    return {
        "last_fetched_at": fetched_at,
        "latest_snapshot": {
            "fetched_at": fetched_at,
            "event_count": len(updated_snapshot_events),
            "events": updated_snapshot_events,
        },
        "history_entries": entries,
    }


def _normalize_snapshot(snapshot: Any) -> dict | None:
    if not isinstance(snapshot, dict):
        return None
    fetched_at = snapshot.get("fetched_at") if isinstance(snapshot.get("fetched_at"), str) else None
    events = snapshot.get("events")
    if not isinstance(events, list):
        return None
    normalized_events = [_normalize_event_payload(item) for item in events if isinstance(item, dict)]
    return {
        "fetched_at": fetched_at,
        "event_count": len(normalized_events),
        "events": normalized_events,
    }


def _normalize_history_entry(raw_item: Any) -> dict | None:
    if not isinstance(raw_item, dict):
        return None
    raw_event = raw_item.get("event")
    if not isinstance(raw_event, dict):
        return None
    raw_event = _normalize_event_payload(raw_event)
    event_key = raw_item.get("event_key") if isinstance(raw_item.get("event_key"), str) and raw_item.get("event_key") else _event_storage_key(raw_event)
    first_seen_at = raw_item.get("first_seen_at") if isinstance(raw_item.get("first_seen_at"), str) else raw_item.get("last_seen_at")
    last_seen_at = raw_item.get("last_seen_at") if isinstance(raw_item.get("last_seen_at"), str) else first_seen_at
    seen_count = raw_item.get("seen_count") if isinstance(raw_item.get("seen_count"), int) and raw_item.get("seen_count") > 0 else 1
    return {
        "event_key": event_key,
        "first_seen_at": first_seen_at,
        "last_seen_at": last_seen_at,
        "seen_count": seen_count,
        "event": raw_event,
    }


def _merge_history_entry(left: dict, right: dict) -> dict:
    right_event = right.get("event") if isinstance(right.get("event"), dict) else None
    left_event = left.get("event") if isinstance(left.get("event"), dict) else None
    merged_event = _normalize_event_payload(right_event or left_event or {})
    if left_event and _normalize_status(left_event.get("status")) == STATUS_COMPLETED:
        merged_event["status"] = STATUS_COMPLETED
    return {
        "event_key": left.get("event_key") or right.get("event_key"),
        "first_seen_at": _min_iso(left.get("first_seen_at"), right.get("first_seen_at")),
        "last_seen_at": _max_iso(left.get("last_seen_at"), right.get("last_seen_at")),
        "seen_count": int(left.get("seen_count", 1)) + int(right.get("seen_count", 1)),
        "event": merged_event,
    }


def _event_storage_key(raw_event: dict) -> str:
    uid = str(raw_event.get("uid", "canvas-event"))
    anchor = raw_event.get("due_at") or raw_event.get("starts_at") or raw_event.get("ends_at") or "no-anchor"
    title = str(raw_event.get("title", ""))
    return f"{uid}|{anchor}|{title}"


def _normalize_event_payload(raw_event: dict) -> dict:
    payload = dict(raw_event)
    payload["status"] = _normalize_status(payload.get("status"))
    return payload


def _normalize_status(value: Any) -> str:
    if isinstance(value, str) and value.strip().lower() == STATUS_COMPLETED:
        return STATUS_COMPLETED
    return STATUS_PENDING


def _history_entry_sort_key(item: dict) -> tuple[datetime, str]:
    raw_event = item.get("event") if isinstance(item.get("event"), dict) else {}
    event = deserialize_canvas_event(raw_event)
    fallback_dt = datetime.max.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return event.anchor_at or fallback_dt, str(raw_event.get("title", ""))


def _find_canvas_entries_for_query(query: str, history_entries: list[dict], limit: int = 5) -> list[dict]:
    ranges = resolve_temporal_ranges(query)
    query_tokens = _tokenize_text(query)
    query_numbers = _extract_number_tokens(query)
    query_course_numbers = _extract_course_numbers(query)
    query_kind_numbers = _extract_kind_number_pairs(query)
    query_kind_hints = _extract_kind_hints(query)
    scored: list[tuple[float, dict]] = []

    for entry in history_entries:
        raw_event = entry.get("event")
        if not isinstance(raw_event, dict):
            continue
        event = deserialize_canvas_event(raw_event)
        title_tokens = _tokenize_text(event.title)
        desc_tokens = _tokenize_text(event.description)
        event_numbers = _extract_number_tokens(f"{event.title} {event.description}")
        event_course_numbers = _extract_course_numbers(event.title)
        event_kind_numbers = _extract_kind_number_pairs(event.title)
        event_kind_hints = _extract_kind_hints(f"{event.title} {event.description}") | {_normalize_kind_hint(event.kind)}
        score = 0.0

        if query_course_numbers and not query_course_numbers.issubset(event_course_numbers):
            continue
        if query_kind_numbers and not query_kind_numbers.issubset(event_kind_numbers):
            continue
        if query_kind_hints and not query_kind_numbers and not (query_kind_hints & event_kind_hints):
            continue
        if query_numbers and not (query_numbers & event_numbers):
            continue

        if ranges:
            anchor = event.anchor_at
            if anchor and any(item["start"] <= anchor <= item["end"] for item in ranges):
                score += 6.0

        score += 4.0 * len(query_course_numbers & event_course_numbers)
        score += 5.0 * len(query_kind_numbers & event_kind_numbers)
        score += 1.5 * len(query_kind_hints & event_kind_hints)

        score += sum(2.0 for token in query_tokens if token in title_tokens)
        score += sum(0.5 for token in query_tokens if token in desc_tokens)

        title_lower = event.title.lower()
        query_lower = query.lower()
        if event.title and event.title.lower() in query_lower:
            score += 8.0
        if _normalize_status(raw_event.get("status")) == STATUS_PENDING:
            score += 0.3

        if score > 0:
            scored.append((score, entry))

    scored.sort(key=lambda item: item[0], reverse=True)
    if not scored:
        return []

    top_score = scored[0][0]
    threshold = max(2.0, top_score - 2.0)
    matched = [entry for score, entry in scored if score >= threshold]
    return matched[:limit]


def _tokenize_text(text: str) -> set[str]:
    lowered = text.lower()
    ascii_tokens = re.findall(r"[a-z0-9_]{2,}", lowered)
    cjk_chars = [ch for ch in lowered if "\u4e00" <= ch <= "\u9fff"]
    cjk_bigrams = [cjk_chars[i] + cjk_chars[i + 1] for i in range(len(cjk_chars) - 1)]
    return set(ascii_tokens + cjk_bigrams)


def _extract_number_tokens(text: str) -> set[str]:
    return set(re.findall(r"\d{1,4}", text))


def _extract_course_numbers(text: str) -> set[str]:
    return set(re.findall(r"(?<!\d)(\d{3})(?!\d)", text))


def _extract_kind_hints(text: str) -> set[str]:
    lowered = text.lower()
    hints: set[str] = set()
    for kind, patterns in _kind_pattern_groups().items():
        if any(pattern in lowered for pattern in patterns):
            hints.add(kind)
    return hints


def _extract_kind_number_pairs(text: str) -> set[tuple[str, str]]:
    lowered = text.lower()
    pairs: set[tuple[str, str]] = set()
    for kind, patterns in _kind_pattern_groups().items():
        for pattern in patterns:
            escaped = re.escape(pattern)
            regexes = [rf"{escaped}[\s_\-]*?(\d{{1,3}})"]
            for regex in regexes:
                for match in re.finditer(regex, lowered):
                    pairs.add((kind, match.group(1)))
    return pairs


def _kind_pattern_groups() -> dict[str, list[str]]:
    return {
        "lab": ["lab", "实验"],
        "journal": ["journal", "日志", "周记"],
        "assignment": ["assignment", "作业"],
        "test": ["term test", "mid-term", "midterm", "test", "quiz", "考试", "测验"],
        "project": ["project", "项目"],
    }


def _normalize_kind_hint(kind: str) -> str:
    if kind == "assignment":
        return "assignment"
    if kind == "exam":
        return "test"
    return kind


def _save_canvas_history(payload: dict, history_file: Path) -> None:
    history_file.parent.mkdir(parents=True, exist_ok=True)
    history_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _min_iso(left: Any, right: Any) -> str | None:
    left_dt = _parse_iso_datetime(left)
    right_dt = _parse_iso_datetime(right)
    if left_dt and right_dt:
        return min(left_dt, right_dt).isoformat()
    if left_dt:
        return left_dt.isoformat()
    if right_dt:
        return right_dt.isoformat()
    return None


def _max_iso(left: Any, right: Any) -> str | None:
    left_dt = _parse_iso_datetime(left)
    right_dt = _parse_iso_datetime(right)
    if left_dt and right_dt:
        return max(left_dt, right_dt).isoformat()
    if left_dt:
        return left_dt.isoformat()
    if right_dt:
        return right_dt.isoformat()
    return None


def select_upcoming_events(events: list[CanvasEvent], days: int, limit: int = DEFAULT_CANVAS_LIMIT) -> list[CanvasEvent]:
    now = datetime.now().astimezone()
    window_end = now + timedelta(days=days)
    selected: list[CanvasEvent] = []
    for event in events:
        anchor = event.anchor_at
        if anchor is None:
            continue
        if anchor < now - timedelta(hours=6):
            continue
        if anchor > window_end:
            continue
        selected.append(event)
    selected.sort(key=lambda item: (item.anchor_at or window_end, item.title.lower()))
    return selected[:limit]


def format_canvas_schedule(events: list[CanvasEvent], days: int) -> str:
    if not events:
        return f"Canvas 未来 {days} 天内没有查到新的作业、考试或日程。"

    now = datetime.now().astimezone()
    urgent = [event for event in events if _is_urgent(event, now)]
    lines = [
        f"当前时间: {now.strftime('%Y-%m-%d')} {WEEKDAYS[now.weekday()]} {now.strftime('%H:%M')} ({now.tzname() or '本地时区'})",
        f"Canvas 未来 {days} 天内共 {len(events)} 个安排。",
        "下面每条都带相对日期标签，优先按这些明确日期理解，不要自己改写时间关系。",
    ]
    if urgent:
        lines.append("优先提醒：")
        for event in urgent[:5]:
            lines.append(f"- {_format_event_line(event, now, emphasize=True)}")

    lines.append("时间线：")
    for event in events:
        lines.append(f"- {_format_event_line(event, now)}")
    return "\n".join(lines)


def serialize_canvas_event(event: CanvasEvent) -> dict:
    return {
        "uid": event.uid,
        "title": event.title,
        "starts_at": event.starts_at.isoformat() if event.starts_at else None,
        "ends_at": event.ends_at.isoformat() if event.ends_at else None,
        "due_at": event.due_at.isoformat() if event.due_at else None,
        "starts_all_day": event.starts_all_day,
        "due_all_day": event.due_all_day,
        "description": event.description,
        "location": event.location,
        "kind": event.kind,
        "status": _normalize_status(event.status),
    }


def deserialize_canvas_event(data: dict) -> CanvasEvent:
    return CanvasEvent(
        uid=str(data.get("uid", "canvas-event")),
        title=str(data.get("title", "未命名事件")),
        starts_at=_parse_iso_datetime(data.get("starts_at")),
        ends_at=_parse_iso_datetime(data.get("ends_at")),
        due_at=_parse_iso_datetime(data.get("due_at")),
        starts_all_day=bool(data.get("starts_all_day")),
        due_all_day=bool(data.get("due_all_day")),
        description=str(data.get("description", "")),
        location=str(data.get("location", "")),
        kind=str(data.get("kind", "event")),
        status=_normalize_status(data.get("status")),
    )


def _mask_feed_url(feed_url: str) -> str:
    parsed = urlparse(feed_url)
    if not parsed.netloc:
        return "已配置（链接已隐藏）"
    tail = feed_url[-8:] if len(feed_url) >= 8 else feed_url
    return f"{parsed.scheme}://{parsed.netloc}/...{tail}"


def _load_env_canvas_feed_url() -> str | None:
    value = os.environ.get(ENV_CANVAS_FEED_URL)
    if isinstance(value, str) and value.strip():
        return value.strip()

    env_file = ROOT_DIR / ".env"
    if not env_file.exists():
        return None
    try:
        for raw_line in env_file.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, raw_value = line.split("=", 1)
            if key.strip() != ENV_CANVAS_FEED_URL:
                continue
            cleaned = raw_value.strip().strip('"').strip("'")
            return cleaned or None
    except OSError:
        return None
    return None


def _save_canvas_feed_url_to_env(feed_url: str) -> None:
    env_file = ROOT_DIR / ".env"
    lines: list[str] = []
    replaced = False
    if env_file.exists():
        try:
            lines = env_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []

    new_line = f'{ENV_CANVAS_FEED_URL}="{feed_url}"'
    updated: list[str] = []
    for raw_line in lines:
        if raw_line.strip().startswith(f"{ENV_CANVAS_FEED_URL}="):
            updated.append(new_line)
            replaced = True
        else:
            updated.append(raw_line)

    if not replaced:
        if updated and updated[-1] != "":
            updated.append("")
        if not updated:
            updated.append("# Canvas Calendar Feed 链接")
            updated.append('# 例子：CANVAS_FEED_URL="https://canvas.example.edu/feeds/calendars/user_xxx.ics"')
        updated.append(new_line)
    env_file.write_text("\n".join(updated) + "\n", encoding="utf-8")


def _remove_canvas_feed_url_from_env() -> bool:
    env_file = ROOT_DIR / ".env"
    if not env_file.exists():
        return False
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False

    updated = [line for line in lines if not line.strip().startswith(f"{ENV_CANVAS_FEED_URL}=")]
    if len(updated) == len(lines):
        return False
    env_file.write_text("\n".join(updated).rstrip() + "\n", encoding="utf-8")
    return True


def _unfold_ics_lines(text: str) -> list[str]:
    result: list[str] = []
    for line in text.splitlines():
        if (line.startswith(" ") or line.startswith("\t")) and result:
            result[-1] += line[1:]
        else:
            result.append(line)
    return result


def _parse_ics_property(line: str) -> tuple[str, dict[str, str], str] | None:
    if ":" not in line:
        return None
    head, value = line.split(":", 1)
    parts = head.split(";")
    name = parts[0].upper()
    params: dict[str, str] = {}
    for part in parts[1:]:
        if "=" not in part:
            continue
        key, param_value = part.split("=", 1)
        params[key.upper()] = param_value.strip('"')
    return name, params, value.strip()


def _build_canvas_event(fields: dict[str, list[tuple[dict[str, str], str]]], local_tz) -> CanvasEvent | None:
    title = _get_first_value(fields, "SUMMARY") or "未命名事件"
    uid = _get_first_value(fields, "UID") or f"canvas-{abs(hash(title))}"
    description = _clean_ics_text(_get_first_value(fields, "DESCRIPTION") or "")
    location = _clean_ics_text(_get_first_value(fields, "LOCATION") or "")
    starts_at, starts_all_day = _get_first_datetime(fields, "DTSTART", local_tz)
    ends_at, _ = _get_first_datetime(fields, "DTEND", local_tz)
    due_at, due_all_day = _get_first_datetime(fields, "DUE", local_tz)
    kind = _classify_canvas_event(title, description)

    if starts_at is None and due_at is None and ends_at is None:
        return None
    return CanvasEvent(
        uid=uid,
        title=title,
        starts_at=starts_at,
        ends_at=ends_at,
        due_at=due_at,
        starts_all_day=starts_all_day,
        due_all_day=due_all_day,
        description=description,
        location=location,
        kind=kind,
    )


def _get_first_value(fields: dict[str, list[tuple[dict[str, str], str]]], name: str) -> str | None:
    values = fields.get(name)
    if not values:
        return None
    return values[0][1]


def _get_first_datetime(fields: dict[str, list[tuple[dict[str, str], str]]], name: str, local_tz) -> tuple[datetime | None, bool]:
    values = fields.get(name)
    if not values:
        return None, False
    params, raw_value = values[0]
    return _parse_ics_datetime(raw_value, params, local_tz)


def _parse_ics_datetime(value: str, params: dict[str, str], local_tz) -> tuple[datetime | None, bool]:
    cleaned = value.strip()
    if not cleaned:
        return None, False

    is_all_day = params.get("VALUE", "").upper() == "DATE" or bool(re.fullmatch(r"\d{8}", cleaned))
    if is_all_day:
        dt = datetime.strptime(cleaned[:8], "%Y%m%d")
        return dt.replace(tzinfo=local_tz), True

    tzid = params.get("TZID")
    fmt_candidates = ["%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S", "%Y%m%dT%H%M"]
    parsed_dt: datetime | None = None
    used_format: str | None = None
    for fmt in fmt_candidates:
        try:
            parsed_dt = datetime.strptime(cleaned, fmt)
            used_format = fmt
            break
        except ValueError:
            continue
    if parsed_dt is None:
        return None, False

    if used_format and used_format.endswith("Z"):
        parsed_dt = parsed_dt.replace(tzinfo=timezone.utc)
    elif tzid:
        try:
            parsed_dt = parsed_dt.replace(tzinfo=ZoneInfo(tzid))
        except Exception:
            parsed_dt = parsed_dt.replace(tzinfo=local_tz)
    else:
        parsed_dt = parsed_dt.replace(tzinfo=local_tz)
    return parsed_dt.astimezone(local_tz), False


def _clean_ics_text(text: str) -> str:
    return text.replace("\\n", "\n").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\").strip()


def _classify_canvas_event(title: str, description: str) -> str:
    haystack = f"{title}\n{description}".lower()
    if any(keyword in haystack for keyword in _EXAM_HINTS):
        return "exam"
    if any(keyword in haystack for keyword in _ASSIGNMENT_HINTS):
        return "assignment"
    return "event"


def _is_urgent(event: CanvasEvent, now: datetime) -> bool:
    anchor = event.anchor_at
    if anchor is None:
        return False
    if anchor <= now + timedelta(days=3):
        return True
    return event.kind in {"assignment", "exam"} and anchor <= now + timedelta(days=7)


def _format_event_line(event: CanvasEvent, now: datetime, emphasize: bool = False) -> str:
    anchor = event.anchor_at
    if anchor is None:
        return event.title

    kind_label = {"assignment": "作业", "exam": "考试", "event": "日程"}.get(event.kind, "日程")
    time_label = _format_event_time(event)
    delta_label = _format_delta(anchor - now)
    relative_label = _relative_day_label(anchor, now)
    suffix_parts = [delta_label]
    if event.location:
        suffix_parts.append(f"地点: {event.location}")

    status_suffix = "（已完成）" if event.status == STATUS_COMPLETED else ""
    line = f"[{kind_label}] {relative_label} | {time_label} | {event.title}{status_suffix}"
    if suffix_parts:
        line += " | " + " | ".join(part for part in suffix_parts if part)
    if emphasize and anchor <= now + timedelta(hours=24):
        line = "紧急: " + line
    return line


def _format_event_time(event: CanvasEvent) -> str:
    anchor = event.anchor_at
    if anchor is None:
        return "时间未知"
    day_text = anchor.strftime("%m-%d")
    weekday = WEEKDAYS[anchor.weekday()]
    prefix = "due " if event.due_at is not None else ""
    if event.is_all_day:
        return f"{prefix}{day_text} {weekday}"
    return f"{prefix}{day_text} {weekday} {anchor.strftime('%H:%M')}"


def _format_delta(delta: timedelta) -> str:
    total_seconds = int(delta.total_seconds())
    if total_seconds < 0:
        overdue_seconds = abs(total_seconds)
        overdue_days, remainder = divmod(overdue_seconds, 86400)
        overdue_hours = remainder // 3600
        if overdue_days > 0:
            return f"已过去 {overdue_days} 天 {overdue_hours} 小时"
        if overdue_hours > 0:
            return f"已过去 {overdue_hours} 小时"
        return "已过去不到 1 小时"
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    if days > 0:
        return f"还有 {days} 天 {hours} 小时"
    if hours > 0:
        return f"还有 {hours} 小时 {minutes} 分"
    return f"还有 {max(minutes, 1)} 分钟"


def _relative_day_label(anchor: datetime, now: datetime) -> str:
    day_delta = (anchor.date() - now.date()).days
    if day_delta == 0:
        return "今天"
    if day_delta == 1:
        return "明天"
    if day_delta == 2:
        return "后天"
    if day_delta == -1:
        return "昨天"
    if day_delta < -1:
        return f"{abs(day_delta)} 天前"
    return f"{day_delta} 天后"


def _parse_iso_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
