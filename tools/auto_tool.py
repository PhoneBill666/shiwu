"""自动工具调用：检测模型回复中的工具标记并执行。

支持: [SEARCH:...] [FETCH:...] [FILE:...] [PDFMERGE:folder|output] [CANVAS:days]
"""

import re

from tools.web import web_search, web_fetch
from tools.file_reader import read_file
from tools.pdf_tools import pdf_merge
from tools.canvas import get_canvas_schedule, normalize_canvas_days
from core.model import Spinner

# 匹配所有工具标记
_TOOL_RE = re.compile(r"\[(SEARCH|FETCH|FILE|PDFMERGE|CANVAS):([^\]]+)\]")

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


def execute_tool_calls(calls: list[tuple[str, str]]) -> str:
    """执行工具调用，返回合并后的结果文本。"""
    results = []
    for action, argument in calls:
        if action == "SEARCH":
            spinner = Spinner("搜索中")
            spinner.start()
            result = web_search(argument)
            spinner.stop()
            results.append(f"【搜索「{argument}」的结果】\n{result}")
        elif action == "FETCH":
            spinner = Spinner("抓取中")
            spinner.start()
            result = web_fetch(argument)
            spinner.stop()
            results.append(f"【抓取 {argument} 的内容】\n{result}")
        elif action == "FILE":
            import os
            spinner = Spinner("读取文件中")
            spinner.start()
            path = os.path.expanduser(argument)
            result = read_file(path)
            spinner.stop()
            results.append(f"【文件: {argument}】\n{result}")
        elif action == "PDFMERGE":
            parts = argument.split("|", 1)
            if len(parts) == 2:
                folder, output = parts[0].strip(), parts[1].strip()
            else:
                folder, output = argument.strip(), "merged.pdf"
            spinner = Spinner("合并 PDF 中")
            spinner.start()
            result = pdf_merge(folder, output)
            spinner.stop()
            results.append(f"【PDF 合并】\n{result}")
        elif action == "CANVAS":
            spinner = Spinner("获取 Canvas 日程中")
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
    return "\n\n".join(results)
