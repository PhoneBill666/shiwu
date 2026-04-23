"""文件读取工具：支持 PDF、Word (.docx)、纯文本等格式。

两种使用方式：
1. @文件名 — 从 shared_files/ 目录读取
2. [/绝对路径] — 直接读取本机文件
"""

import os
import re

# 项目根目录下的共享文件夹
SHARED_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "shared_files")

# 文件内容最大字符数（防止撑爆上下文）
MAX_CHARS = 15000

# 支持的文件扩展名
SUPPORTED_EXTENSIONS = {
    ".txt", ".md", ".py", ".js", ".ts", ".json", ".csv", ".yaml", ".yml",
    ".html", ".css", ".xml", ".log", ".sh", ".toml", ".ini", ".cfg",
    ".java", ".c", ".cpp", ".h", ".go", ".rs", ".rb", ".php", ".sql",
    ".pdf", ".docx",
}

# ---- 解析用户输入 ----

# @文件名（shared_files 目录）
_AT_RE = re.compile(r"@([\w\-\.]+\.\w+)")
# [/路径] 或 [~/路径]（本机绝对路径）
_PATH_RE = re.compile(r"\[([/~][^\]]+)\]")


def parse_file_references(text: str) -> list[dict]:
    """从用户输入中提取文件引用，返回 [{type, name, path}, ...]。"""
    refs = []
    seen = set()

    for match in _AT_RE.finditer(text):
        filename = match.group(1)
        if filename in seen:
            continue
        seen.add(filename)
        path = os.path.join(SHARED_DIR, filename)
        refs.append({"type": "shared", "name": filename, "path": path})

    for match in _PATH_RE.finditer(text):
        raw_path = match.group(1).strip()
        path = os.path.expanduser(raw_path)
        if path in seen:
            continue
        seen.add(path)
        refs.append({"type": "local", "name": os.path.basename(path), "path": path})

    return refs


def strip_file_references(text: str) -> str:
    """从用户输入中移除文件引用标记，保留其余文本。"""
    text = _AT_RE.sub("", text)
    text = _PATH_RE.sub("", text)
    return text.strip()


# ---- 文件读取 ----


def read_file(path: str, max_chars: int = MAX_CHARS) -> str:
    """读取文件内容，根据扩展名自动选择读取方式。"""
    if not os.path.exists(path):
        return f"文件不存在: {path}"

    ext = os.path.splitext(path)[1].lower()

    if ext not in SUPPORTED_EXTENSIONS:
        return f"不支持的文件格式: {ext}（支持: {', '.join(sorted(SUPPORTED_EXTENSIONS))}）"

    try:
        if ext == ".pdf":
            text = _read_pdf(path)
        elif ext == ".docx":
            text = _read_docx(path)
        else:
            text = _read_text(path)
    except Exception as e:
        return f"读取文件失败 ({path}): {e}"

    truncated = ""
    if len(text) > max_chars:
        text = text[:max_chars]
        truncated = f"\n\n（内容已截断，显示前 {max_chars} 字符）"

    return text + truncated


def _read_pdf(path: str) -> str:
    """读取 PDF 文件。"""
    import fitz
    doc = fitz.open(path)
    pages = []
    for i, page in enumerate(doc):
        page_text = page.get_text().strip()
        if page_text:
            pages.append(f"--- 第 {i + 1} 页 ---\n{page_text}")
    doc.close()
    return "\n\n".join(pages) if pages else "（PDF 无法提取文本内容，可能是扫描件）"


def _read_docx(path: str) -> str:
    """读取 Word (.docx) 文件。"""
    import docx
    doc = docx.Document(path)
    paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
    return "\n".join(paragraphs) if paragraphs else "（文档内容为空）"


def _read_text(path: str) -> str:
    """读取纯文本文件。"""
    encodings = ["utf-8", "gbk", "gb2312", "latin-1"]
    for enc in encodings:
        try:
            with open(path, "r", encoding=enc) as f:
                return f.read()
        except (UnicodeDecodeError, UnicodeError):
            continue
    return "（无法解码文件内容，尝试了 UTF-8 / GBK / Latin-1）"


# ---- 批量读取 ----


def read_all_references(refs: list[dict]) -> str:
    """批量读取所有文件引用，返回合并后的文本。"""
    if not refs:
        return ""

    parts = []
    for ref in refs:
        content = read_file(ref["path"])
        label = f"@{ref['name']}" if ref["type"] == "shared" else ref["path"]
        parts.append(f"【文件: {label}】\n{content}")

    return "\n\n".join(parts)


def list_shared_files() -> list[str]:
    """列出 shared_files 目录下的所有文件。"""
    if not os.path.isdir(SHARED_DIR):
        return []
    return sorted(f for f in os.listdir(SHARED_DIR) if not f.startswith("."))
