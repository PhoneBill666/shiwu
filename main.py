import re

from prompt_toolkit import prompt as pt_prompt
from prompt_toolkit.history import InMemoryHistory

from core.model import LocalModel, AVAILABLE_MODELS, Spinner
from core.message_builder import build_messages
from memory.conversation import Conversation
from memory.memory_store import MemoryStore
from memory.llm_extractor import try_llm_extract, reextract_from_logs_llm
from prompts.system import SYSTEM_PROMPT
from tools.web import web_search, web_fetch
from tools.auto_tool import (
    detect_tool_calls,
    execute_tool_calls,
    enrich_reply_with_sources,
    extract_tool_markers,
    source_instruction_for_tool_results,
    strip_empty_source_section,
)
from tools.canvas import (
    DEFAULT_CANVAS_DAYS,
    canvas_status,
    clear_canvas_feed_url,
    get_canvas_context_for_query,
    get_canvas_history_for_query,
    get_canvas_schedule,
    mark_canvas_events_completed,
    mark_canvas_events_pending,
    normalize_canvas_days,
    save_canvas_feed_url,
    should_mark_canvas_completed,
    should_mark_canvas_pending,
    should_auto_check_canvas,
)
from tools.file_opener import extract_paths_from_text, infer_open_request, open_recent
from tools.file_reader import parse_file_references, strip_file_references, read_all_references, list_shared_files
from tools.local_search import execute_local_search, infer_local_search_query
from tools.path_finder import find_paths, infer_path_query
from tools.pdf_tools import pdf_merge
from tools.system_status import get_system_status, infer_status_query

REMEMBER_KIND_RE = re.compile(
    r"^(user_identity|user_preference|project_context|technical_constraint|assistant_identity|other|manual)\s*[:：]\s*(.+)$"
)

COMMANDS = {
    "/exit": "退出程序",
    "/clear": "清空对话历史（不影响长期记忆）",
    "/history": "查看当前对话历史",
    "/memories": "查看长期记忆条目",
    "/forget <id>": "删除一条长期记忆",
    "/remember <内容>": "手动添加长期记忆（可选 kind: /remember user_preference: ...）",
    "/reextract [最近N条日志]": "从 memory_log 离线补提炼长期记忆",
    "/model": "查看当前模型",
    "/models": "列出可用模型",
    "/switch <编号>": "切换模型",
    "/think": "开关思考模式（Qwen3.5）",
    "/thinking": "查看最近一次思考过程",
    "/tokens": "查看当前 max_tokens",
    "/tokens <数量>": "设置 max_tokens",
    "/search <关键词>": "联网搜索（DuckDuckGo）",
    "/fetch <url>": "抓取网页内容",
    "/canvas": "查看或配置 Canvas Calendar Feed 日程",
    "/canvas history <日期>": "查看 Canvas 历史缓存中的过去日程",
    "/canvas done <内容>": "将匹配到的 Canvas 事件标记为已完成",
    "/canvas undo <内容>": "将匹配到的 Canvas 事件改回未完成",
    "/files": "列出 shared_files/ 中可用 @引用 的文件",
    "/pdfmerge <文件夹> <输出名>": "合并文件夹内所有 PDF",
    "/sys [项]": "查看系统状态（battery/cpu/memory/disk/network/foreground/processes/ports/all）",
    "/help": "显示可用命令",
}


def print_help():
    print("\n可用命令：")
    for cmd, desc in COMMANDS.items():
        print(f"  {cmd:20s} {desc}")
    print()


def _print_appended_reply_delta(original: str, enriched: str):
    if enriched == original:
        return

    visible_original = strip_empty_source_section(original)
    for base in (visible_original, original):
        if enriched.startswith(base):
            suffix = enriched[len(base):]
            if suffix:
                print(suffix)
            return


def _tool_result_spinner_messages(tool_calls: list[tuple[str, str]]) -> tuple[str, str]:
    actions = {action for action, _ in tool_calls}
    if "SEARCH" in actions:
        return "正在整理搜索结果", "正在生成回答"
    if "FETCH" in actions:
        return "正在整理网页内容", "正在生成回答"
    if "FILE" in actions:
        return "正在整理文件内容", "正在生成回答"
    if "PATH" in actions:
        return "正在整理路径结果", "正在生成回答"
    if "LOCAL" in actions:
        return "正在整理本机搜索结果", "正在生成回答"
    if "PDFMERGE" in actions:
        return "正在整理 PDF 结果", "正在生成回答"
    if "CANVAS" in actions:
        return "正在整理 Canvas 日程", "正在生成回答"
    if "STATUS" in actions:
        return "正在整理系统状态", "正在生成回答"
    if "MAIL" in actions:
        return "正在整理邮件结果", "正在生成回答"
    return "正在整理工具结果", "正在生成回答"


def main():
    model = LocalModel()
    conv = Conversation()
    mem = MemoryStore()
    input_history = InMemoryHistory()
    recent_file_paths: list[str] = []

    print("\n本地助手已启动，输入你的问题开始对话。")
    print("输入 /help 查看命令列表。\n")

    while True:
        try:
            user_input = pt_prompt("你: ", history=input_history).strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            break

        if not user_input:
            continue

        # ---- 命令处理 ----
        if user_input == "/exit":
            print("再见！")
            break
        elif user_input == "/clear":
            conv.reset()
            print("对话历史已清空（长期记忆不受影响）。\n")
            continue
        elif user_input == "/history":
            print(conv.summary())
            print()
            continue
        elif user_input == "/memories":
            print(mem.display())
            print()
            continue
        elif user_input.startswith("/forget "):
            mid = user_input[len("/forget "):].strip()
            if mem.remove_memory(mid):
                print(f"已删除记忆 [{mid}]。\n")
            else:
                print(f"未找到记忆 [{mid}]。\n")
            continue
        elif user_input.startswith("/remember "):
            payload = user_input[len("/remember "):].strip()
            kind = "manual"
            content = payload
            m = REMEMBER_KIND_RE.match(payload)
            if m:
                kind = m.group(1)
                content = m.group(2).strip()
            if content:
                item = mem.add_memory(
                    kind=kind,
                    content=content,
                    source="user_command",
                    confidence=0.98,
                )
                print(f"已保存记忆 [{item['id']}] ({item['kind']}): {content}\n")
            else:
                print("请提供要记住的内容。\n")
            continue
        elif user_input.startswith("/reextract"):
            arg = user_input[len("/reextract"):].strip()
            log_limit = 200
            if arg:
                try:
                    log_limit = int(arg)
                    if log_limit <= 0:
                        print("请输入正整数，例如: /reextract 200\n")
                        continue
                except ValueError:
                    print("参数无效，示例: /reextract 200\n")
                    continue
            stats = reextract_from_logs_llm(model, mem, log_limit=log_limit, max_new=25)
            print(
                "离线补提炼完成："
                f"\n  扫描日志: {stats['scanned_logs']}"
                f"\n  处理批次: {stats['batches']}"
                f"\n  新增记忆: {stats['added']}"
                f"\n  重复跳过: {stats['duplicates']}"
                f"\n  墓碑拦截: {stats.get('tombstoned', 0)}\n"
            )
            continue
        elif user_input == "/model":
            print(f"当前模型: {model.model_name}\n")
            continue
        elif user_input == "/models":
            print("\n可用模型：")
            for i, name in enumerate(AVAILABLE_MODELS):
                marker = " <-- 当前" if name == model.model_name else ""
                print(f"  [{i}] {name}{marker}")
            print(f"\n使用 /switch <编号> 切换模型。\n")
            continue
        elif user_input.startswith("/switch "):
            arg = user_input[len("/switch "):].strip()
            try:
                idx = int(arg)
                if 0 <= idx < len(AVAILABLE_MODELS):
                    model.switch(AVAILABLE_MODELS[idx])
                    print()
                else:
                    print(f"编号无效，请输入 0 到 {len(AVAILABLE_MODELS) - 1}。\n")
            except ValueError:
                print("请输入模型编号，例如: /switch 1\n")
            continue
        elif user_input == "/think":
            model.thinking_enabled = not model.thinking_enabled
            state = "开启" if model.thinking_enabled else "关闭"
            print(f"思考模式: {state}\n")
            continue
        elif user_input == "/thinking":
            if model.last_thinking:
                print(f"\n--- 最近一次思考过程 ---\n{model.last_thinking}\n--- 结束 ---\n")
            else:
                print("暂无思考记录。\n")
            continue
        elif user_input == "/tokens":
            print(f"当前 max_tokens: {model.max_tokens}\n")
            continue
        elif user_input.startswith("/tokens "):
            arg = user_input[len("/tokens "):].strip()
            try:
                val = int(arg)
                if val > 0:
                    model.max_tokens = val
                    print(f"max_tokens 已设置为: {val}\n")
                else:
                    print("请输入正整数。\n")
            except ValueError:
                print("请输入数字，例如: /tokens 2048\n")
            continue
        elif user_input.startswith("/search "):
            query = user_input[len("/search "):].strip()
            if not query:
                print("请输入搜索关键词，例如: /search Python async\n")
                continue
            spinner = Spinner(f"正在搜索: {query}")
            spinner.start()
            raw_results = web_search(query)
            spinner.stop()
            # 让模型基于搜索结果生成总结
            conv.add_user(
                f"我搜索了「{query}」，请根据以下搜索结果回答我的问题。\n"
                f"要求：直接用自然语言总结要点，不要逐条罗列标题。"
                f"{source_instruction_for_tool_results(raw_results)}\n\n"
                f"{raw_results}"
            )
            mem.append_log("user", f"/search {query}")
            messages = build_messages(SYSTEM_PROMPT, conv, mem, query, model.model_name)
            try:
                reply = model.chat(
                    messages,
                    spinner_message="正在整理搜索结果",
                    next_spinner_message="正在生成回答",
                )
            except Exception as e:
                print(f"生成回复时出错: {e}\n")
                conv.history.pop()
                continue
            enriched = enrich_reply_with_sources(reply, raw_results)
            _print_appended_reply_delta(reply, enriched)
            reply = enriched
            conv.add_assistant(reply)
            mem.append_log("assistant", reply)
            continue
        elif user_input.startswith("/fetch "):
            url = user_input[len("/fetch "):].strip()
            if not url:
                print("请输入网址，例如: /fetch https://example.com\n")
                continue
            spinner = Spinner(f"正在抓取网页: {url}")
            spinner.start()
            raw_content = web_fetch(url)
            spinner.stop()
            # 让模型总结网页内容
            conv.add_user(
                f"我抓取了这个网页的内容，请帮我总结要点：\n\n{raw_content}"
            )
            mem.append_log("user", f"/fetch {url}")
            messages = build_messages(SYSTEM_PROMPT, conv, mem, url, model.model_name)
            try:
                reply = model.chat(
                    messages,
                    spinner_message="正在整理网页内容",
                    next_spinner_message="正在生成回答",
                )
            except Exception as e:
                print(f"生成回复时出错: {e}\n")
                conv.history.pop()
                continue
            enriched = enrich_reply_with_sources(reply, raw_content)
            _print_appended_reply_delta(reply, enriched)
            reply = enriched
            conv.add_assistant(reply)
            mem.append_log("assistant", reply)
            continue
        elif user_input == "/canvas":
            print(f"{get_canvas_schedule(days=DEFAULT_CANVAS_DAYS)}\n")
            continue
        elif user_input.startswith("/canvas "):
            arg = user_input[len("/canvas "):].strip()
            if arg.startswith("set-feed "):
                feed_url = arg[len("set-feed "):].strip()
                if not feed_url:
                    print("请粘贴完整的 Canvas Calendar Feed 链接。\n")
                    continue
                try:
                    save_canvas_feed_url(feed_url)
                except ValueError as e:
                    print(f"{e}\n")
                    continue
                print("Canvas Calendar Feed 已保存。之后输入 /canvas 就能查看近期作业、考试和日程。\n")
                continue
            if arg == "clear-feed":
                removed = clear_canvas_feed_url()
                if removed:
                    print("Canvas Calendar Feed 配置已清除。\n")
                else:
                    print("当前没有可清除的 Canvas Feed 配置。\n")
                continue
            if arg == "status":
                print(f"{canvas_status()}\n")
                continue
            if arg.startswith("history "):
                history_query = arg[len("history "):].strip()
                if not history_query:
                    print("请提供日期或时间描述，例如: /canvas history 2026-03-31\n")
                    continue
                print(f"{get_canvas_history_for_query(history_query)}\n")
                continue
            if arg.startswith("done "):
                completion_query = arg[len("done "):].strip()
                if not completion_query:
                    print("请提供事件描述，例如: /canvas done Journal 3\n")
                    continue
                print(f"{mark_canvas_events_completed(completion_query)}\n")
                continue
            if arg.startswith("undo "):
                undo_query = arg[len("undo "):].strip()
                if not undo_query:
                    print("请提供事件描述，例如: /canvas undo Lab 6\n")
                    continue
                print(f"{mark_canvas_events_pending(undo_query)}\n")
                continue
            try:
                days = normalize_canvas_days(arg)
            except ValueError:
                print(
                    "Canvas 用法：\n"
                    "  /canvas\n"
                    "  /canvas 7\n"
                    "  /canvas history 2026-03-31\n"
                    "  /canvas done Journal 3\n"
                    "  /canvas undo Lab 6\n"
                    "  /canvas set-feed <Calendar Feed 链接>\n"
                    "  /canvas status\n"
                    "  /canvas clear-feed\n"
                )
                continue
            print(f"{get_canvas_schedule(days=days)}\n")
            continue
        elif user_input.startswith("/sys"):
            query = user_input[len("/sys"):].strip() or "all"
            print(f"\n{get_system_status(query)}\n")
            continue
        elif user_input == "/files":
            files = list_shared_files()
            if files:
                print("\nshared_files/ 中的文件（可用 @文件名 引用）：")
                for f in files:
                    print(f"  {f}")
                print()
            else:
                print("shared_files/ 目录为空，把文件拖进去即可用 @文件名 引用。\n")
            continue
        elif user_input.startswith("/pdfmerge "):
            args = user_input[len("/pdfmerge "):].strip().split(None, 1)
            if len(args) < 2:
                print("用法: /pdfmerge <文件夹路径> <输出文件名>\n示例: /pdfmerge ~/Desktop/pdfs merged.pdf\n")
                continue
            folder_path, output_name = args
            spinner = Spinner("合并 PDF 中")
            spinner.start()
            result = pdf_merge(folder_path, output_name)
            spinner.stop()
            print(f"\n{result}\n")
            continue
        elif user_input == "/help":
            print_help()
            continue
        elif user_input.startswith("/"):
            print(f"未知命令: {user_input}，输入 /help 查看可用命令。\n")
            continue

        # ---- 对话处理 ----

        open_reference = infer_open_request(user_input)
        if open_reference is not None:
            result, opened_path = open_recent(open_reference, recent_file_paths)
            print(f"\n{result}\n")
            conv.add_user(user_input)
            conv.add_assistant(result)
            mem.append_log("user", user_input)
            mem.append_log("assistant", result)
            if opened_path and opened_path in recent_file_paths:
                recent_file_paths.remove(opened_path)
                recent_file_paths.insert(0, opened_path)
            continue

        local_search_query = infer_local_search_query(user_input)
        if local_search_query:
            spinner = Spinner(f"正在本机搜索: {local_search_query}")
            spinner.start()
            raw_search = execute_local_search(local_search_query)
            spinner.stop()
            found_paths = extract_paths_from_text(raw_search)
            if found_paths:
                recent_file_paths = found_paths
            conv.add_user(
                f"{user_input}\n\n"
                f"【系统临时注入的本机只读搜索结果，不属于长期记忆】\n{raw_search}\n\n"
                "请根据这些本机搜索结果直接回答。回答末尾注明来源：本机只读搜索工具。"
            )
            mem.append_log("user", user_input)
            messages = build_messages(SYSTEM_PROMPT, conv, mem, user_input, model.model_name)
            try:
                reply = model.chat(
                    messages,
                    spinner_message="正在整理本机搜索结果",
                    next_spinner_message="正在生成回答",
                )
            except Exception as e:
                print(f"生成回复时出错: {e}\n")
                conv.history.pop()
                continue
            enriched = enrich_reply_with_sources(reply, "【本机搜索】\n" + raw_search)
            _print_appended_reply_delta(reply, enriched)
            reply = enriched
            conv.add_assistant(reply)
            mem.append_log("assistant", reply)
            continue

        path_query = infer_path_query(user_input)
        if path_query:
            name, kind = path_query
            spinner = Spinner(f"正在查找路径: {name}")
            spinner.start()
            raw_paths = find_paths(name, kind=kind)
            spinner.stop()
            found_paths = extract_paths_from_text(raw_paths)
            if found_paths:
                recent_file_paths = found_paths
            conv.add_user(
                f"{user_input}\n\n"
                f"【系统临时注入的本机路径查找结果，不属于长期记忆】\n{raw_paths}\n\n"
                "请根据这些本机路径查找结果直接回答。回答末尾注明来源：本地路径查找工具。"
            )
            mem.append_log("user", user_input)
            messages = build_messages(SYSTEM_PROMPT, conv, mem, user_input, model.model_name)
            try:
                reply = model.chat(
                    messages,
                    spinner_message="正在整理路径结果",
                    next_spinner_message="正在生成回答",
                )
            except Exception as e:
                print(f"生成回复时出错: {e}\n")
                conv.history.pop()
                continue
            enriched = enrich_reply_with_sources(reply, "【路径查找】\n" + raw_paths)
            _print_appended_reply_delta(reply, enriched)
            reply = enriched
            conv.add_assistant(reply)
            mem.append_log("assistant", reply)
            continue

        status_query = infer_status_query(user_input)
        if status_query:
            spinner = Spinner(f"正在获取系统状态: {status_query}")
            spinner.start()
            raw_status = get_system_status(status_query)
            spinner.stop()
            conv.add_user(
                f"{user_input}\n\n"
                f"【系统临时注入的本机实时状态，不属于长期记忆】\n{raw_status}\n\n"
                "请根据这些实时状态直接回答，不要再次请求工具。回答末尾注明来源：本地系统状态工具（实时读取）。"
            )
            mem.append_log("user", user_input)
            messages = build_messages(SYSTEM_PROMPT, conv, mem, user_input, model.model_name)
            try:
                reply = model.chat(
                    messages,
                    spinner_message="正在整理系统状态",
                    next_spinner_message="正在生成回答",
                )
            except Exception as e:
                print(f"生成回复时出错: {e}\n")
                conv.history.pop()
                continue
            enriched = enrich_reply_with_sources(reply, f"【系统状态】\n{raw_status}")
            _print_appended_reply_delta(reply, enriched)
            reply = enriched
            conv.add_assistant(reply)
            mem.append_log("assistant", reply)
            continue

        if should_mark_canvas_pending(user_input):
            result = mark_canvas_events_pending(user_input)
            print(f"{result}\n")
            conv.add_user(user_input)
            conv.add_assistant(result)
            mem.append_log("user", user_input)
            mem.append_log("assistant", result)
            continue

        if should_mark_canvas_completed(user_input):
            result = mark_canvas_events_completed(user_input)
            print(f"{result}\n")
            conv.add_user(user_input)
            conv.add_assistant(result)
            mem.append_log("user", user_input)
            mem.append_log("assistant", result)
            continue

        # 检测用户输入中的文件引用（@文件名 或 [/路径]）
        file_refs = parse_file_references(user_input)
        file_context = ""
        if file_refs:
            file_context = read_all_references(file_refs)
            # 从用户输入中移除文件引用标记，保留问题本身
            user_input = strip_file_references(user_input)
            if not user_input:
                user_input = "请阅读并总结以下文件内容。"

        canvas_context = ""
        skip_memory_extract = False
        if should_auto_check_canvas(user_input):
            canvas_context = get_canvas_context_for_query(user_input, days=DEFAULT_CANVAS_DAYS)
            skip_memory_extract = True

        mem.append_log("user", user_input)

        if file_context and canvas_context:
            conv.add_user(
                f"{user_input}\n\n{file_context}\n\n"
                f"【系统临时注入的 Canvas 日程，不属于用户长期记忆】\n{canvas_context}"
            )
        elif file_context:
            conv.add_user(f"{user_input}\n\n{file_context}")
        elif canvas_context:
            conv.add_user(
                f"{user_input}\n\n"
                f"【系统临时注入的 Canvas 日程，不属于用户长期记忆】\n{canvas_context}\n\n"
                "请基于这些安排回答，并主动提醒临近 due、考试或重要日程。"
                "如果系统已经明确标注了今天/明天/后天，就直接沿用，不要自己改写时间关系。"
            )
        else:
            conv.add_user(user_input)
        messages = build_messages(SYSTEM_PROMPT, conv, mem, user_input, model.model_name)

        try:
            reply = model.chat(messages)
        except Exception as e:
            print(f"生成回复时出错: {e}\n")
            conv.history.pop()
            continue

        # 被 Esc 打断时跳过工具调用和记忆提取
        if model._interrupted:
            if reply:
                conv.add_assistant(reply)
                mem.append_log("assistant", reply)
            continue

        # 检测模型是否想调用工具
        if model._tool_detected:
            # 流式输出已停止，reply 中包含工具标记
            tool_calls = detect_tool_calls(reply)
            if tool_calls:
                skip_memory_extract = True
                # 执行工具调用
                tool_results = execute_tool_calls(tool_calls)
                found_paths = extract_paths_from_text(tool_results)
                if found_paths:
                    recent_file_paths = found_paths
                # 只把工具标记写回对话，避免把第一轮的半成品回答污染第二轮上下文
                conv.add_assistant(extract_tool_markers(reply) or reply)
                conv.add_user(
                    f"以下是你请求的工具执行结果，请据此给出完整回答。\n"
                    f"要求：直接用自然语言总结要点。不要再次请求工具。"
                    f"{source_instruction_for_tool_results(tool_results)}\n\n"
                    f"{tool_results}"
                )
                messages = build_messages(SYSTEM_PROMPT, conv, mem, user_input, model.model_name)
                try:
                    spinner_message, next_spinner_message = _tool_result_spinner_messages(tool_calls)
                    reply = model.chat(
                        messages,
                        spinner_message=spinner_message,
                        next_spinner_message=next_spinner_message,
                    )
                except Exception as e:
                    print(f"生成回复时出错: {e}\n")
                    conv.history.pop()
                    continue
                enriched = enrich_reply_with_sources(reply, tool_results)
                _print_appended_reply_delta(reply, enriched)
                reply = enriched

        conv.add_assistant(reply)
        mem.append_log("assistant", reply)

        # LLM 记忆提取：分析最近对话，自动提取值得长期记住的信息
        if not skip_memory_extract:
            try_llm_extract(model, conv.history, mem)


if __name__ == "__main__":
    main()
