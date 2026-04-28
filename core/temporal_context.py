from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

WEEKDAYS = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]

_ABSOLUTE_PATTERNS = [
    re.compile(r"(?P<year>20\d{2})[-/](?P<month>\d{1,2})[-/](?P<day>\d{1,2})"),
    re.compile(r"(?P<year>20\d{2})年(?P<month>\d{1,2})月(?P<day>\d{1,2})日?"),
    re.compile(r"(?<!\d)(?P<month>\d{1,2})/(?P<day>\d{1,2})(?!\d)"),
    re.compile(r"(?<!\d)(?P<month>\d{1,2})月(?P<day>\d{1,2})日?"),
]
_COMPACT_MMDD_RE = re.compile(r"(?<!\d)(\d{3,4})(?!\d)")
_PAST_HINTS = ["之前", "过去", "当时", "那天", "上周", "上个月", "昨天", "前天"]
_COMPACT_DATE_HINTS = ["考试", "exam", "quiz", "作业", "due", "ddl", "canvas", "日程", "记录", "聊天", "讨论", "那天"]


def build_runtime_context(model_name: str | None = None) -> str:
    now = datetime.now().astimezone()
    lines = [
        "【runtime context】",
        f"- 当前日期: {now.strftime('%Y年%m月%d日')} {WEEKDAYS[now.weekday()]}",
        f"- 当前时间: {now.strftime('%H:%M:%S')}",
        f"- 当前时区: {now.tzname() or '本地时区'}",
    ]
    if model_name:
        lines.append(f"- 当前模型: {model_name}")
    lines.append("- 如果用户问过去的事情，必须区分它是历史记录还是当前对话，不要把上周或更早的记录说成刚刚发生。")
    return "\n".join(lines)


def query_mentions_past_time(query: str, now: datetime | None = None) -> bool:
    if any(hint in query for hint in _PAST_HINTS):
        return True
    ranges = resolve_temporal_ranges(query, now=now)
    if not ranges:
        return False
    current_date = (now or datetime.now().astimezone()).date()
    return any(item["end"].date() < current_date for item in ranges)


def resolve_temporal_ranges(query: str, now: datetime | None = None) -> list[dict[str, datetime]]:
    current = now or datetime.now().astimezone()
    ranges: list[dict[str, datetime]] = []
    seen: set[tuple[str, str]] = set()

    def add_range(label: str, start_dt: datetime, end_dt: datetime):
        key = (start_dt.isoformat(), end_dt.isoformat())
        if key in seen:
            return
        seen.add(key)
        ranges.append({"label": label, "start": start_dt, "end": end_dt})

    for pattern in _ABSOLUTE_PATTERNS:
        for match in pattern.finditer(query):
            groups = match.groupdict()
            year = int(groups.get("year") or current.year)
            month = int(groups["month"])
            day = int(groups["day"])
            target = _safe_date(year, month, day)
            if target is None:
                continue
            start_dt, end_dt = _date_range(target, current)
            add_range(f"{target.isoformat()}", start_dt, end_dt)

    if any(hint in query.lower() for hint in _COMPACT_DATE_HINTS):
        for token in _COMPACT_MMDD_RE.findall(query):
            target = _parse_compact_mmdd(token, current.year)
            if target is None:
                continue
            start_dt, end_dt = _date_range(target, current)
            add_range(f"{target.isoformat()}", start_dt, end_dt)

    if "今天" in query:
        start_dt, end_dt = _date_range(current.date(), current)
        add_range("今天", start_dt, end_dt)
    if "昨天" in query:
        start_dt, end_dt = _date_range(current.date() - timedelta(days=1), current)
        add_range("昨天", start_dt, end_dt)
    if "前天" in query:
        start_dt, end_dt = _date_range(current.date() - timedelta(days=2), current)
        add_range("前天", start_dt, end_dt)
    if "明天" in query:
        start_dt, end_dt = _date_range(current.date() + timedelta(days=1), current)
        add_range("明天", start_dt, end_dt)
    if "后天" in query:
        start_dt, end_dt = _date_range(current.date() + timedelta(days=2), current)
        add_range("后天", start_dt, end_dt)

    if "这周" in query:
        add_range("这周", *_week_range(current, 0))
    if "上周" in query:
        add_range("上周", *_week_range(current, -1))
    if "下周" in query:
        add_range("下周", *_week_range(current, 1))

    if "这个月" in query or "本月" in query:
        add_range("这个月", *_month_range(current, 0))
    if "上个月" in query:
        add_range("上个月", *_month_range(current, -1))
    if "下个月" in query:
        add_range("下个月", *_month_range(current, 1))

    ranges.sort(key=lambda item: item["start"])
    return ranges


def format_temporal_ranges(ranges: list[dict[str, datetime]]) -> str:
    if not ranges:
        return ""
    lines = ["【时间查询范围】"]
    for item in ranges:
        lines.append(
            f"- {item['label']}: {item['start'].strftime('%Y-%m-%d %H:%M')} 到 {item['end'].strftime('%Y-%m-%d %H:%M')}"
        )
    return "\n".join(lines)


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _parse_compact_mmdd(token: str, year: int) -> date | None:
    if len(token) == 3:
        month = int(token[0])
        day = int(token[1:])
    elif len(token) == 4:
        month = int(token[:2])
        day = int(token[2:])
    else:
        return None
    return _safe_date(year, month, day)


def _date_range(target: date, now: datetime) -> tuple[datetime, datetime]:
    tz = now.tzinfo
    start_dt = datetime.combine(target, time.min, tzinfo=tz)
    end_dt = datetime.combine(target, time.max.replace(microsecond=0), tzinfo=tz)
    return start_dt, end_dt


def _week_range(now: datetime, week_offset: int) -> tuple[datetime, datetime]:
    monday = now.date() - timedelta(days=now.weekday()) + timedelta(days=week_offset * 7)
    sunday = monday + timedelta(days=6)
    return _date_range(monday, now)[0], _date_range(sunday, now)[1]


def _month_range(now: datetime, month_offset: int) -> tuple[datetime, datetime]:
    year = now.year
    month = now.month + month_offset
    while month <= 0:
        month += 12
        year -= 1
    while month > 12:
        month -= 12
        year += 1
    start_date = date(year, month, 1)
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    end_date = next_month - timedelta(days=1)
    return _date_range(start_date, now)[0], _date_range(end_date, now)[1]
