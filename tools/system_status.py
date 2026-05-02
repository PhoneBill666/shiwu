"""系统状态感知工具：获取 macOS 设备的实时状态信息。

支持的查询项:
  battery   — 电量 / 是否充电
  cpu       — CPU 型号、核心数、使用率
  memory    — 内存总量、已用、使用率
  disk      — 磁盘用量
  network   — 网络接口、IP（本机 + 公网）
  foreground— 当前前台 app、窗口标题
  processes — 正在运行的关键进程（按内存排序 top 15）
  ports     — 端口占用（监听中的 TCP 端口）

调用方式:
  [STATUS:all]           — 获取全部状态
  [STATUS:cpu,memory]    — 只获取指定项（逗号分隔）
  [STATUS:port:3000]     — 精确查询某个 TCP 端口
"""

import re

from tools.shell_control import run

_VALID_KEYS = {"battery", "cpu", "memory", "disk", "network", "foreground", "processes", "ports"}
_PORT_QUERY_RE = re.compile(
    r"(?:localhost|127\.0\.0\.1)?[:：]\s*(\d{2,5})|(?:端口|port)\s*(\d{2,5})|(\d{2,5})\s*(?:端口|port)",
    re.I,
)
_STATUS_HINTS = ["系统状态", "电脑状态", "设备状态", "机器状态", "系统情况", "电脑情况"]
_KEY_HINTS = {
    "battery": ["电池", "电量", "battery"],
    "cpu": ["cpu", "处理器", "芯片", "负载"],
    "memory": ["内存", "ram", "memory"],
    "disk": ["磁盘", "硬盘", "存储", "disk"],
    "network": ["网络", "wifi", "网速", "ip", "network"],
    "foreground": ["前台", "当前窗口", "当前应用", "foreground"],
    "processes": ["进程", "process", "后台程序"],
    "ports": ["端口", "port"],
}


def _run(cmd: list[str], timeout: int = 10) -> str:
    """执行安全命令，返回 stdout（strip 后）。"""
    result = run(cmd, timeout=timeout)
    if result.timed_out:
        return "(超时)"
    if result.error:
        return f"(错误: {result.error})"
    return result.stdout.strip()


# ---- 各项采集 ----


def _get_battery() -> str:
    # pmset -g batt 输出示例:
    #   -InternalBattery-0 (id=...)	87%; charging; 1:23 remaining
    raw = _run(["pmset", "-g", "batt"])
    if not raw:
        return "电池信息不可用"

    pct_match = re.search(r"(\d+)%", raw)
    pct = pct_match.group(1) if pct_match else "未知"

    if "charging" in raw or "charged" in raw:
        status = "充电中" if "charging" in raw else "已充满"
    elif "discharging" in raw:
        status = "放电中（未接电源）"
    else:
        status = "未知"

    remain_match = re.search(r"(\d+:\d+) remaining", raw)
    remain = f", 剩余 {remain_match.group(1)}" if remain_match else ""

    return f"{pct}% — {status}{remain}"


def _get_cpu() -> str:
    # CPU 型号
    brand = _run(["sysctl", "-n", "machdep.cpu.brand_string"])
    # 核心数
    cores_physical = _run(["sysctl", "-n", "hw.physicalcpu"])
    cores_logical = _run(["sysctl", "-n", "hw.logicalcpu"])
    # CPU 使用率（top 采样 1 秒）
    top_raw_all = _run(["top", "-l", "1", "-n", "0"])
    top_raw = next(
        (line for line in top_raw_all.splitlines() if "CPU usage" in line),
        "",
    )
    if top_raw:
        # 格式: CPU usage: 12.34% user, 5.67% sys, 82.0% idle
        cpu_usage = top_raw
    else:
        cpu_usage = "无法获取"

    return f"{brand}\n  物理核心: {cores_physical}, 逻辑核心: {cores_logical}\n  {cpu_usage}"


def _get_memory() -> str:
    # 总内存
    total_bytes = _run(["sysctl", "-n", "hw.memsize"])
    try:
        total_gb = int(total_bytes) / (1024 ** 3)
    except (ValueError, TypeError):
        total_gb = 0

    # vm_stat 获取页面信息
    vm = _run(["vm_stat"])
    # 从 vm_stat 第一行获取实际 page size
    page_size = 16384  # Apple Silicon 默认 16KB
    ps_match = re.search(r"page size of (\d+) bytes", vm)
    if ps_match:
        page_size = int(ps_match.group(1))

    pages_free = pages_active = pages_inactive = pages_wired = 0

    m = re.search(r"Pages free:\s+(\d+)", vm)
    if m:
        pages_free = int(m.group(1))
    m = re.search(r"Pages active:\s+(\d+)", vm)
    if m:
        pages_active = int(m.group(1))
    m = re.search(r"Pages inactive:\s+(\d+)", vm)
    if m:
        pages_inactive = int(m.group(1))
    m = re.search(r"Pages wired down:\s+(\d+)", vm)
    if m:
        pages_wired = int(m.group(1))

    # 已用 = 活跃 + 驻留（不含压缩页，压缩页是已压缩存储的原始页面数量，
    # 实际占用远小于 原始页面数 × page_size）
    used_bytes = (pages_active + pages_wired) * page_size
    used_gb = used_bytes / (1024 ** 3)
    free_gb = pages_free * page_size / (1024 ** 3)
    inactive_gb = pages_inactive * page_size / (1024 ** 3)
    pct = (used_gb / total_gb * 100) if total_gb else 0

    return (
        f"总计 {total_gb:.1f} GB, 已用 {used_gb:.1f} GB ({pct:.1f}%), 空闲 {free_gb:.1f} GB\n"
        f"  活跃: {pages_active * page_size / (1024**3):.1f} GB, "
        f"驻留: {pages_wired * page_size / (1024**3):.1f} GB, "
        f"非活跃: {inactive_gb:.1f} GB"
    )


def _get_disk() -> str:
    raw = _run(["df", "-h", "/"])
    lines = raw.splitlines()
    if len(lines) < 2:
        return "磁盘信息不可用"
    # 第二行是根分区
    parts = lines[1].split()
    if len(parts) >= 5:
        size, used, avail = parts[1], parts[2], parts[3]
        pct = parts[4]
        return f"总计 {size}, 已用 {used} ({pct}), 可用 {avail}"
    return lines[1]


def _get_network() -> str:
    lines = []
    # 当前 Wi-Fi SSID
    ssid_raw = _run([
        "/System/Library/PrivateFrameworks/Apple80211.framework/Versions/Current/Resources/airport",
        "-I",
    ])
    ssid = next((line for line in ssid_raw.splitlines() if " SSID" in line), "")
    if ssid:
        m = re.search(r':\s*(.+)', ssid)
        if m:
            lines.append(f"Wi-Fi: {m.group(1).strip()}")

    # 本机 IP（en0）
    local_ip = _run(["ipconfig", "getifaddr", "en0"])
    if local_ip:
        lines.append(f"本机 IP (en0): {local_ip}")

    # 公网 IP
    public_ip = _run(["curl", "-s", "--connect-timeout", "5", "ifconfig.me"])
    if public_ip and not public_ip.startswith("("):
        lines.append(f"公网 IP: {public_ip}")
    else:
        lines.append("公网 IP: 获取失败（可能无外网连接）")

    # 网络连通性
    ping = _run(["ping", "-c", "1", "-t", "3", "8.8.8.8"], timeout=5)
    if "1 packets transmitted" in ping and "1 packets received" in ping:
        m = re.search(r"time=(\d+\.?\d*)", ping)
        latency = f", 延迟 {m.group(1)}ms" if m else ""
        lines.append(f"网络连通: 正常 (8.8.8.8){latency}")
    else:
        lines.append("网络连通: 不可达 (8.8.8.8)")

    return "\n  ".join(lines) if lines else "网络信息不可用"


def _get_foreground() -> str:
    # 前台 app
    app = _run([
        "osascript",
        "-e",
        'tell application "System Events" to get name of first application process whose frontmost is true',
    ])
    # 窗口标题
    title = _run([
        "osascript",
        "-e",
        'tell application "System Events" to tell (first application process whose frontmost is true) to get name of front window',
    ])
    result = f"前台应用: {app or '未知'}"
    if title and not title.startswith("("):
        result += f"\n  窗口标题: {title}"
    # 当前终端路径
    cwd = _run(["pwd"])
    if cwd:
        result += f"\n  当前路径: {cwd}"
    return result


def _get_processes() -> str:
    # 按内存排序 top 15 进程
    raw = _run(["ps", "aux"])
    lines = raw.splitlines()
    if not lines:
        return "进程信息不可用"

    header, process_lines = lines[0], lines[1:]
    def _mem_value(line: str) -> float:
        parts = line.split(None, 10)
        if len(parts) < 4:
            return 0.0
        try:
            return float(parts[3])
        except ValueError:
            return 0.0
    lines = [header, *sorted(process_lines, key=_mem_value, reverse=True)[:15]]

    procs = []
    for line in lines:
        if line.startswith("USER"):
            continue
        parts = line.split(None, 10)
        if len(parts) >= 11:
            user, pid, cpu, mem = parts[0], parts[1], parts[2], parts[3]
            cmd = parts[10][:60]
            procs.append(f"  PID {pid:>6}  CPU {cpu:>5}%  MEM {mem:>5}%  {cmd}")
    return "\n".join(procs) if procs else "进程信息不可用"


def _get_ports() -> str:
    # 监听中的 TCP 端口
    raw = _run(["lsof", "-iTCP", "-sTCP:LISTEN", "-P", "-n"])
    if not raw:
        return "没有检测到监听端口（或需要 sudo 权限）"

    lines = raw.splitlines()
    ports = []
    seen = set()
    for line in lines[1:]:  # 跳过 header
        parts = line.split()
        if len(parts) >= 9:
            cmd, pid = parts[0], parts[1]
            addr = parts[8]  # *:8080 或 127.0.0.1:3000
            key = f"{cmd}:{addr}"
            if key not in seen:
                seen.add(key)
                ports.append(f"  {cmd:<20} PID {pid:>6}  {addr}")
    return "\n".join(ports) if ports else "端口信息解析失败"


def _format_lsof_port_rows(raw: str) -> list[str]:
    rows = []
    seen = set()
    for line in raw.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 9:
            continue
        cmd, pid, user = parts[0], parts[1], parts[2]
        fd, typ = parts[3], parts[4]
        name = parts[8]
        state = parts[9].strip("()") if len(parts) > 9 else ""
        key = (cmd, pid, fd, name, state)
        if key in seen:
            continue
        seen.add(key)
        state_text = f"  {state}" if state else ""
        rows.append(f"  {cmd:<20} PID {pid:>6}  USER {user:<10}  {typ:<5} {fd:<5}  {name}{state_text}")
    return rows


def _get_port(port: str) -> str:
    if not port.isdigit():
        return f"无效端口: {port}"
    port_num = int(port)
    if port_num < 1 or port_num > 65535:
        return f"无效端口: {port}"

    listen_result = run(
        ["lsof", "-nP", f"-iTCP:{port_num}", "-sTCP:LISTEN"],
        timeout=10,
    )
    if listen_result.error:
        return f"查询 {port_num} 端口失败: {listen_result.error}"

    rows = _format_lsof_port_rows(listen_result.stdout)
    if rows:
        return f"{port_num} 端口正在被占用（监听中）：\n" + "\n".join(rows)

    any_result = run(["lsof", "-nP", f"-iTCP:{port_num}"], timeout=10)
    if any_result.error:
        return f"查询 {port_num} 端口失败: {any_result.error}"

    any_rows = _format_lsof_port_rows(any_result.stdout)
    if any_rows:
        return f"{port_num} 端口没有监听进程，但存在相关 TCP 连接：\n" + "\n".join(any_rows)

    return f"{port_num} 端口当前没有检测到监听进程。"


# ---- 主入口 ----


_COLLECTORS = {
    "battery": _get_battery,
    "cpu": _get_cpu,
    "memory": _get_memory,
    "disk": _get_disk,
    "network": _get_network,
    "foreground": _get_foreground,
    "processes": _get_processes,
    "ports": _get_ports,
}

_LABELS = {
    "battery": "🔋 电池",
    "cpu": "🧠 CPU",
    "memory": "💾 内存",
    "disk": "💿 磁盘",
    "network": "🌐 网络",
    "foreground": "🖥️ 当前前台",
    "processes": "📋 关键进程",
    "ports": "🔌 端口占用",
}


def get_system_status(query: str = "all") -> str:
    """获取系统状态信息。

    Args:
        query: "all" 或逗号分隔的 key 列表，如 "cpu,memory,battery"

    Returns:
        格式化的状态文本
    """
    query = query.strip().lower()
    port_match = re.fullmatch(r"port[:：](\d{2,5})", query)
    if port_match:
        port = port_match.group(1)
        return f"【🔌 端口 {port}】\n{_get_port(port)}"

    if query in ("all", "全部", "*"):
        keys = list(_VALID_KEYS)
    else:
        keys = [k.strip() for k in query.split(",") if k.strip() in _VALID_KEYS]
        invalid = [k.strip() for k in query.split(",") if k.strip() and k.strip() not in _VALID_KEYS]
        if not keys:
            return f"无效的查询项: {query}\n可用: {', '.join(sorted(_VALID_KEYS))}"

    sections = []
    for key in keys:
        try:
            value = _COLLECTORS[key]()
        except Exception as e:
            value = f"(采集失败: {e})"
        sections.append(f"【{_LABELS[key]}】\n{value}")

    return "\n\n".join(sections)


def infer_status_query(text: str) -> str | None:
    lowered = text.lower()
    port_match = _PORT_QUERY_RE.search(lowered)
    if port_match:
        port = next(group for group in port_match.groups() if group)
        return f"port:{port}"

    matched_keys = [key for key, hints in _KEY_HINTS.items() if any(hint in lowered for hint in hints)]
    if matched_keys:
        return ",".join(dict.fromkeys(matched_keys))
    if any(hint in text for hint in _STATUS_HINTS):
        return "all"
    return None
