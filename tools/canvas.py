"""Canvas Calendar Feed 工具。

通过 Canvas 提供的 calendar feed (ICS) 获取近期安排，侧重提醒：
- 临近 due 的作业
- quiz / exam / test
- 未来几天的重要课程日程
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CANVAS_CONFIG_FILE = DATA_DIR / "canvas_config.json"

DEFAULT_CANVAS_DAYS = 14
MAX_CANVAS_DAYS = 60
DEFAULT_CANVAS_LIMIT = 12
CANVAS_TIMEOUT = 15
ENV_CANVAS_FEED_URL = "CANVAS_FEED_URL"

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
_ASSIGNMENT_HINTS = [
    "assignment",
    "homework",
    "submission",
    "project",
    "essay",
    "due",
    "作业",
    "截止",
    "ddl",
]
_EXAM_HINTS = [
    "exam",
    "quiz",
    "test",
    "midterm",
    "final",
    "测验",
    "考试",
    "期中",
    "期末",
]


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

    @property
    def anchor_at(self) -> datetime | None:
        return self.due_at or self.starts_at or self.ends_at

    @property
    def is_all_day(self) -> bool:
        if self.due_at is not None:
            return self.due_all_day
        return self.starts_all_day


def load_canvas_config(config_file: Path = CANVAS_CONFIG_FILE) -> dict:
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
        return (
            "Canvas Calendar Feed 尚未配置。"
            "可在 .env 中设置 CANVAS_FEED_URL，或使用 /canvas set-feed <链接>。"
        )
    source = "环境变量" if config.get("source") == "env" else "本地配置文件"
    return f"Canvas Calendar Feed 已配置（来源: {source}）：{_mask_feed_url(feed_url)}"


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


def fetch_canvas_events(feed_url: str) -> list[CanvasEvent]:
    response = httpx.get(feed_url, timeout=CANVAS_TIMEOUT, follow_redirects=True)
    response.raise_for_status()
    return parse_canvas_ics(response.text)


def parse_canvas_ics(text: str) -> list[CanvasEvent]:
    lines = _unfold_ics_lines(text)
    local_tz = datetime.now().astimezone().tzinfo
    if local_tz is None:
        local_tz = timezone.utc

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

    lines = [f"Canvas 未来 {days} 天内共 {len(events)} 个安排。"]
    if urgent:
        lines.append("优先提醒：")
        for event in urgent[:5]:
            lines.append(f"- {_format_event_line(event, now, emphasize=True)}")

    lines.append("时间线：")
    for event in events:
        lines.append(f"- {_format_event_line(event, now)}")
    return "\n".join(lines)


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

    env_file = DATA_DIR.parent / ".env"
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
    env_file = DATA_DIR.parent / ".env"
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
        stripped = raw_line.strip()
        if stripped.startswith(f"{ENV_CANVAS_FEED_URL}="):
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
    env_file = DATA_DIR.parent / ".env"
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
    return (
        text.replace("\\n", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
        .strip()
    )


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

    kind_label = {
        "assignment": "作业",
        "exam": "考试",
        "event": "日程",
    }.get(event.kind, "日程")
    time_label = _format_event_time(event)
    delta_label = _format_delta(anchor - now)
    suffix_parts = [delta_label]
    if event.location:
        suffix_parts.append(f"地点: {event.location}")

    line = f"[{kind_label}] {time_label} | {event.title}"
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
        return "已过期"
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    if days > 0:
        return f"还有 {days} 天 {hours} 小时"
    if hours > 0:
        return f"还有 {hours} 小时 {minutes} 分"
    return f"还有 {max(minutes, 1)} 分钟"
