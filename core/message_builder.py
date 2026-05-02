from core.directory_context import build_directory_context
from core.temporal_context import build_runtime_context, query_mentions_past_time
from memory.conversation import Conversation
from memory.memory_retriever import retrieve, format_for_prompt
from memory.memory_store import MemoryStore

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
    model_name: str | None = None,
) -> list[dict[str, str]]:
    system_parts: list[str] = [system_prompt]

    system_parts.append(build_runtime_context(model_name=model_name))

    directory_context = build_directory_context(user_query)
    if directory_context:
        system_parts.append(directory_context)

    # 长期记忆
    relevant = retrieve(user_query, memory_store)
    memory_text = format_for_prompt(relevant)
    if memory_text:
        system_parts.append(memory_text)

    recent_timeline = conversation.build_recent_timeline()
    if recent_timeline:
        system_parts.append(recent_timeline)

    temporal_log_text = memory_store.build_temporal_log_context(user_query)
    if temporal_log_text:
        system_parts.append(temporal_log_text)

    if any(hint in user_query for hint in FACT_QUERY_HINTS):
        system_parts.append(
            "这是事实型问题。请优先使用 runtime context 与长期记忆。"
            "若信息未提供，请明确说不知道，不要猜测。"
            "回答后直接结束，不要反问用户“记住了吗”“对吗”“可以吗”。"
        )

    if query_mentions_past_time(user_query):
        system_parts.append(
            "这是带时间线的历史问题。请明确区分“过去记录”与“当前这轮对话”。"
            "如果引用了历史日志，回答里可以直接说明是上周、某天或某个历史时间点发生的。"
        )

    messages: list[dict[str, str]] = [
        {"role": "system", "content": "\n\n".join(system_parts)},
    ]

    # 防御性过滤：历史中若出现 system（脏数据），避免破坏 chat template
    history = [m for m in conversation.get_trimmed_history() if m.get("role") != "system"]
    messages.extend(history)
    return messages
