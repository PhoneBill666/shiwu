"""Local path lookup tool backed by shell_control.

This is intentionally read-only. It searches common user work locations for a
file or directory name and returns absolute paths.
"""

from __future__ import annotations

import re
from pathlib import Path

from storage_paths import ROOT_DIR
from tools.shell_control import run


MAX_RESULTS = 20
MAX_OUTPUT_CHARS = 20000
PATH_HINTS = ("路径", "在哪", "哪里", "位置", "目录", "文件夹", "项目", "folder", "path")
_PATH_QUERY_PATTERNS = [
    re.compile(r"([A-Za-z0-9_.\-\u4e00-\u9fff]+)\s*(?:文件夹|目录|folder)\s*(?:的)?\s*(?:路径|位置|在哪|在哪里|哪里)"),
    re.compile(r"([A-Za-z0-9_.\-\u4e00-\u9fff]+)\s*(?:项目|project)\s*(?:的)?\s*(?:路径|位置|在哪|在哪里|哪里)"),
    re.compile(r"(?:找|查找|搜索|定位|看看|看一下)\s*([A-Za-z0-9_.\-\u4e00-\u9fff]+)\s*(?:文件夹|目录|folder|路径|位置)"),
    re.compile(r"([A-Za-z0-9_.\-]+)\s*(?:path|folder)"),
]
_PRUNE_DIRS = ("node_modules", ".git", ".venv", "venv", "__pycache__", ".next", "dist", "build")


def _default_roots() -> list[Path]:
    home = Path.home()
    candidates = [
        ROOT_DIR,
        ROOT_DIR.parent,
        ROOT_DIR.parent.parent,
        home / "Work",
        home / "Projects",
        home / "Desktop",
        home / "Documents",
        home / "Downloads",
    ]
    roots: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        try:
            resolved = path.expanduser().resolve()
        except OSError:
            continue
        if not resolved.exists() or not resolved.is_dir():
            continue
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        roots.append(resolved)
    return roots


def _clean_query_name(name: str) -> str:
    cleaned = name.strip().strip("'\"`“”‘’")
    cleaned = re.sub(r"^(这个|那个|当前|本地|我的)", "", cleaned).strip()
    cleaned = re.sub(r"(文件夹|目录|folder|path|路径|位置)$", "", cleaned, flags=re.I).strip()
    return cleaned


def infer_path_query(text: str) -> tuple[str, str] | None:
    """Infer a read-only path lookup from a user utterance.

    Returns:
        (name, kind), where kind is "directory" or "any".
    """
    lowered = text.lower()
    if not any(hint in lowered or hint in text for hint in PATH_HINTS):
        return None

    for pattern in _PATH_QUERY_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        name = _clean_query_name(match.group(1))
        if name:
            kind = "directory" if any(word in text for word in ("文件夹", "目录", "项目", "folder", "project")) else "any"
            return name, kind
    return None


def _direct_matches(name: str, roots: list[Path], kind: str) -> list[str]:
    lowered = name.lower()
    matches: list[str] = []
    seen: set[str] = set()

    candidates = [ROOT_DIR, *ROOT_DIR.parents]
    for root in roots:
        candidates.append(root)
        try:
            candidates.extend(root.iterdir())
        except OSError:
            continue

    for path in candidates:
        try:
            resolved = path.expanduser().resolve()
        except OSError:
            continue
        if resolved.name.lower() != lowered:
            continue
        if kind == "directory" and not resolved.is_dir():
            continue
        if kind == "file" and not resolved.is_file():
            continue
        value = str(resolved)
        if value not in seen:
            seen.add(value)
            matches.append(value)
    return matches


def find_paths(
    name: str,
    kind: str = "any",
    roots: list[str] | None = None,
    max_results: int = MAX_RESULTS,
) -> str:
    """Find local paths by basename using safe shell commands."""
    name = _clean_query_name(name)
    if not name:
        return "没有提供要查找的文件或文件夹名称。"

    explicit = Path(name).expanduser()
    if explicit.exists():
        return f"找到明确路径：\n  {explicit.resolve()}"

    search_roots = [Path(root).expanduser().resolve() for root in roots] if roots else _default_roots()
    if not search_roots:
        return "没有可搜索的本地目录。"

    direct = _direct_matches(name, search_roots, kind)
    if direct:
        label = "文件夹" if kind == "directory" else "路径"
        lines = [f"找到明确匹配的{label}："]
        lines.extend(f"  {path}" for path in direct[:max_results])
        return "\n".join(lines)

    find_args = ["-iname", name, "-print"]
    if kind == "directory":
        find_args = ["-type", "d", *find_args]
    elif kind == "file":
        find_args = ["-type", "f", *find_args]

    prune_args = ["("]
    for index, dirname in enumerate(_PRUNE_DIRS):
        if index:
            prune_args.append("-o")
        prune_args.extend(["-name", dirname])
    prune_args.extend([")", "-prune", "-o"])

    matches: list[str] = []
    seen: set[str] = set()
    errors: list[str] = []

    for root in search_roots:
        result = run(
            ["find", str(root), *prune_args, *find_args],
            timeout=8,
            max_output_chars=MAX_OUTPUT_CHARS,
        )
        if result.error:
            errors.append(f"{root}: {result.error}")
            continue
        for line in result.stdout.splitlines():
            path = line.strip()
            if not path or path in seen:
                continue
            seen.add(path)
            matches.append(path)
            if len(matches) >= max_results:
                break
        if len(matches) >= max_results:
            break

    label = "文件夹" if kind == "directory" else "路径"
    if matches:
        lines = [f"找到 {len(matches)} 个匹配的{label}："]
        lines.extend(f"  {path}" for path in matches)
        if len(matches) >= max_results:
            lines.append(f"  ...（最多显示 {max_results} 条）")
        return "\n".join(lines)

    root_text = ", ".join(str(root) for root in search_roots)
    if errors:
        return f"没有找到名为 {name} 的{label}。\n已搜索: {root_text}\n部分目录查询失败: {'; '.join(errors[:3])}"
    return f"没有找到名为 {name} 的{label}。\n已搜索: {root_text}"
