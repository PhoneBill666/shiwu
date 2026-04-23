"""自动工具调用：检测模型回复中的 [SEARCH:...] / [FETCH:...] / [FILE:...] 标记并执行。"""

import re

from tools.web import web_search, web_fetch
from tools.file_reader import read_file
from core.model import Spinner

# 匹配 [SEARCH:...] / [FETCH:...] / [FILE:...]
_TOOL_RE = re.compile(r"\[(SEARCH|FETCH|FILE):([^\]]+)\]")

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
    return "\n\n".join(results)
