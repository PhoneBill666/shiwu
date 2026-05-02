"""自动工具调用：检测模型回复中的工具标记并执行。

支持: [SEARCH:...] [FETCH:...] [FILE:...] [PATH:...] [PDFMERGE:folder|output] [CANVAS:days] [STATUS:...] [MAIL:query]
"""

import re
import sys

from tools.web import web_search, web_fetch
from tools.file_reader import read_file
from tools.path_finder import find_paths
from tools.pdf_tools import pdf_merge
from tools.canvas import get_canvas_schedule, normalize_canvas_days
from tools.system_status import get_system_status
from tools.mail_tool import search_mail

# 匹配所有工具标记
_TOOL_RE = re.compile(r"\[(SEARCH|FETCH|FILE|PATH|PDFMERGE|CANVAS|STATUS|MAIL):([^\]]+)\]")
_URL_RE = re.compile(r"https?://[^\s)\]>]+")

# 最多执行几次工具调用（防止循环）
MAX_TOOL_CALLS = 5


def detect_tool_calls(text: str) -> list[tuple[str, str]]:
    """从文本中提取工具调用标记，返回 [(action, argument), ...]。"""
    calls = []
    for match in _TOOL_RE.finditer(text):
        action = match.group(1)
        argument = match.group(2).strip()
        if argument:
            calls.append((action, argument))
    return calls[:MAX_TOOL_CALLS]


def extract_tool_markers(text: str) -> str:
    """只保留回复中的工具标记，避免把模型前置废话写回上下文。"""
    markers = [match.group(0) for match in _TOOL_RE.finditer(text)]
    return " ".join(markers)


def enrich_reply_with_sources(reply: str, tool_results: str, max_urls: int = 5) -> str:
    """补齐真实来源，并清理模型生成的空来源段。"""
    reply = strip_empty_source_section(reply)

    local_source = _infer_local_source_label(tool_results)
    if local_source and not _has_web_source_marker(tool_results):
        trimmed = reply.rstrip()
        if not _has_source_section(reply):
            return f"{trimmed}\n\n来源：{local_source}"
        return reply

    result_urls = _extract_urls(tool_results)
    if not result_urls:
        if local_source:
            trimmed = reply.rstrip()
            if not _has_source_section(reply):
                return f"{trimmed}\n\n来源：{local_source}"
        return reply

    reply_urls = _extract_urls(reply)
    missing = [url for url in result_urls if url not in reply_urls][:max_urls]
    if not missing:
        return reply

    trimmed = reply.rstrip()
    source_lines = "\n".join(f"- {url}" for url in missing)
    if _has_source_section(trimmed):
        return f"{trimmed}\n{source_lines}"
    if re.search(r"引用来源[:：]\s*$", trimmed):
        return f"{trimmed}\n{source_lines}"
    return f"{trimmed}\n\n引用来源：\n{source_lines}"


def strip_empty_source_section(text: str) -> str:
    """Remove a trailing empty "引用来源/来源" heading."""
    return re.sub(r"\n+(?:引用来源|来源)[:：]\s*$", "", text.rstrip()).rstrip()


def _has_source_section(text: str) -> bool:
    return bool(re.search(r"(引用来源|来源)[:：]", text))


def _infer_local_source_label(tool_results: str) -> str:
    if "【系统状态】" in tool_results:
        return "本地系统状态工具（实时读取）"
    if "【Canvas " in tool_results:
        return "Canvas Calendar Feed（本地抓取）"
    if "【邮件】" in tool_results:
        return "macOS Mail 应用（本地读取）"
    if "【文件:" in tool_results:
        return "本地文件读取"
    if "【路径查找】" in tool_results:
        return "本地路径查找工具"
    if "【PDF 合并】" in tool_results:
        return "本地 PDF 合并工具"
    return ""


def _has_web_source_marker(tool_results: str) -> bool:
    return "【搜索" in tool_results or "【抓取 " in tool_results


def source_instruction_for_tool_results(tool_results: str) -> str:
    """Return a prompt instruction that matches the available source type."""
    local_source = _infer_local_source_label(tool_results)
    if local_source and not _has_web_source_marker(tool_results):
        return f"回答末尾注明来源：{local_source}。"

    urls = _extract_urls(tool_results)
    if urls:
        return "回答末尾附上你实际使用到的引用来源 URL。"

    return "如果工具结果没有明确来源，不要输出“引用来源”或“来源”标题。"


def _make_spinner(message: str):
    model_module = sys.modules.get("core.model")
    spinner_class = getattr(model_module, "Spinner", None) if model_module else None
    if spinner_class:
        return spinner_class(message)

    class _NoopSpinner:
        def start(self):
            return None

        def stop(self):
            return None

    return _NoopSpinner()


def _extract_urls(text: str) -> list[str]:
    seen: set[str] = set()
    urls: list[str] = []
    for match in _URL_RE.finditer(text):
        url = match.group(0).rstrip('.,;)]')
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def execute_tool_calls(calls: list[tuple[str, str]]) -> str:
    """执行工具调用，返回合并后的结果文本。"""
    results = []
    for action, argument in calls:
        if action == "SEARCH":
            spinner = _make_spinner(f"正在搜索: {argument}")
            spinner.start()
            result = web_search(argument)
            spinner.stop()
            results.append(f"【搜索「{argument}」的结果】\n{result}")
        elif action == "FETCH":
            spinner = _make_spinner(f"正在抓取网页: {argument}")
            spinner.start()
            result = web_fetch(argument)
            spinner.stop()
            results.append(f"【抓取 {argument} 的内容】\n{result}")
        elif action == "FILE":
            import os
            spinner = _make_spinner(f"正在读取文件: {argument}")
            spinner.start()
            path = os.path.expanduser(argument)
            result = read_file(path)
            spinner.stop()
            results.append(f"【文件: {argument}】\n{result}")
        elif action == "PATH":
            spinner = _make_spinner(f"正在查找路径: {argument}")
            spinner.start()
            result = find_paths(argument, kind="directory")
            spinner.stop()
            results.append(f"【路径查找】\n{result}")
        elif action == "PDFMERGE":
            parts = argument.split("|", 1)
            if len(parts) == 2:
                folder, output = parts[0].strip(), parts[1].strip()
            else:
                folder, output = argument.strip(), "merged.pdf"
            spinner = _make_spinner(f"正在合并 PDF: {argument}")
            spinner.start()
            result = pdf_merge(folder, output)
            spinner.stop()
            results.append(f"【PDF 合并】\n{result}")
        elif action == "CANVAS":
            spinner = _make_spinner(f"正在获取 Canvas 日程（未来 {argument} 天）")
            spinner.start()
            try:
                days = normalize_canvas_days(argument)
                result = get_canvas_schedule(days=days)
            except ValueError as e:
                result = str(e)
            spinner.stop()
            results.append(
                f"【Canvas 未来 {argument} 天日程】\n"
                f"以下是系统抓取的临时外部数据，不属于用户长期记忆。\n{result}"
            )
        elif action == "STATUS":
            spinner = _make_spinner(f"正在获取系统状态: {argument}")
            spinner.start()
            result = get_system_status(argument)
            spinner.stop()
            results.append(f"【系统状态】\n{result}")
        elif action == "MAIL":
            spinner = _make_spinner(f"正在读取邮件: {argument}")
            spinner.start()
            result = search_mail(argument)
            spinner.stop()
            results.append(f"【邮件】\n{result}")
    return "\n\n".join(results)
