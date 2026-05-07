"""Open files from recent local search results.

This tool is intentionally small: it resolves user references like "第2个" or
"journal3" against recently shown file paths, then opens the selected path with
macOS `open`.
"""

from __future__ import annotations

import difflib
import re
from pathlib import Path

from tools.shell_control import run


_PATH_LINE_RE = re.compile(r"^\s*(?:[-*]\s*)?(\/Users\/.+?)\s*$")
_OPEN_PREFIX_RE = re.compile(r"^(?:帮我|请|麻烦)?\s*(?:打开|开启|open)\s*", re.I)
_ORDINAL_RE = re.compile(r"(?:第\s*)?(\d+)\s*(?:个|项|条|份)?$")
_CHINESE_NUMBERS = {
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}


def extract_paths_from_text(text: str) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        match = _PATH_LINE_RE.match(line)
        if not match:
            continue
        path = match.group(1).strip()
        if path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


def infer_open_request(text: str) -> str | None:
    stripped = text.strip()
    match = _OPEN_PREFIX_RE.match(stripped)
    if not match:
        return None
    query = stripped[match.end():].strip()
    query = query.strip("'\"`“”‘’")
    query = re.sub(r"(这个|那个|文件|文档)$", "", query).strip()
    return query or ""


def _chinese_ordinal(text: str) -> int | None:
    match = re.fullmatch(r"第?\s*([一二两三四五六七八九十])\s*(?:个|项|条|份)?", text)
    if not match:
        return None
    return _CHINESE_NUMBERS.get(match.group(1))


def _ordinal(text: str) -> int | None:
    match = _ORDINAL_RE.fullmatch(text.strip())
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            return None
    return _chinese_ordinal(text)


def _normalize_name(value: str) -> str:
    value = Path(value).stem if "/" in value else value
    value = value.lower()
    value = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", value)
    return value


def _score(query: str, path: str) -> float:
    normalized_query = _normalize_name(query)
    basename = Path(path).name
    normalized_name = _normalize_name(basename)
    normalized_stem = _normalize_name(Path(path).stem)

    if not normalized_query:
        return 0.0
    if normalized_query == normalized_name or normalized_query == normalized_stem:
        return 1.0
    if normalized_query in normalized_name or normalized_query in normalized_stem:
        return 0.92
    if normalized_stem in normalized_query:
        return 0.9
    return max(
        difflib.SequenceMatcher(None, normalized_query, normalized_name).ratio(),
        difflib.SequenceMatcher(None, normalized_query, normalized_stem).ratio(),
    )


def resolve_recent_path(reference: str, recent_paths: list[str]) -> tuple[str | None, str]:
    existing = [path for path in recent_paths if Path(path).exists()]
    if not existing:
        return None, "最近没有可打开的本机文件结果。请先搜索或列出文件。"

    index = _ordinal(reference)
    if index is not None:
        if 1 <= index <= len(existing):
            return existing[index - 1], ""
        return None, f"最近结果只有 {len(existing)} 个，找不到第 {index} 个。"

    if not reference.strip():
        if len(existing) == 1:
            return existing[0], ""
        return None, "请说明要打开哪一个，例如“打开第2个”或“打开 journal3”。"

    ranked = sorted(((_score(reference, path), path) for path in existing), reverse=True)
    best_score, best_path = ranked[0]
    if best_score >= 0.62:
        return best_path, ""

    preview = "\n".join(f"  {i + 1}. {Path(path).name}" for i, path in enumerate(existing[:8]))
    return None, f"没有在最近结果中匹配到“{reference}”。最近可选文件：\n{preview}"


def open_path(path: str) -> str:
    target = Path(path).expanduser()
    if not target.exists():
        return f"文件不存在，无法打开: {target}"
    result = run(["open", str(target)], timeout=10)
    if result.ok:
        return f"已打开: {target}"
    return f"打开失败: {result.stderr.strip() or result.error or result.exit_code}"


def open_recent(reference: str, recent_paths: list[str]) -> tuple[str, str | None]:
    path, error = resolve_recent_path(reference, recent_paths)
    if not path:
        return error, None
    return open_path(path), path
