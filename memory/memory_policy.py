"""长期记忆策略：类型、过滤、价值判定、置信度规则。"""

import re

SUPPORTED_MEMORY_KINDS = {
    "user_identity",
    "user_preference",
    "project_context",
    "technical_constraint",
    "assistant_identity",
    "other",
    "manual",
}

KIND_ALIASES = {
    "technical_decision": "technical_constraint",
    "user_request": "user_preference",
}

MIN_CONTENT_LENGTH = 12
RELAXED_MIN_CONTENT_LENGTH = 9

SKIP_PATTERNS = [
    re.compile(r"^[^。，！；：\.]*[？?]$"),
    re.compile(r"(记住了吗|对吗|是吗|好吗|行吗|可以吗)"),
    re.compile(r"^(不好|换一个|再换|再想|不行|算了)"),
    re.compile(r"(哈哈|开玩笑|随便说说|先这样|等会再说)"),
    re.compile(r"(你是谁|我是谁|你记得什么|你对我的了解|你都知道些什么)"),
]

OFFLINE_SKIP_PATTERNS = [
    re.compile(r"(这些内容不用分析|不用分析或是建议|正在给你补充对我的了解)"),
    re.compile(r"(继续补充|先不要分析|先别分析|先不用回复|先别回复)"),
    re.compile(r"(给你起个名字|换一个名字|再换一个)"),
    re.compile(r"(我正在测试|测试你是否能记住|能不能记住)"),
]

IDENTITY_HINTS = ["我是", "我叫", "我的名字", "我在", "我的专业", "大学", "学生"]
PREFERENCE_HINTS = [
    "偏好",
    "希望",
    "默认",
    "回答风格",
    "沟通偏好",
    "我更喜欢",
    "我不喜欢",
    "不要",
    "必须",
    "先给结论",
    "逻辑清晰",
]
PROJECT_HINTS = ["项目", "正在做", "正在开发", "本地助手", "assistant", "计划", "目标"]
TECH_HINTS = [
    "技术路线",
    "架构",
    "模型",
    "不使用",
    "必须用",
    "不要用",
    "不允许用",
    "不做GUI",
    "不做语音",
    "不联网",
    "数据库",
]

USER_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(我的沟通偏好|回答风格|我更喜欢|我不喜欢|不要官腔|不要空泛).{4,}"), "user_preference"),
    (re.compile(r"我(是|叫|在读|来自|目前是)[^？?]{6,}"), "user_identity"),
    (re.compile(r"我(具备|能够|擅长|熟悉).{6,}"), "user_identity"),
    (re.compile(r"(项目|目标是|计划是|正在做|正在开发|本地.*助手).{6,}"), "project_context"),
    (re.compile(r"(技术路线|模型支持|不使用|必须用|不要用|不允许用|不做GUI|不联网|不做语音).{3,}"), "technical_constraint"),
    (re.compile(r"我(喜欢|偏好|习惯|倾向于|不喜欢|不想用|讨厌).{3,}"), "user_preference"),
]

KIND_BASE_CONFIDENCE = {
    "user_identity": 0.88,
    "user_preference": 0.84,
    "project_context": 0.82,
    "technical_constraint": 0.90,
    "other": 0.78,
}

KIND_LABELS = {
    "user_identity": "用户身份",
    "user_preference": "用户偏好",
    "project_context": "项目背景",
    "technical_constraint": "技术约束",
    "assistant_identity": "助手身份",
    "manual": "手动记忆",
    "other": "其他",
}


def contains_any(content: str, hints: list[str]) -> bool:
    return any(h in content for h in hints)


def should_skip_online(content: str) -> bool:
    if len(content) < MIN_CONTENT_LENGTH:
        return True
    for pattern in SKIP_PATTERNS:
        if pattern.search(content):
            return True
    return False


def should_skip_offline(content: str) -> bool:
    if len(content) < RELAXED_MIN_CONTENT_LENGTH:
        return True
    if content.startswith("/"):
        return True
    if "?" in content or "？" in content:
        return True
    if re.search(r"(给我讲讲我是谁|你是谁|你叫什么|你都知道些什么)", content):
        return True
    if re.search(r"(不是我的名字|你的名字是|十五是你的名字)", content):
        return True
    for pattern in SKIP_PATTERNS:
        if pattern.search(content):
            return True
    for pattern in OFFLINE_SKIP_PATTERNS:
        if pattern.search(content):
            return True
    return False


def is_worth_long_term_online(content: str) -> bool:
    if len(content) < MIN_CONTENT_LENGTH:
        return False
    if content.count("，") + content.count(",") + content.count("。") < 1 and len(content) < 18:
        return False

    if re.search(r"(记住|别忘了|记下来)", content) and not (
        contains_any(content, IDENTITY_HINTS)
        or contains_any(content, PREFERENCE_HINTS)
        or contains_any(content, PROJECT_HINTS)
        or contains_any(content, TECH_HINTS)
    ):
        return False

    return (
        contains_any(content, IDENTITY_HINTS)
        or contains_any(content, PREFERENCE_HINTS)
        or contains_any(content, PROJECT_HINTS)
        or contains_any(content, TECH_HINTS)
    )


def is_worth_long_term_offline(content: str) -> bool:
    if len(content) < RELAXED_MIN_CONTENT_LENGTH:
        return False
    if re.search(r"(你的名字|你叫|叫你|不是我的名字)", content):
        return False
    if re.search(r"(这些内容不用分析|正在给你补充对我的了解|先别分析|继续补充)", content):
        return False
    return (
        contains_any(content, IDENTITY_HINTS)
        or contains_any(content, PREFERENCE_HINTS)
        or contains_any(content, PROJECT_HINTS)
        or contains_any(content, TECH_HINTS)
    )


def classify_kind(content: str) -> str | None:
    for pattern, kind in USER_RULES:
        if pattern.search(content):
            return kind
    if contains_any(content, TECH_HINTS):
        return "technical_constraint"
    if contains_any(content, PROJECT_HINTS):
        return "project_context"
    if contains_any(content, PREFERENCE_HINTS):
        return "user_preference"
    if contains_any(content, IDENTITY_HINTS):
        return "user_identity"
    return None


def estimate_confidence(kind: str, content: str) -> float:
    conf = KIND_BASE_CONFIDENCE.get(kind, 0.80)
    if re.search(r"(默认|长期|以后|请记住|明确|必须|不要)", content):
        conf += 0.05
    if re.search(r"(可能|大概|也许|试试|先这样)", content):
        conf -= 0.10
    return max(0.50, min(0.98, conf))


def estimate_offline_confidence(kind: str, content: str) -> float:
    return max(0.55, min(0.88, estimate_confidence(kind, content) - 0.14))


def build_description(kind: str, content: str, max_len: int = 42) -> str:
    label = KIND_LABELS.get(kind, "长期记忆")
    plain = re.sub(r"\s+", "", content.strip())
    if len(plain) > max_len:
        plain = plain[:max_len] + "..."
    return f"{label}: {plain}"


def infer_explicit(source: str) -> bool:
    return source in {"user_command", "manual", "manual_cleanup"}


def is_low_value_memory_content(content: str) -> bool:
    return bool(
        re.search(
            r"(以下的东西你都要记住|请记住我说的话|别忘了我说的话|给我讲讲我是谁|你都知道些什么|你是谁|你叫什么|十五是你的名字|不是我的名字)",
            content,
        )
    )


def fix_legacy_kind(kind: str, content: str) -> str:
    if kind == "user_identity" and re.search(r"(高标准|细节导向|偏好|沟通|回答风格|不喜欢)", content):
        return "user_preference"
    return kind
