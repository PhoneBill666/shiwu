"""Safe command execution and background task management.

This module is the low-level execution layer for tools. It does not parse
natural language and does not contain business logic.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from storage_paths import ROOT_DIR


RUNTIME_DIR = ROOT_DIR / "runtime"
TASKS_DIR = RUNTIME_DIR / "tasks"
LOGS_DIR = RUNTIME_DIR / "logs"
TASK_LOGS_DIR = LOGS_DIR / "tasks"
TASKS_FILE = TASKS_DIR / "shell_tasks.json"
MAIN_LOG_FILE = LOGS_DIR / "shell_control.log"

BLOCKED_COMMANDS = {
    "sudo",
    "su",
    "dd",
    "diskutil",
    "mkfs",
    "shutdown",
    "reboot",
    "passwd",
    "launchctl",
    "security",
}

SHELL_COMMANDS = {"sh", "bash", "zsh", "fish", "csh", "tcsh"}

CONFIRM_COMMANDS = {
    "rm",
    "mv",
    "cp",
    "chmod",
    "chown",
    "kill",
    "pkill",
    "git",
    "brew",
    "npm",
    "pnpm",
    "pip",
    "pip3",
    "uv",
}

SAFE_COMMANDS = {
    "ls",
    "pwd",
    "cat",
    "head",
    "tail",
    "wc",
    "grep",
    "find",
    "du",
    "df",
    "ps",
    "lsof",
    "which",
    "whereis",
    "whoami",
    "date",
    "open",
    "curl",
    "ping",
    "pmset",
    "sysctl",
    "top",
    "vm_stat",
    "ipconfig",
    "osascript",
    "mdfind",
    "pdfunite",
    "sort",
}


@dataclass
class CommandResult:
    ok: bool
    command: list[str]
    cwd: Optional[str]
    exit_code: Optional[int]
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool
    error: Optional[str] = None


@dataclass
class TaskInfo:
    task_id: str
    name: str
    command: list[str]
    cwd: Optional[str]
    pid: Optional[int]
    status: str
    started_at: str
    ended_at: Optional[str]
    log_file: str
    exit_code: Optional[int] = None


def ensure_runtime_dirs() -> None:
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    TASK_LOGS_DIR.mkdir(parents=True, exist_ok=True)
    if not TASKS_FILE.exists():
        TASKS_FILE.write_text("[]", encoding="utf-8")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def normalize_path(path: str, base_dir: str | Path | None = None) -> str:
    raw = Path(path).expanduser()
    if not raw.is_absolute() and base_dir is not None:
        raw = Path(base_dir).expanduser() / raw
    return str(raw.resolve())


def normalize_cwd(cwd: Optional[str]) -> Optional[str]:
    if cwd is None:
        return None
    path = Path(cwd).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"cwd does not exist: {path}")
    if not path.is_dir():
        raise NotADirectoryError(f"cwd is not a directory: {path}")
    return str(path)


def normalize_command(command: list[str], cwd: Optional[str] = None) -> list[str]:
    """Expand a leading ~/ path in command arguments without shell parsing."""
    normalized: list[str] = []
    for index, part in enumerate(command):
        if index == 0:
            normalized.append(part)
        elif part == "~" or part.startswith("~/"):
            normalized.append(normalize_path(part, cwd))
        else:
            normalized.append(part)
    return normalized


def truncate_output(text: str | bytes | None, max_chars: int = 12000) -> str:
    if text is None:
        return ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n\n...[output truncated]..."


def log_event(event: dict) -> None:
    ensure_runtime_dirs()
    payload = {"timestamp": now_iso(), **event}
    with MAIN_LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def load_tasks() -> list[TaskInfo]:
    ensure_runtime_dirs()
    try:
        raw = json.loads(TASKS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = []
    tasks: list[TaskInfo] = []
    for item in raw:
        if isinstance(item, dict):
            try:
                tasks.append(TaskInfo(**item))
            except TypeError:
                continue
    return tasks


def save_tasks(tasks: list[TaskInfo]) -> None:
    ensure_runtime_dirs()
    TASKS_FILE.write_text(
        json.dumps([asdict(task) for task in tasks], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def executable_name(command: list[str]) -> str:
    if not command:
        raise ValueError("command cannot be empty")
    return Path(command[0]).name


def _has_option(command: list[str], short_flags: str) -> bool:
    wanted = set(short_flags)
    for arg in command[1:]:
        if arg.startswith("--"):
            continue
        if arg.startswith("-") and wanted.issubset(set(arg[1:])):
            return True
    return False


def _targets_protected_path(command: list[str]) -> bool:
    protected = {
        "/",
        str(Path.home()),
        "/Users",
        "/Users/phonebill",
        "/System",
        "/Library",
        "/Applications",
        "/bin",
        "/sbin",
        "/usr",
        "/var",
        "/private",
    }
    for arg in command[1:]:
        if arg.startswith("-"):
            continue
        if arg in {"~", "~/"}:
            return True
        try:
            resolved = str(Path(arg).expanduser().resolve())
        except OSError:
            continue
        if resolved in protected:
            return True
    return False


def classify_command(command: list[str]) -> str:
    exe = executable_name(command)

    if exe in BLOCKED_COMMANDS:
        return "blocked"

    if exe in SHELL_COMMANDS:
        return "blocked"

    joined = " ".join(command)
    if "|" in command or ("|" in joined and (" sh" in joined or " bash" in joined or " zsh" in joined)):
        return "blocked"

    if exe in {"curl", "wget"} and any(arg.startswith("|") for arg in command):
        return "blocked"

    if exe == "find" and "-delete" in command:
        return "confirm"

    if exe == "rm":
        return "confirm"

    if exe in {"chmod", "chown"} and "-R" in command:
        return "confirm"

    if exe == "kill" and ("-9" in command or "-KILL" in command):
        return "confirm"

    if exe == "git":
        if "reset" in command and "--hard" in command:
            return "confirm"
        if "clean" in command and any(flag in command for flag in ("-fd", "-xdf", "-fxd")):
            return "confirm"

    if exe in {"rm", "mv", "cp", "chmod", "chown", "mkdir", "touch", "ln"} and _targets_protected_path(command):
        return "confirm"

    if exe in CONFIRM_COMMANDS:
        return "confirm"

    if exe in SAFE_COMMANDS:
        return "safe"

    return "safe"


def validate_command(
    command: list[str],
    *,
    require_confirm: bool = False,
    allow_dangerous: bool = False,
) -> None:
    if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
        raise TypeError("command must be list[str]")
    if not command:
        raise ValueError("command cannot be empty")
    if not command[0].strip():
        raise ValueError("executable cannot be empty")
    if any("\x00" in item for item in command):
        raise ValueError("command cannot contain NUL bytes")

    level = classify_command(command)
    if level == "blocked":
        raise PermissionError(f"blocked command: {command[0]}")
    if require_confirm and not allow_dangerous:
        raise PermissionError(f"command requires confirmation: {' '.join(command)}")
    if level == "confirm" and not allow_dangerous:
        raise PermissionError(
            f"command requires confirmation or allow_dangerous=True: {' '.join(command)}"
        )


def run(
    command: list[str],
    cwd: str | None = None,
    timeout: int = 30,
    env: dict[str, str] | None = None,
    require_confirm: bool = False,
    allow_dangerous: bool = False,
    max_output_chars: int = 12000,
) -> CommandResult:
    ensure_runtime_dirs()
    started = time.time()
    normalized_cwd: Optional[str] = cwd
    normalized_command = command

    try:
        normalized_cwd = normalize_cwd(cwd)
        normalized_command = normalize_command(command, normalized_cwd)
        validate_command(
            normalized_command,
            require_confirm=require_confirm,
            allow_dangerous=allow_dangerous,
        )

        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)

        completed = subprocess.run(
            normalized_command,
            cwd=normalized_cwd,
            env=merged_env,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
        )

        duration_ms = int((time.time() - started) * 1000)
        result = CommandResult(
            ok=completed.returncode == 0,
            command=normalized_command,
            cwd=normalized_cwd,
            exit_code=completed.returncode,
            stdout=truncate_output(completed.stdout, max_output_chars),
            stderr=truncate_output(completed.stderr, max_output_chars),
            duration_ms=duration_ms,
            timed_out=False,
        )
        log_event(
            {
                "type": "run",
                "command": normalized_command,
                "cwd": normalized_cwd,
                "exit_code": completed.returncode,
                "duration_ms": duration_ms,
                "ok": result.ok,
            }
        )
        return result

    except subprocess.TimeoutExpired as exc:
        duration_ms = int((time.time() - started) * 1000)
        result = CommandResult(
            ok=False,
            command=normalized_command,
            cwd=normalized_cwd,
            exit_code=None,
            stdout=truncate_output(exc.stdout, max_output_chars),
            stderr=truncate_output(exc.stderr, max_output_chars),
            duration_ms=duration_ms,
            timed_out=True,
            error=f"Command timed out after {timeout}s",
        )
        log_event(
            {
                "type": "run_timeout",
                "command": normalized_command,
                "cwd": normalized_cwd,
                "duration_ms": duration_ms,
                "ok": False,
                "error": result.error,
            }
        )
        return result

    except Exception as exc:
        duration_ms = int((time.time() - started) * 1000)
        result = CommandResult(
            ok=False,
            command=normalized_command,
            cwd=normalized_cwd,
            exit_code=None,
            stdout="",
            stderr="",
            duration_ms=duration_ms,
            timed_out=False,
            error=str(exc),
        )
        log_event(
            {
                "type": "run_error",
                "command": normalized_command,
                "cwd": normalized_cwd,
                "duration_ms": duration_ms,
                "ok": False,
                "error": str(exc),
            }
        )
        return result


def start_task(
    name: str,
    command: list[str],
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    allow_dangerous: bool = False,
) -> TaskInfo:
    ensure_runtime_dirs()
    normalized_cwd = normalize_cwd(cwd)
    normalized_command = normalize_command(command, normalized_cwd)
    validate_command(normalized_command, allow_dangerous=allow_dangerous)

    safe_name = "".join(ch if ch.isalnum() or ch in ("-", "_") else "-" for ch in name).strip("-")
    safe_name = safe_name or "task"
    task_id = f"{safe_name}-{uuid.uuid4().hex[:8]}"
    log_file = TASK_LOGS_DIR / f"{task_id}.log"

    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)

    log_handle = log_file.open("a", encoding="utf-8")
    try:
        process = subprocess.Popen(
            normalized_command,
            cwd=normalized_cwd,
            env=merged_env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
            shell=False,
            start_new_session=True,
        )
    except Exception:
        log_handle.close()
        raise
    finally:
        # The child process owns the file descriptor after fork; the parent
        # should close its handle to avoid leaking descriptors in the CLI loop.
        if not log_handle.closed:
            log_handle.close()

    task = TaskInfo(
        task_id=task_id,
        name=name,
        command=normalized_command,
        cwd=normalized_cwd,
        pid=process.pid,
        status="running",
        started_at=now_iso(),
        ended_at=None,
        log_file=str(log_file),
        exit_code=None,
    )

    tasks = load_tasks()
    tasks.append(task)
    save_tasks(tasks)
    log_event(
        {
            "type": "start_task",
            "task_id": task_id,
            "name": name,
            "command": normalized_command,
            "cwd": normalized_cwd,
            "pid": process.pid,
            "ok": True,
        }
    )
    return task


def is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def refresh_task_status(task: TaskInfo) -> TaskInfo:
    if task.status == "running" and task.pid is not None and not is_pid_alive(task.pid):
        task.status = "unknown"
        task.ended_at = now_iso()
    return task


def list_tasks() -> list[TaskInfo]:
    tasks = load_tasks()
    refreshed = [refresh_task_status(task) for task in tasks]
    save_tasks(refreshed)
    return refreshed


def find_task(task_id: str) -> TaskInfo:
    for task in load_tasks():
        if task.task_id == task_id:
            return refresh_task_status(task)
    raise KeyError(f"task not found: {task_id}")


def update_task(updated: TaskInfo) -> None:
    tasks = load_tasks()
    for index, task in enumerate(tasks):
        if task.task_id == updated.task_id:
            tasks[index] = updated
            save_tasks(tasks)
            return
    raise KeyError(f"task not found: {updated.task_id}")


def stop_task(task_id: str, force: bool = False) -> TaskInfo:
    task = find_task(task_id)
    if task.status != "running" or task.pid is None:
        update_task(task)
        return task

    try:
        sig = signal.SIGKILL if force else signal.SIGTERM
        os.killpg(task.pid, sig)
        task.status = "killed"
        task.ended_at = now_iso()
        update_task(task)
        log_event(
            {
                "type": "stop_task",
                "task_id": task_id,
                "pid": task.pid,
                "force": force,
                "ok": True,
            }
        )
        return task
    except ProcessLookupError:
        task.status = "unknown"
        task.ended_at = now_iso()
        update_task(task)
        return task
    except Exception as exc:
        log_event(
            {
                "type": "stop_task_error",
                "task_id": task_id,
                "pid": task.pid,
                "force": force,
                "ok": False,
                "error": str(exc),
            }
        )
        raise


def restart_task(task_id: str) -> TaskInfo:
    old = find_task(task_id)
    if old.status == "running":
        stop_task(task_id)
    return start_task(
        name=old.name,
        command=old.command,
        cwd=old.cwd,
        env=None,
        allow_dangerous=False,
    )


def read_task_log(task_id: str, lines: int = 80, max_chars: int = 12000) -> str:
    task = find_task(task_id)
    path = Path(task.log_file)
    if not path.exists():
        return ""
    content_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return truncate_output("\n".join(content_lines[-lines:]), max_chars)
