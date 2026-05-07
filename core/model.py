import sys
import itertools
import threading
import time
import re
import tty
import termios
import select

from mlx_lm import load, stream_generate

# 匹配工具标记 [ACTION:argument]
_TOOL_MARKER_RE = re.compile(r"\[(SEARCH|FETCH|FILE|PATH|LOCAL|PDFMERGE|CANVAS|STATUS|MAIL):([^\]]+)\]")

# DEFAULT_MODEL = "mlx-community/Qwen2.5-7B-Instruct-4bit"
DEFAULT_MODEL = "mlx-community/Qwen3.5-9B-4bit"

DEFAULT_MAX_TOKENS = 2048

AVAILABLE_MODELS = [
    "mlx-community/Qwen2.5-7B-Instruct-4bit",
    "mlx-community/Qwen3.5-9B-4bit",
]

THINK_END = "</think>"
_THINKING_MODELS = ["Qwen3"]
_EMPTY_SOURCE_SECTION_RE = re.compile(r"\n+(?:引用来源|来源)[:：]?\s*$")
_SOURCE_HEADINGS = ("引用来源：", "引用来源:", "来源：", "来源:")


class StreamPrinter:
    """Stream text while holding only a possible trailing source heading."""

    def __init__(self, silent: bool = False):
        self.silent = silent
        self.pending = ""
        self.printed_header = False

    def write(self, text: str):
        if not text:
            return
        if self.silent:
            return

        self.pending += text
        match = _EMPTY_SOURCE_SECTION_RE.search(self.pending)
        if match:
            self._emit(self.pending[:match.start()])
            self.pending = self.pending[match.start():]
            return

        last_newline = self.pending.rfind("\n")
        if last_newline == -1:
            self._emit(self.pending)
            self.pending = ""
            return

        tail = self.pending[last_newline:]
        if self._is_possible_source_heading(tail):
            self._emit(self.pending[:last_newline])
            self.pending = tail
            return

        self._emit(self.pending)
        self.pending = ""

    def finish(self):
        if self.silent:
            return
        final = _EMPTY_SOURCE_SECTION_RE.sub("", self.pending.rstrip())
        if final:
            self._emit(final)
        self.pending = ""

    def discard(self):
        self.pending = ""

    def _emit(self, text: str):
        if not text:
            return
        if not self.printed_header:
            print("\n十五: ", end="", flush=True)
            self.printed_header = True
        print(text, end="", flush=True)

    def _is_possible_source_heading(self, text: str) -> bool:
        stripped = text.strip()
        if not stripped:
            return True
        return any(heading.startswith(stripped) for heading in _SOURCE_HEADINGS)


class Spinner:
    def __init__(
        self,
        message: str = "思考中",
        next_message: str | None = None,
        next_message_after: float = 0.0,
    ):
        self._message = message
        self._next_message = next_message
        self._next_message_after = next_message_after
        self._stop = False
        self._thread = None

    def start(self):
        self._stop = False
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop = True
        if self._thread:
            self._thread.join()
        sys.stdout.write("\r\033[K")
        sys.stdout.flush()

    def _spin(self):
        started_at = time.monotonic()
        switched = False
        for frame in itertools.cycle(["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]):
            if self._stop:
                break
            if (
                self._next_message
                and not switched
                and time.monotonic() - started_at >= self._next_message_after
            ):
                self._message = self._next_message
                switched = True
            sys.stdout.write(f"\r{frame} {self._message}")
            sys.stdout.flush()
            time.sleep(0.1)


class EscListener:
    """后台线程监听 Esc 键，按下后设置标志位。"""

    def __init__(self):
        self._pressed = False
        self._stop = False
        self._thread = None
        self._old_settings = None

    @property
    def pressed(self) -> bool:
        return self._pressed

    def start(self):
        self._pressed = False
        self._stop = False
        self._thread = threading.Thread(target=self._listen, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop = True
        if self._thread:
            self._thread.join(timeout=0.3)
        # 恢复终端设置
        if self._old_settings is not None:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._old_settings)
            self._old_settings = None

    def _listen(self):
        fd = sys.stdin.fileno()
        self._old_settings = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            while not self._stop:
                if select.select([fd], [], [], 0.1)[0]:
                    ch = sys.stdin.read(1)
                    if ch == "\x1b":  # Esc
                        self._pressed = True
                        break
        except Exception:
            pass
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, self._old_settings)
            self._old_settings = None


class LocalModel:
    """MLX 本地模型封装：加载、流式生成、切换模型。"""

    def __init__(self, model_name: str = DEFAULT_MODEL):
        self.model_name = model_name
        self.max_tokens = DEFAULT_MAX_TOKENS
        self.thinking_enabled = False
        self.last_thinking: str | None = None  # 最近一次思考内容
        self._extracting = False  # 防止重入
        self._interrupted = False  # 是否被 Esc 打断
        self._load(model_name)

    def _load(self, model_name: str):
        print(f"正在加载模型: {model_name} ...")
        self.model, self.tokenizer = load(model_name)
        self.model_name = model_name
        self._has_thinking = any(k in model_name for k in _THINKING_MODELS)
        print("模型加载完成。")

    def switch(self, model_name: str):
        if model_name == self.model_name:
            print(f"当前已经是 {model_name}，无需切换。")
            return
        self._load(model_name)

    def chat(
        self,
        messages: list[dict],
        silent: bool = False,
        spinner_message: str | None = None,
        next_spinner_message: str | None = None,
        next_spinner_after: float = 0.8,
    ) -> str:
        # 决定是否启用 thinking
        use_thinking = self._has_thinking and self.thinking_enabled
        self._interrupted = False
        self._tool_detected = False  # 是否检测到工具标记

        # 从用户发送消息起就开始转圈
        if spinner_message is None:
            spinner_message = "思考中" if use_thinking else "正在生成回答"
        spinner = Spinner(
            spinner_message,
            next_message=next_spinner_message,
            next_message_after=next_spinner_after,
        )
        spinner.start()

        # 启动 Esc 监听
        esc = EscListener()
        esc.start()

        template_kwargs = {}
        if self._has_thinking:
            template_kwargs["enable_thinking"] = use_thinking

        prompt = self.tokenizer.apply_chat_template(
            messages,
            add_generation_prompt=True,
            **template_kwargs,
        )

        gen = stream_generate(
            self.model,
            self.tokenizer,
            prompt=prompt,
            max_tokens=self.max_tokens,
        )

        self.last_thinking = None
        try:
            if use_thinking:
                return self._stream_with_thinking(gen, spinner, esc, silent=silent)
            else:
                return self._stream_direct(gen, spinner, esc, silent=silent)
        finally:
            esc.stop()

    def _stream_direct(self, gen, spinner: Spinner, esc: EscListener, silent: bool = False) -> str:
        """直接流式输出，首字到达时停掉 spinner。按 Esc 可打断。
        检测到工具标记时停止输出并设置 _tool_detected。
        """
        buffer = ""
        output_buffer = ""  # 已输出的内容
        tool_pending = False  # 是否在等待工具标记闭合
        tool_start_pos = -1  # 工具标记开始位置
        printer = StreamPrinter(silent=silent)

        for resp in gen:
            if esc.pressed:
                self._interrupted = True
                if spinner:
                    spinner.stop()
                    spinner = None
                if not silent:
                    print("\n（已打断）\n")
                break

            if spinner:
                spinner.stop()
                spinner = None

            buffer += resp.text

            # 检测工具标记
            if not tool_pending:
                # 检查是否出现 `[` 开始工具标记
                bracket_pos = buffer.find("[", len(output_buffer))
                if bracket_pos != -1:
                    tool_pending = True
                    tool_start_pos = bracket_pos
                    # 输出 `[` 之前的内容
                    before_bracket = buffer[len(output_buffer):bracket_pos]
                    if before_bracket:
                        printer.write(before_bracket)
                        output_buffer += before_bracket
                    tool_text = buffer[tool_start_pos:]
                    match = _TOOL_MARKER_RE.match(tool_text)
                    if match:
                        self._tool_detected = True
                        printer.discard()
                        break
                else:
                    # 没有工具标记，正常输出
                    new_text = buffer[len(output_buffer):]
                    if new_text:
                        printer.write(new_text)
                        output_buffer = buffer
            else:
                # 正在等待工具标记闭合，检查是否匹配完成
                tool_text = buffer[tool_start_pos:]
                match = _TOOL_MARKER_RE.match(tool_text)
                if match:
                    # 匹配到完整工具标记，停止输出
                    self._tool_detected = True
                    # 输出工具标记之前的内容（已经在上面输出过了）
                    break
                elif "]" in tool_text:
                    # 有 `]` 但不是工具标记，恢复输出
                    tool_pending = False
                    tool_start_pos = -1
                    # 输出之前暂停的内容
                    new_text = buffer[len(output_buffer):]
                    if new_text:
                        printer.write(new_text)
                        output_buffer = buffer
                # else: 继续等待更多 token

        if spinner:
            spinner.stop()
        if not self._interrupted and not self._tool_detected:
            printer.finish()
        if not self._interrupted and not silent and printer.printed_header:
            print("\n")
        return buffer.strip()

    def generate_silent(self, messages: list[dict], max_tokens: int = 512) -> str:
        """静默生成：不打印、不思考，用于后台任务（如记忆提取）。"""
        template_kwargs = {}
        if self._has_thinking:
            template_kwargs["enable_thinking"] = False

        prompt = self.tokenizer.apply_chat_template(
            messages,
            add_generation_prompt=True,
            **template_kwargs,
        )

        result = ""
        for resp in stream_generate(
            self.model,
            self.tokenizer,
            prompt=prompt,
            max_tokens=max_tokens,
        ):
            result += resp.text
        return result.strip()

    def _stream_with_thinking(self, gen, spinner: Spinner, esc: EscListener, silent: bool = False) -> str:
        """thinking 阶段转圈，回答阶段流式输出。按 Esc 可打断。
        检测到工具标记时停止输出并设置 _tool_detected。
        """
        buffer = ""
        found_end = False
        output_buffer = ""  # 已输出的内容（仅回答部分）
        tool_pending = False  # 是否在等待工具标记闭合
        tool_start_pos = -1  # 工具标记开始位置（在 buffer 中）
        printer = StreamPrinter(silent=silent)

        for resp in gen:
            if esc.pressed:
                self._interrupted = True
                if spinner:
                    spinner.stop()
                # 保存已有的思考内容
                thinking_text = buffer
                for tag in ["<think>", "<think>\n"]:
                    if thinking_text.startswith(tag):
                        thinking_text = thinking_text[len(tag):]
                if THINK_END in thinking_text:
                    thinking_text = thinking_text[:thinking_text.index(THINK_END)]
                self.last_thinking = thinking_text.strip() or None
                if not silent:
                    print("\n（已打断）\n")
                answer_start = buffer.index(THINK_END) + len(THINK_END) if found_end else len(buffer)
                return buffer[answer_start:].strip()

            buffer += resp.text

            if not found_end and THINK_END in buffer:
                found_end = True
                spinner.stop()
                spinner = None
                answer_start = buffer.index(THINK_END) + len(THINK_END)
                answer_so_far = buffer[answer_start:].lstrip()
                if answer_so_far:
                    printer.write(answer_so_far)
                output_buffer = buffer[:answer_start] + answer_so_far
                continue

            if found_end:
                # 回答阶段，检测工具标记
                if not tool_pending:
                    # 检查是否出现 `[` 开始工具标记
                    bracket_pos = buffer.find("[", len(output_buffer))
                    if bracket_pos != -1:
                        tool_pending = True
                        tool_start_pos = bracket_pos
                        # 输出 `[` 之前的内容
                        before_bracket = buffer[len(output_buffer):bracket_pos]
                        if before_bracket:
                            printer.write(before_bracket)
                            output_buffer += before_bracket
                        tool_text = buffer[tool_start_pos:]
                        match = _TOOL_MARKER_RE.match(tool_text)
                        if match:
                            self._tool_detected = True
                            printer.discard()
                            break
                    else:
                        # 没有工具标记，正常输出
                        new_text = buffer[len(output_buffer):]
                        if new_text:
                            printer.write(new_text)
                            output_buffer = buffer
                else:
                    # 正在等待工具标记闭合，检查是否匹配完成
                    tool_text = buffer[tool_start_pos:]
                    match = _TOOL_MARKER_RE.match(tool_text)
                    if match:
                        # 匹配到完整工具标记，停止输出
                        self._tool_detected = True
                        break
                    elif "]" in tool_text:
                        # 有 `]` 但不是工具标记，恢复输出
                        tool_pending = False
                        tool_start_pos = -1
                        # 输出之前暂停的内容
                        new_text = buffer[len(output_buffer):]
                        if new_text:
                            printer.write(new_text)
                            output_buffer = buffer
                    # else: 继续等待更多 token

        if not found_end:
            # thinking 没闭合，token 用完了
            spinner.stop()
            thinking_text = buffer
            for tag in ["<think>", "<think>\n"]:
                if thinking_text.startswith(tag):
                    thinking_text = thinking_text[len(tag):]
            self.last_thinking = thinking_text.strip()
            if not silent:
                print("\n十五: （思考未完成，用 /thinking 查看思考内容，/tokens <数量> 增大上限）\n")
            return ""

        if not self._tool_detected:
            printer.finish()
        if not silent and printer.printed_header:
            print("\n")
        answer_start = buffer.index(THINK_END) + len(THINK_END)
        think_start = 0
        for tag in ["<think>", "<think>\n"]:
            if buffer.startswith(tag):
                think_start = len(tag)
                break
        self.last_thinking = buffer[think_start:buffer.index(THINK_END)].strip()
        return buffer[answer_start:].strip()
