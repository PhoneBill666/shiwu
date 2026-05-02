"""Directory map context for the local assistant.

The map is user-maintained Markdown. It helps the model understand the user's
computer layout, but exact file existence should still be checked with tools.
"""

from __future__ import annotations

from pathlib import Path

from storage_paths import ROOT_DIR


DIRECTORY_MAP_FILE = ROOT_DIR / "COMPUTER_DIRECTORY_MAP.md"
MAX_FULL_CHARS = 14000
MAX_COMPACT_CHARS = 3500

DIRECTORY_QUERY_HINTS = (
    "路径",
    "目录",
    "文件夹",
    "文件",
    "在哪",
    "哪里",
    "找",
    "查找",
    "搜索",
    "整理",
    "移动",
    "课程",
    "作业",
    "讲义",
    "资料",
    "项目",
    "folder",
    "file",
    "path",
    "directory",
)


def _read_directory_map() -> str:
    if not DIRECTORY_MAP_FILE.exists():
        return ""
    try:
        return DIRECTORY_MAP_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n\n...[目录地图已截断]..."


def _is_directory_related_query(user_query: str) -> bool:
    lowered = user_query.lower()
    return any(hint in user_query or hint in lowered for hint in DIRECTORY_QUERY_HINTS)


def _compact_map(markdown: str) -> str:
    """Keep the top overview and priority table for non-directory questions."""
    marker = "## Work"
    if marker in markdown:
        markdown = markdown[: markdown.index(marker)].rstrip()
    return _truncate(markdown, MAX_COMPACT_CHARS)


def build_directory_context(user_query: str) -> str:
    markdown = _read_directory_map()
    if not markdown:
        return ""

    if _is_directory_related_query(user_query):
        body = _truncate(markdown, MAX_FULL_CHARS)
        scope = "完整目录地图"
    else:
        body = _compact_map(markdown)
        scope = "目录概览"

    return (
        f"【本机文件结构参考：{scope}】\n"
        "以下内容来自 COMPUTER_DIRECTORY_MAP.md，是用户维护的电脑目录结构地图。"
        "它用于理解目录用途和常见位置；如果需要确认某个文件是否真实存在，仍应使用 PATH/FILE 等工具查询。\n\n"
        f"{body}"
    )

