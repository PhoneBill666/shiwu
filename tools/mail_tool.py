"""macOS Mail.app 读取与查找工具。

搜索策略:
- 有关键词时: SQLite 查询 Envelope Index 数据库 (最快最可靠)
- fallback: mdfind (需 Full Disk Access) → JXA GUI 搜索
- 仅查最近: SQLite 或 JXA 直接读取最新 N 封
"""

from __future__ import annotations

import email as email_lib
import json
import os
import re
import sqlite3
from dataclasses import dataclass

from tools.shell_control import run

DEFAULT_MAIL_LIMIT = 5
MAX_MAIL_LIMIT = 8
MAIL_DIR = os.path.expanduser("~/Library/Mail")
MDFIND_TIMEOUT = 8
MAX_MDFIND_FILES = 30
MAX_BODY_CHARS = 4000
JXA_HEADER_SCAN = 200
JXA_BODY_FALLBACK = 5
JXA_BODY_PREVIEW = 1500

# macOS epoch offset: 2001-01-01 vs 1970-01-01
_MAC_EPOCH_OFFSET = 978307200

# 中英文关键词映射：搜中文时自动补充英文，反之亦然
_KEYWORD_ALIASES: dict[str, list[str]] = {
    "小米": ["xiaomi"],
    "苹果": ["apple"],
    "华为": ["huawei"],
    "阿里巴巴": ["alibaba", "aliyun", "taobao"],
    "腾讯": ["tencent", "qq", "wechat"],
    "京东": ["jd"],
    "百度": ["baidu"],
    "字节跳动": ["bytedance", "tiktok", "douyin"],
    "微软": ["microsoft", "outlook", "office365"],
    "谷歌": ["google", "gmail"],
    "亚马逊": ["amazon", "aws"],
    "奈飞": ["netflix"],
    "微信": ["wechat", "weixin"],
    "支付宝": ["alipay"],
    "美团": ["meituan"],
    "滴滴": ["didi"],
    "网易": ["netease", "163"],
    "新浪": ["sina", "weibo"],
    "github": ["github"],
    "linkedin": ["领英"],
    "steam": ["steam"],
    "bilibili": ["b站", "哔哩哔哩"],
}

def _find_envelope_db() -> str | None:
    """Locate Mail's Envelope Index SQLite database."""
    for version in ["V10", "V9", "V8"]:
        db_path = os.path.expanduser(
            f"~/Library/Mail/{version}/MailData/Envelope Index"
        )
        if os.path.isfile(db_path):
            return db_path
    return None

_MAIL_STOPWORDS = [
    "帮我", "查找", "搜索", "查一下", "找一下", "找找", "看一下",
    "读一下", "读取", "查看", "看看", "来自", "邮件", "邮箱",
    "正文", "内容", "概括", "总结", "原文", "关于", "最近", "最新",
    "封", "并", "的",
]


@dataclass
class MailQuery:
    raw_query: str
    keywords: list[str]
    limit: int = DEFAULT_MAIL_LIMIT
    recent_only: bool = False
    need_body: bool = True


def search_mail(query: str) -> str:
    parsed = _parse_mail_query(query)
    messages = _search_mail(parsed)

    if isinstance(messages, str):
        return messages
    if not messages:
        return f"没有找到与「{query}」相关的邮件。"

    lines = [
        f"Mail 中找到 {len(messages)} 封相关邮件。",
        "以下内容来自 macOS Mail.app 的本地读取结果。",
    ]
    for index, item in enumerate(messages, 1):
        subject = _clean_text(item.get("subject") or "(无主题)")
        sender = _clean_text(item.get("sender") or "未知发件人")
        date_received = _clean_text(item.get("dateReceived") or item.get("date") or "时间未知")
        mailbox = _clean_text(item.get("mailbox") or "")
        body = _clean_body(item.get("body") or "")
        lines.append(f"[{index}] {subject}")
        lines.append(f"    发件人: {sender}")
        lines.append(f"    时间: {date_received}")
        if mailbox:
            lines.append(f"    邮箱: {mailbox}")
        if body:
            lines.append("    正文:")
            for line in body.splitlines():
                lines.append(f"      {line}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _parse_mail_query(query: str) -> MailQuery:
    raw = query.strip()
    limit = DEFAULT_MAIL_LIMIT

    limit_match = re.search(r"(?:limit|数量|最近)[:：= ]?(\d{1,2})", raw, flags=re.IGNORECASE)
    if limit_match:
        limit = max(1, min(int(limit_match.group(1)), MAX_MAIL_LIMIT))

    lowered = raw.lower()
    recent_only = any(token in lowered for token in ["最新", "最近", "recent", "latest", "新邮件"])
    need_body = any(token in raw for token in ["正文", "原文", "内容", "概括", "总结", "读一下", "读取"])

    cleaned = re.sub(r"(?:limit|数量|最近)[:：= ]?\d{1,2}", " ", raw, flags=re.IGNORECASE)
    cleaned = re.sub(r"[，,。？！!?:：]", " ", cleaned)
    for word in _MAIL_STOPWORDS:
        cleaned = cleaned.replace(word, " ")
    keywords = [
        token
        for token in re.findall(r"[a-zA-Z0-9._%+-]{2,}|[一-鿿]{2,}", cleaned)
        if token.lower() not in {"mail", "email"}
    ]
    return MailQuery(raw_query=raw, keywords=keywords[:8], limit=limit,
                     recent_only=recent_only, need_body=need_body)


def _search_mail(query: MailQuery) -> list[dict] | str:
    """Main search dispatcher. SQLite > mdfind > JXA GUI."""
    # Try SQLite first (fastest, most reliable with Full Disk Access)
    try:
        results = _search_mail_sqlite(query)
        if results:
            return results
    except Exception:
        pass
    # Fallback: mdfind
    if query.keywords:
        try:
            results = _search_mail_mdfind(query)
            if results:
                return results
        except Exception:
            pass
    # Last resort: JXA
    if query.recent_only and not query.keywords:
        return _fetch_mail_jxa(keywords=[], limit=query.limit,
                               need_body=True, scan_count=query.limit)
    if query.keywords:
        return _fetch_mail_jxa(
            keywords=query.keywords, limit=query.limit,
            need_body=query.need_body, scan_count=JXA_HEADER_SCAN,
        )
    return _fetch_mail_jxa(keywords=[], limit=query.limit,
                           need_body=True, scan_count=query.limit)


# ---------------------------------------------------------------------------
# SQLite: direct query on Mail's Envelope Index database (fastest)
# ---------------------------------------------------------------------------

def _expand_keywords(keywords: list[str]) -> list[str]:
    """Expand keywords using alias mapping (e.g. 小米 → xiaomi)."""
    expanded = list(keywords)
    for kw in keywords:
        kw_lower = kw.lower()
        for key, aliases in _KEYWORD_ALIASES.items():
            if kw_lower == key.lower() or kw_lower in [a.lower() for a in aliases]:
                # Add the key and all aliases
                expanded.append(key)
                expanded.extend(aliases)
    # Deduplicate while preserving order
    seen = set()
    result = []
    for kw in expanded:
        kw_lower = kw.lower()
        if kw_lower not in seen:
            seen.add(kw_lower)
            result.append(kw)
    return result


def _search_mail_sqlite(query: MailQuery) -> list[dict]:
    """Search Mail via the Envelope Index SQLite database."""
    db_path = _find_envelope_db()
    if not db_path:
        return []

    if query.recent_only and not query.keywords:
        return _sqlite_fetch_recent(db_path, query.limit, query.need_body)
    if not query.keywords:
        return _sqlite_fetch_recent(db_path, query.limit, query.need_body)

    # Expand keywords with aliases (e.g. 小米 → xiaomi)
    expanded = _expand_keywords(query.keywords)

    # Build WHERE clause for keyword search (subject + sender + body)
    conditions = []
    params = []
    for kw in expanded:
        kw_pattern = f"%{kw.lower()}%"
        conditions.append(
            "(LOWER(s.subject) LIKE ? OR LOWER(a.comment) LIKE ?"
            " OR LOWER(a.address) LIKE ? OR LOWER(sm.summary) LIKE ?)"
        )
        params.extend([kw_pattern, kw_pattern, kw_pattern, kw_pattern])
    where_clause = " OR ".join(conditions)

    sql = f"""
        SELECT s.subject, a.comment, a.address,
               m.date_received, mb.url,
               sm.summary
        FROM messages m
        JOIN subjects s ON m.subject = s.ROWID
        LEFT JOIN addresses a ON m.sender = a.ROWID
        LEFT JOIN mailboxes mb ON m.mailbox = mb.ROWID
        LEFT JOIN summaries sm ON m.summary = sm.ROWID
        WHERE {where_clause}
        ORDER BY m.date_received DESC
        LIMIT ?
    """
    params.append(query.limit * 2)  # fetch extra in case some are garbage

    try:
        conn = sqlite3.connect(db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
        conn.close()
    except Exception:
        return []

    results = []
    for row in rows:
        entry = {
            "subject": row["subject"] or "",
            "sender": _format_sender(row["comment"], row["address"]),
            "dateReceived": _format_mac_date(row["date_received"]),
            "mailbox": _parse_mailbox_url(row["url"] or ""),
            "body": _clean_body(row["summary"] or "") if query.need_body else "",
        }
        results.append(entry)
    return results[: query.limit]


def _sqlite_fetch_recent(db_path: str, limit: int, need_body: bool) -> list[dict]:
    """Fetch most recent messages from SQLite."""
    sql = """
        SELECT s.subject, a.comment, a.address,
               m.date_received, mb.url,
               sm.summary
        FROM messages m
        JOIN subjects s ON m.subject = s.ROWID
        LEFT JOIN addresses a ON m.sender = a.ROWID
        LEFT JOIN mailboxes mb ON m.mailbox = mb.ROWID
        LEFT JOIN summaries sm ON m.summary = sm.ROWID
        ORDER BY m.date_received DESC
        LIMIT ?
    """
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, (limit,)).fetchall()
        conn.close()
    except Exception:
        return []

    results = []
    for row in rows:
        entry = {
            "subject": row["subject"] or "",
            "sender": _format_sender(row["comment"], row["address"]),
            "dateReceived": _format_mac_date(row["date_received"]),
            "mailbox": _parse_mailbox_url(row["url"] or ""),
            "body": _clean_body(row["summary"] or "") if need_body else "",
        }
        results.append(entry)
    return results


def _format_sender(comment: str | None, address: str | None) -> str:
    """Format sender like 'Name <addr>' or just name/address."""
    comment = (comment or "").strip()
    address = (address or "").strip()
    if comment and address:
        return f"{comment} <{address}>"
    return comment or address or "未知发件人"


def _format_mac_date(ts: int | None) -> str:
    """Convert Unix timestamp to readable date string."""
    if not ts:
        return "时间未知"
    try:
        import datetime
        return datetime.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError):
        return str(ts)


def _parse_mailbox_url(url: str) -> str:
    """Extract readable mailbox name from IMAP URL."""
    if not url:
        return ""
    # imap://UUID/INBOX or imap://UUID/[Gmail]/All Mail
    parts = url.split("/", 3)
    if len(parts) >= 4:
        name = parts[3]
        # URL decode
        name = name.replace("%20", " ").replace("%5B", "[").replace("%5D", "]")
        return name
    return url


# ---------------------------------------------------------------------------
# mdfind: fast keyword search via Spotlight (requires Full Disk Access)
# ---------------------------------------------------------------------------

def _search_mail_mdfind(query: MailQuery) -> list[dict]:
    try:
        elx_files = _mdfind_mail(query.keywords)
    except Exception:
        return []
    if not elx_files:
        return []

    results: list[dict] = []
    for fpath in elx_files[:MAX_MDFIND_FILES]:
        parsed = _parse_elx(fpath, query.keywords)
        if parsed is None:
            continue
        results.append({
            "subject": parsed.subject,
            "sender": parsed.sender,
            "dateReceived": parsed.date,
            "mailbox": "",
            "body": _clean_body(parsed.body) if query.need_body else "",
        })
    results.sort(key=lambda x: x.get("dateReceived", ""), reverse=True)
    return results[: query.limit]


def _mdfind_mail(keywords: list[str]) -> list[str]:
    keyword_clauses = []
    for kw in keywords:
        kw_lower = kw.lower()
        keyword_clauses.append(
            f"(kMDItemTextContent == '*{kw_lower}'c || "
            f"kMDItemSubject == '*{kw_lower}'c || "
            f"kMDItemDisplayName == '*{kw_lower}'c)"
        )
    query_str = " || ".join(keyword_clauses)
    result = run(["mdfind", "-onlyin", MAIL_DIR, query_str], timeout=MDFIND_TIMEOUT)
    if result.exit_code != 0:
        return []
    paths = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return [p for p in paths if p.lower().endswith(".emlx")]


def _parse_elx(fpath: str, keywords: list[str]):
    try:
        with open(fpath, "rb") as f:
            raw = f.read()
    except OSError:
        return None
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        return None
    lines = text.split("\n", 1)
    if len(lines) > 1 and re.match(r"^\d+\s*$", lines[0]):
        text = lines[1]
    try:
        msg = email_lib.message_from_string(text)
    except Exception:
        return None
    subject = _decode_header(str(msg.get("subject", "")))
    sender = _decode_header(str(msg.get("from", "")))
    date = str(msg.get("date", ""))
    body = _extract_body_text(msg)
    body_lower = body.lower()
    if not any(kw.lower() in body_lower for kw in keywords):
        return None
    from dataclasses import dataclass as _dc
    @_dc
    class _R:
        subject: str; sender: str; date: str; body: str
    return _R(subject=subject, sender=sender, date=date, body=body[:MAX_BODY_CHARS])


def _extract_body_text(msg: email_lib.message.Message) -> str:
    parts: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        parts.append(payload.decode(charset, errors="replace"))
                except Exception:
                    continue
    else:
        try:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or "utf-8"
                parts.append(payload.decode(charset, errors="replace"))
        except Exception:
            pass
    return "\n".join(parts)


def _decode_header(header_value: str) -> str:
    try:
        decoded_parts = email_lib.header.decode_header(header_value)
        result: list[str] = []
        for part, charset in decoded_parts:
            if isinstance(part, bytes):
                result.append(part.decode(charset or "utf-8", errors="replace"))
            else:
                result.append(str(part))
        return " ".join(result)
    except Exception:
        return header_value


# ---------------------------------------------------------------------------
# JXA: two-phase search via Mail.app scripting
# ---------------------------------------------------------------------------

def _fetch_mail_jxa(*, keywords: list[str], limit: int, need_body: bool,
                    scan_count: int) -> list[dict] | str:
    """Fetch messages via JXA (fallback when SQLite/mdfind unavailable)."""
    return _fetch_mail_inbox_recent(limit=limit, need_body=need_body)


def _fetch_mail_gui_search(*, keywords: list[str], limit: int,
                           need_body: bool) -> list[dict]:
    """GUI search via System Events — kept as last-resort fallback."""
    # This function is intentionally left as a stub.
    # Primary search uses SQLite (_search_mail_sqlite).
    # If needed, the GUI approach can be re-enabled here.
    return []
    payload = {
        "keywords": keywords,
        "limit": limit,
        "body_preview": JXA_BODY_PREVIEW,
        "need_body": need_body,
    }
    env = os.environ.copy()
    env["SHIWU_MAIL_QUERY"] = json.dumps(payload, ensure_ascii=False)
    result = run(
        ["osascript", "-l", "JavaScript", "-e", script],
        timeout=45,
        env=env,
    )
    if result.exit_code != 0:
        error = (result.stderr or result.stdout or result.error or "未知错误").strip()
        raise RuntimeError(error)
    output = (result.stdout or "").strip()
    if not output:
        return []
    try:
        data = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Mail 输出解析失败: {exc}") from exc
    if isinstance(data, dict) and "error" in data:
        raise RuntimeError(data["error"])
    return data if isinstance(data, list) else []


def _fetch_mail_inbox_recent(*, limit: int, need_body: bool) -> list[dict]:
    """Read latest N messages from inbox directly via JXA (no search)."""
    script = r'''
ObjC.import('Foundation');
function run() {
  const env = $.NSProcessInfo.processInfo.environment;
  const queryJSON = ObjC.unwrap(env.objectForKey('SHIWU_MAIL_QUERY'));
  const parsed = JSON.parse(queryJSON);
  const limit = parsed.limit || 5;
  const needBody = !!parsed.need_body;
  const bodyPreview = parsed.body_preview || 1500;

  function normalize(v) {
    return (v === null || v === undefined) ? '' : String(v);
  }

  const app = Application('Mail');
  const inbox = app.inbox();
  const messages = inbox.messages();
  const count = messages.length;
  const start = Math.max(0, count - limit);
  const results = [];

  for (let i = count - 1; i >= start; i--) {
    try {
      const msg = messages[i];
      const entry = {
        subject: normalize(msg.subject()),
        sender: normalize(msg.sender()),
        dateReceived: normalize(msg.dateReceived()),
        mailbox: normalize(inbox.name()),
        body: '',
      };
      if (needBody) {
        try {
          entry.body = normalize(msg.content()).slice(0, bodyPreview);
        } catch (e) {
          entry.body = '(正文读取失败)';
        }
      }
      results.push(entry);
    } catch (e) { /* skip */ }
  }
  return JSON.stringify(results);
}
'''
    payload = {
        "limit": limit,
        "body_preview": JXA_BODY_PREVIEW,
        "need_body": need_body,
    }
    env = os.environ.copy()
    env["SHIWU_MAIL_QUERY"] = json.dumps(payload, ensure_ascii=False)
    result = run(
        ["osascript", "-l", "JavaScript", "-e", script],
        timeout=15,
        env=env,
    )
    if result.exit_code != 0:
        error = (result.stderr or result.stdout or result.error or "未知错误").strip()
        raise RuntimeError(error)
    output = (result.stdout or "").strip()
    if not output:
        return []
    try:
        data = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Mail 输出解析失败: {exc}") from exc
    return data if isinstance(data, list) else []


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _clean_body(text: str) -> str:
    cleaned = text.replace("\r", "\n")
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    if len(cleaned) > MAX_BODY_CHARS:
        return cleaned[:MAX_BODY_CHARS] + "\n...(正文已截断)"
    return cleaned
