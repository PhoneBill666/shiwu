"""Read-only local search tools.

Provides three primitives:
- list_dir: inspect a directory tree
- glob_paths: find paths by filename pattern
- grep_content: search inside text/code files with ripgrep
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

from storage_paths import ROOT_DIR
from tools.shell_control import normalize_path, run, truncate_output


DEFAULT_MAX_RESULTS = 80
DEFAULT_MAX_CHARS = 16000
SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    "venv",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".next",
    "dist",
    "build",
    "target",
    ".idea",
    ".vscode",
}

_EXTENSION_QUERY_RE = re.compile(
    r"(?:找|查找|搜索|列出|列一下)\s*(?P<root>.+?)\s*(?:里面|里|下|目录下|中)\s*(?:所有|全部|全部的)?\s*\.?(?P<ext>[A-Za-z0-9]{1,8})\s*(?:格式)?(?:文件)?[？?。.]?$",
    re.I,
)
_LIST_QUERY_RE = re.compile(
    r"(?:看看|看一下|列出|列一下)\s*(?P<root>.+?)\s*(?:里面|里|下|目录下|的)?\s*(?:有什么|结构|目录|文件)[？?。.]?$",
    re.I,
)
_GREP_QUERY_RE = re.compile(
    r"(?:在|搜|搜索|查找)\s*(?P<root>.+?)\s*(?:里面|里|下|目录下|中)\s*(?:搜|搜索|查找|找)\s*(?P<pattern>.+?)[？?。.]?$",
    re.I,
)
_COURSE_RE = re.compile(r"^(?:cs|compsci)\s*[-_ ]?(\d{3})$", re.I)


def _resolve_root(root: str | None) -> Path:
    if root:
        path = Path(normalize_path(root))
    else:
        path = ROOT_DIR
    if not path.exists():
        raise FileNotFoundError(f"路径不存在: {path}")
    if not path.is_dir():
        raise NotADirectoryError(f"不是目录: {path}")
    return path


def _clean_root_name(text: str) -> str:
    cleaned = text.strip().strip("'\"`“”‘’")
    cleaned = re.sub(r"^(这个|那个|当前|本地|我的)", "", cleaned).strip()
    cleaned = re.sub(r"(文件夹|目录|项目|folder|project)$", "", cleaned, flags=re.I).strip()
    return cleaned


def _resolve_named_root(name: str) -> Path | None:
    name = _clean_root_name(name)
    if not name:
        return None

    explicit = Path(name).expanduser()
    if explicit.exists() and explicit.is_dir():
        return explicit.resolve()

    home = Path.home()
    candidates: list[Path] = []

    course_match = _COURSE_RE.match(name)
    if course_match:
        course = f"cs{course_match.group(1)}"
        candidates.extend([
            home / "Study" / "UOA" / course,
            home / "Study" / "UOAIC" / course,
            home / "Work" / f"CS{course_match.group(1)} Ccode",
            home / "Work" / f"CS{course_match.group(1)} javacode",
        ])

    candidates.extend([
        home / "Work" / "Dev" / name,
        home / "Work" / name,
        home / "Study" / "UOA" / name,
        home / "Study" / "UOAIC" / name,
        home / "Study" / name,
        home / "Downloads" / name,
        home / "Desktop" / name,
        home / "Documents" / name,
    ])

    lowered = name.lower()
    for candidate in candidates:
        if candidate.exists() and candidate.is_dir():
            return candidate.resolve()

    shallow_roots = [
        home / "Work" / "Dev",
        home / "Work",
        home / "Study" / "UOA",
        home / "Study" / "UOAIC",
        home / "Study",
        home / "Downloads",
        home / "Desktop",
        home / "Documents",
    ]
    for root in shallow_roots:
        if not root.exists() or not root.is_dir():
            continue
        try:
            for child in root.iterdir():
                if child.is_dir() and child.name.lower() == lowered:
                    return child.resolve()
        except OSError:
            continue
    return None


def _should_skip(path: Path) -> bool:
    return path.name in SKIP_DIRS


def _display_path(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def list_dir(
    path: str | None = None,
    depth: int = 1,
    max_entries: int = DEFAULT_MAX_RESULTS,
    include_hidden: bool = False,
) -> str:
    """Return a compact directory tree."""
    try:
        root = _resolve_root(path)
    except Exception as exc:
        return f"列目录失败: {exc}"

    depth = max(0, min(depth, 5))
    max_entries = max(1, min(max_entries, 300))
    lines = [f"{root}/"]
    count = 0
    skipped = 0

    def walk(current: Path, level: int) -> None:
        nonlocal count, skipped
        if level >= depth or count >= max_entries:
            return
        try:
            children = sorted(current.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError:
            return
        visible = []
        for child in children:
            if not include_hidden and child.name.startswith("."):
                skipped += 1
                continue
            if child.is_dir() and _should_skip(child):
                skipped += 1
                continue
            visible.append(child)

        for index, child in enumerate(visible):
            if count >= max_entries:
                break
            count += 1
            connector = "└── " if index == len(visible) - 1 else "├── "
            suffix = "/" if child.is_dir() else ""
            lines.append(f"{'│   ' * level}{connector}{child.name}{suffix}")
            if child.is_dir():
                walk(child, level + 1)

    walk(root, 0)
    if count >= max_entries:
        lines.append(f"...（最多显示 {max_entries} 项）")
    if skipped:
        lines.append(f"...（已跳过 {skipped} 个隐藏/缓存/依赖目录）")
    return "\n".join(lines)


def glob_paths(
    root: str | None,
    pattern: str,
    max_results: int = DEFAULT_MAX_RESULTS,
    include_hidden: bool = False,
) -> str:
    """Find files or directories by glob pattern under root."""
    pattern = pattern.strip() or "*"
    try:
        search_root = _resolve_root(root)
    except Exception as exc:
        return f"按模式查找失败: {exc}"

    max_results = max(1, min(max_results, 300))
    pattern_variants = [pattern]
    if pattern.startswith("**/") and len(pattern) > 3:
        pattern_variants.append(pattern[3:])
    elif "/" not in pattern:
        pattern_variants.append(f"**/{pattern}")
    pattern_variants = list(dict.fromkeys(pattern_variants))

    matches: list[Path] = []
    skipped = 0

    for path in search_root.rglob("*"):
        if len(matches) >= max_results:
            break
        parts = set(path.relative_to(search_root).parts[:-1])
        if parts & SKIP_DIRS:
            skipped += 1
            continue
        if not include_hidden and any(part.startswith(".") for part in path.relative_to(search_root).parts):
            skipped += 1
            continue
        rel = _display_path(path, search_root)
        rel_lower = rel.lower()
        name_lower = path.name.lower()
        if any(
            fnmatch.fnmatch(name_lower, variant.lower())
            or fnmatch.fnmatch(rel_lower, variant.lower())
            for variant in pattern_variants
        ):
            matches.append(path)

    if not matches:
        return f"没有找到匹配 `{pattern}` 的路径。\n搜索范围: {search_root}"

    lines = [f"找到 {len(matches)} 个匹配 `{pattern}` 的路径："]
    lines.extend(f"  {path}" for path in matches)
    if len(matches) >= max_results:
        lines.append(f"  ...（最多显示 {max_results} 条）")
    if skipped:
        lines.append(f"...（已跳过部分隐藏/缓存/依赖路径）")
    return "\n".join(lines)


def grep_content(
    root: str | None,
    pattern: str,
    glob: str | None = None,
    max_results: int = 80,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> str:
    """Search file contents using ripgrep."""
    pattern = pattern.strip()
    if not pattern:
        return "没有提供要搜索的内容。"

    try:
        search_root = _resolve_root(root)
    except Exception as exc:
        return f"内容搜索失败: {exc}"

    max_results = max(1, min(max_results, 300))
    command = [
        "rg",
        "--line-number",
        "--column",
        "--smart-case",
        "--hidden",
    ]
    for dirname in sorted(SKIP_DIRS):
        command.extend(["--glob", f"!**/{dirname}/**"])
    if glob:
        command.extend(["--glob", glob])
    command.extend([pattern, str(search_root)])

    result = run(command, timeout=20, max_output_chars=max_chars)
    if result.error and "No such file or directory" in result.error:
        return "内容搜索失败: 未安装 ripgrep。请先安装 `rg`。"
    if result.timed_out:
        return f"内容搜索超时，已搜索: {search_root}"
    if result.error:
        return f"内容搜索失败: {result.error}"
    if result.exit_code == 1 or not result.stdout.strip():
        return f"没有在 {search_root} 中找到内容：{pattern}"
    if result.exit_code not in (0, 1):
        detail = result.stderr.strip() or result.stdout.strip() or f"exit_code={result.exit_code}"
        return f"内容搜索失败: {detail}"

    lines = result.stdout.splitlines()
    limited = "\n".join(lines[:max_results])
    output = truncate_output(limited, max_chars)
    header = f"在 {search_root} 中找到内容 `{pattern}`："
    if glob:
        header += f"\n文件过滤: {glob}"
    if len(lines) > max_results:
        output += f"\n...（最多显示 {max_results} 条，实际更多）"
    return f"{header}\n{output}"


def infer_local_search_query(text: str) -> str | None:
    """Infer a deterministic read-only LOCAL query from common utterances."""
    stripped = text.strip()

    ext_match = _EXTENSION_QUERY_RE.search(stripped)
    if ext_match:
        root = _resolve_named_root(ext_match.group("root"))
        if root:
            ext = ext_match.group("ext").lower().lstrip(".")
            return f"glob:{root}|**/*.{ext}"

    list_match = _LIST_QUERY_RE.search(stripped)
    if list_match:
        root = _resolve_named_root(list_match.group("root"))
        if root:
            return f"ls:{root}|depth=2"

    grep_match = _GREP_QUERY_RE.search(stripped)
    if grep_match:
        root = _resolve_named_root(grep_match.group("root"))
        pattern = grep_match.group("pattern").strip()
        if root and pattern:
            return f"grep:{root}|{pattern}"

    return None


def execute_local_search(argument: str) -> str:
    """Parse compact tool arguments.

    Supported:
    - ls:/path|depth=2
    - glob:/path|*.pdf
    - grep:/path|pattern
    - grep:/path|pattern|*.py
    """
    mode, sep, rest = argument.partition(":")
    if not sep:
        return "LOCAL 参数格式错误。用法: ls:/path|depth=2 或 glob:/path|*.pdf 或 grep:/path|关键词|*.py"

    mode = mode.strip().lower()
    parts = [part.strip() for part in rest.split("|")]
    root = parts[0] or None

    if mode == "ls":
        depth = 1
        max_entries = DEFAULT_MAX_RESULTS
        for item in parts[1:]:
            if item.startswith("depth="):
                try:
                    depth = int(item[len("depth="):])
                except ValueError:
                    depth = 1
            elif item.startswith("max="):
                try:
                    max_entries = int(item[len("max="):])
                except ValueError:
                    max_entries = DEFAULT_MAX_RESULTS
        return list_dir(root, depth=depth, max_entries=max_entries)

    if mode == "glob":
        pattern = parts[1] if len(parts) > 1 and parts[1] else "*"
        return glob_paths(root, pattern)

    if mode == "grep":
        if len(parts) < 2 or not parts[1]:
            return "grep 需要搜索内容。用法: grep:/path|关键词|*.py"
        file_glob = parts[2] if len(parts) > 2 and parts[2] else None
        return grep_content(root, parts[1], glob=file_glob)

    return f"不支持的 LOCAL 模式: {mode}"
