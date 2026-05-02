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
MAX_OUTPUT_CHARS = 40000
PATH_HINTS = ("路径", "在哪", "哪里", "位置", "目录", "文件夹", "文件", "项目", "folder", "file", "path")
_PATH_QUERY_PATTERNS = [
    re.compile(r"([A-Za-z0-9_.\-\u4e00-\u9fff]+)\s*(?:文件夹|目录|folder)\s*(?:的)?\s*(?:路径|位置|在哪|在哪里|哪里)"),
    re.compile(r"(?:文件|file)\s+(.+?)\s*(?:的)?\s*(?:路径|位置|在哪|在哪里|哪里)[？?]?$", re.I),
    re.compile(r"(.+?)\s*(?:文件|file)\s*(?:的)?\s*(?:路径|位置|在哪|在哪里|哪里)[？?]?$", re.I),
    re.compile(r"(?:找|查找|搜索|定位|看看|看一下)\s*(?:文件|file)\s+(.+?)(?:\s*(?:的)?\s*(?:路径|位置|在哪|在哪里|哪里))?[？?]?$", re.I),
    re.compile(r"([A-Za-z0-9_.\-\u4e00-\u9fff]+)\s*(?:项目|project)\s*(?:的)?\s*(?:路径|位置|在哪|在哪里|哪里)"),
    re.compile(r"(?:找|查找|搜索|定位|看看|看一下)\s*([A-Za-z0-9_.\-\u4e00-\u9fff ]+?)\s*(?:文件夹|目录|folder|路径|位置)[？?]?$"),
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
        home / "Study",
        home / "Projects",
        home / "Desktop",
        home / "Documents",
        home / "Downloads",
        home / "Library" / "Mobile Documents" / "com~apple~CloudDocs",
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
    cleaned = re.sub(r"(文件夹|目录|文件|folder|file|path|路径|位置)$", "", cleaned, flags=re.I).strip()
    cleaned = cleaned.strip("？?。.")
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
            if any(word in lowered for word in ("folder", "project")) or any(word in text for word in ("文件夹", "目录", "项目")):
                kind = "directory"
            elif any(word in lowered for word in ("file",)) or "文件" in text:
                kind = "file"
            else:
                kind = "any"
            return name, kind
    return None


def infer_kind_from_name(name: str) -> str:
    lowered = name.lower()
    if lowered.startswith(("file ", "文件 ")):
        return "file"
    if lowered.startswith(("folder ", "directory ", "project ")) or lowered.startswith(("文件夹 ", "目录 ", "项目 ")):
        return "directory"
    return "any"


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


def _search_tokens(name: str) -> list[str]:
    normalized = re.sub(r"[_\-.]+", " ", name)
    return [
        token.lower()
        for token in re.findall(r"[A-Za-z0-9\u4e00-\u9fff]+", normalized)
        if len(token) >= 2 or token.isdigit()
    ]


def _rank_matches(paths: list[str], query: str, allow_weak: bool = True) -> list[str]:
    tokens = _search_tokens(query)
    if not tokens:
        return paths

    def score(path: str) -> tuple[int, int, str]:
        base = Path(path).name.lower()
        normalized_base = re.sub(r"[_\-.]+", " ", base)
        full = path.lower()
        token_hits = sum(1 for token in tokens if token in normalized_base)
        full_hits = sum(1 for token in tokens if token in full)
        phrase_hit = int(query.lower() in base)
        return (phrase_hit, token_hits, full_hits, path)

    ranked = sorted(paths, key=score, reverse=True)
    strong = [path for path in ranked if score(path)[1] == len(tokens) or score(path)[0]]
    if strong:
        return strong
    return ranked if allow_weak else []


def _find_once(root: Path, find_args: list[str], max_output_chars: int = MAX_OUTPUT_CHARS):
    prune_args = ["("]
    for index, dirname in enumerate(_PRUNE_DIRS):
        if index:
            prune_args.append("-o")
        prune_args.extend(["-name", dirname])
    prune_args.extend([")", "-prune", "-o"])
    return run(
        ["find", str(root), *prune_args, *find_args],
        timeout=10,
        max_output_chars=max_output_chars,
    )


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

    type_args: list[str] = []
    if kind == "directory":
        type_args = ["-type", "d"]
    elif kind == "file":
        type_args = ["-type", "f"]

    search_patterns = [name]
    if "*" not in name:
        search_patterns.append(f"*{name}*")

    matches: list[str] = []
    seen: set[str] = set()
    errors: list[str] = []

    for root in search_roots:
        for pattern in search_patterns:
            result = _find_once(root, [*type_args, "-iname", pattern, "-print"])
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
            if matches:
                break
        if len(matches) >= max_results:
            break

    if not matches:
        tokens = _search_tokens(name)
        first_token = next((token for token in tokens if not token.isdigit()), tokens[0] if tokens else "")
        if first_token:
            broad_matches: list[str] = []
            for root in search_roots:
                result = _find_once(root, [*type_args, "-iname", f"*{first_token}*", "-print"], max_output_chars=MAX_OUTPUT_CHARS * 2)
                if result.error:
                    errors.append(f"{root}: {result.error}")
                    continue
                for line in result.stdout.splitlines():
                    path = line.strip()
                    if not path or path in seen:
                        continue
                    seen.add(path)
                    broad_matches.append(path)
                if len(broad_matches) >= max_results * 5:
                    break
            matches = _rank_matches(broad_matches, name, allow_weak=False)[:max_results]
    else:
        matches = _rank_matches(matches, name)[:max_results]

    label = "文件夹" if kind == "directory" else "文件" if kind == "file" else "路径"
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
