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
from tools.auto_tool import detect_tool_calls, execute_tool_calls
from tools.file_reader import parse_file_references, strip_file_references, read_all_references, list_shared_files
from tools.pdf_tools import pdf_merge

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
    "/files": "列出 shared_files/ 中可用 @引用 的文件",
    "/pdfmerge <文件夹> <输出名>": "合并文件夹内所有 PDF",
    "/help": "显示可用命令",
}


def print_help():
    print("\n可用命令：")
    for cmd, desc in COMMANDS.items():
        print(f"  {cmd:20s} {desc}")
    print()


def main():
    model = LocalModel()
    conv = Conversation()
    mem = MemoryStore()
    input_history = InMemoryHistory()

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
                f"\n  重复跳过: {stats['duplicates']}\n"
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
            spinner = Spinner("搜索中")
            spinner.start()
            raw_results = web_search(query)
            spinner.stop()
            # 让模型基于搜索结果生成总结
            conv.add_user(
                f"我搜索了「{query}」，请根据以下搜索结果回答我的问题。\n"
                f"要求：直接用自然语言总结要点，不要逐条罗列标题。"
                f"回答末尾附上引用来源，格式为：链接 (简短描述)\n\n"
                f"{raw_results}"
            )
            mem.append_log("user", f"/search {query}")
            messages = build_messages(SYSTEM_PROMPT, conv, mem, query)
            try:
                reply = model.chat(messages)
            except Exception as e:
                print(f"生成回复时出错: {e}\n")
                conv.history.pop()
                continue
            conv.add_assistant(reply)
            mem.append_log("assistant", reply)
            continue
        elif user_input.startswith("/fetch "):
            url = user_input[len("/fetch "):].strip()
            if not url:
                print("请输入网址，例如: /fetch https://example.com\n")
                continue
            spinner = Spinner("抓取中")
            spinner.start()
            raw_content = web_fetch(url)
            spinner.stop()
            # 让模型总结网页内容
            conv.add_user(
                f"我抓取了这个网页的内容，请帮我总结要点：\n\n{raw_content}"
            )
            mem.append_log("user", f"/fetch {url}")
            messages = build_messages(SYSTEM_PROMPT, conv, mem, url)
            try:
                reply = model.chat(messages)
            except Exception as e:
                print(f"生成回复时出错: {e}\n")
                conv.history.pop()
                continue
            conv.add_assistant(reply)
            mem.append_log("assistant", reply)
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

        # 检测用户输入中的文件引用（@文件名 或 [/路径]）
        file_refs = parse_file_references(user_input)
        file_context = ""
        if file_refs:
            file_context = read_all_references(file_refs)
            # 从用户输入中移除文件引用标记，保留问题本身
            user_input = strip_file_references(user_input)
            if not user_input:
                user_input = "请阅读并总结以下文件内容。"

        mem.append_log("user", user_input)

        if file_context:
            conv.add_user(f"{user_input}\n\n{file_context}")
        else:
            conv.add_user(user_input)
        messages = build_messages(SYSTEM_PROMPT, conv, mem, user_input)

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
        tool_calls = detect_tool_calls(reply)
        if tool_calls:
            # 执行工具调用
            tool_results = execute_tool_calls(tool_calls)
            # 把第一轮回复（含标记）和工具结果都加入对话
            conv.add_assistant(reply)
            conv.add_user(
                f"以下是你请求的工具执行结果，请据此给出完整回答。\n"
                f"要求：直接用自然语言总结要点，回答末尾附上引用来源链接。\n\n"
                f"{tool_results}"
            )
            messages = build_messages(SYSTEM_PROMPT, conv, mem, user_input)
            try:
                reply = model.chat(messages)
            except Exception as e:
                print(f"生成回复时出错: {e}\n")
                conv.history.pop()
                continue

        conv.add_assistant(reply)
        mem.append_log("assistant", reply)

        # LLM 记忆提取：分析最近对话，自动提取值得长期记住的信息
        try_llm_extract(model, conv.history, mem)


if __name__ == "__main__":
    main()
