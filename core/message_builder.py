from datetime import datetime

from memory.conversation import Conversation
from memory.memory_retriever import retrieve, format_for_prompt
from memory.memory_store import MemoryStore

WEEKDAYS = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
FACT_QUERY_HINTS = [
    "你是谁",
    "你叫什么",
    "我是谁",
    "你记得什么",
    "你对我的了解",
    "总结一下你对我的了解",
]


def build_messages(
    system_prompt: str,
    conversation: Conversation,
    memory_store: MemoryStore,
    user_query: str,
) -> list[dict[str, str]]:
    system_parts: list[str] = [system_prompt]

    # 当前时间注入
    now = datetime.now().astimezone()
    runtime_text = (
        f"【runtime context】当前时间: "
        f"{now.strftime('%Y年%m月%d日')} {WEEKDAYS[now.weekday()]} {now.strftime('%H:%M')}"
    )
    system_parts.append(runtime_text)

    # 长期记忆
    relevant = retrieve(user_query, memory_store)
    memory_text = format_for_prompt(relevant)
    if memory_text:
        system_parts.append(memory_text)

    if any(hint in user_query for hint in FACT_QUERY_HINTS):
        system_parts.append(
            "这是事实型问题。请优先使用 runtime context 与长期记忆。"
            "若信息未提供，请明确说不知道，不要猜测。"
            "回答后直接结束，不要反问用户“记住了吗”“对吗”“可以吗”。"
        )

    messages: list[dict[str, str]] = [
        {"role": "system", "content": "\n\n".join(system_parts)},
    ]

    # 防御性过滤：历史中若出现 system（脏数据），避免破坏 chat template
    history = [m for m in conversation.get_trimmed_history() if m.get("role") != "system"]
    messages.extend(history)
    return messages
