"""联网工具：搜索 + 网页抓取。

搜索用 DuckDuckGo HTML（直接 httpx 请求，零额外依赖）。
抓取用 httpx + html2text（若有）或正则清洗。
"""

import re
import httpx

# ---- 配置 ----

SEARCH_MAX_RESULTS = 6
FETCH_TIMEOUT = 15
FETCH_MAX_CHARS = 8000
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": USER_AGENT}

# html2text 可选依赖
try:
    import html2text as _h2t_mod

    _h2t = _h2t_mod.HTML2Text()
    _h2t.ignore_links = False
    _h2t.ignore_images = True
    _h2t.ignore_emphasis = False
    _h2t.body_width = 0
    _HAS_H2T = True
except ImportError:
    _HAS_H2T = False


def _html_to_text(html: str) -> str:
    """HTML 转纯文本，优先用 html2text，否则用正则清洗。"""
    if _HAS_H2T:
        return _h2t.handle(html)
    # 正则 fallback
    text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&#\d+;", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# ---- 搜索 ----


def _parse_ddg_html(html: str, max_results: int) -> list[dict]:
    """从 DuckDuckGo HTML 搜索结果页解析标题、链接、摘要。"""
    results = []

    # DuckDuckGo HTML 版本的结果在 <a class="result__a"> 和 <a class="result__snippet">
    # 但格式经常变。用多种 pattern 尝试。

    # Pattern 1: HTML lite 版本（class 和 href 顺序不固定）
    blocks = re.findall(
        r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
        html,
        re.DOTALL,
    )
    if not blocks:
        # href 在前的情况
        blocks = re.findall(
            r'<a[^>]*href="([^"]+)"[^>]*class="[^"]*result__a[^"]*"[^>]*>(.*?)</a>',
            html,
            re.DOTALL,
        )

    snippets = re.findall(
        r'class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</(?:a|td|div|span)>',
        html,
        re.DOTALL,
    )

    for i, (url, title_html) in enumerate(blocks[:max_results]):
        title = re.sub(r"<[^>]+>", "", title_html).strip()
        # DuckDuckGo lite 的 URL 是重定向链接，提取真实 URL
        real_url = url
        ud_match = re.search(r"uddg=([^&]+)", url)
        if ud_match:
            from urllib.parse import unquote
            real_url = unquote(ud_match.group(1))

        snippet = ""
        if i < len(snippets):
            snippet = re.sub(r"<[^>]+>", "", snippets[i]).strip()

        # 过滤 DuckDuckGo 广告链接
        if not title or not real_url:
            continue
        if "duckduckgo.com/y.js" in real_url:
            continue
        results.append({"title": title, "href": real_url, "body": snippet})

    # Pattern 2: 如果 pattern 1 没匹配到，尝试更通用的解析
    if not results:
        # 匹配所有超链接 + 后续文本
        links = re.findall(
            r'<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>',
            html,
            re.DOTALL,
        )
        seen_urls = set()
        for url, title_html in links:
            if "duckduckgo.com" in url:
                continue
            if url in seen_urls:
                continue
            seen_urls.add(url)
            title = re.sub(r"<[^>]+>", "", title_html).strip()
            if title and len(title) > 5:
                results.append({"title": title, "href": url, "body": ""})
            if len(results) >= max_results:
                break

    return results


def web_search(query: str, max_results: int = SEARCH_MAX_RESULTS) -> str:
    """用 DuckDuckGo 搜索，返回格式化的结果文本。"""
    try:
        # 使用 DuckDuckGo HTML lite 版本（最稳定，不需要 JS）
        resp = httpx.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers=HEADERS,
            timeout=15,
            follow_redirects=True,
        )
        resp.raise_for_status()
    except Exception as e:
        return f"搜索失败: {e}"

    results = _parse_ddg_html(resp.text, max_results)

    if not results:
        return f"没有找到关于「{query}」的结果。"

    lines = [f"搜索「{query}」的结果（共 {len(results)} 条）：\n"]
    for i, r in enumerate(results, 1):
        lines.append(f"[{i}] {r['title']}")
        lines.append(f"    {r['href']}")
        if r.get("body"):
            lines.append(f"    {r['body']}")
        lines.append("")
    lines.append("提示: 用 /fetch <url> 可以查看某条结果的完整内容。")
    return "\n".join(lines)


# ---- 抓取 ----


def web_fetch(url: str, max_chars: int = FETCH_MAX_CHARS) -> str:
    """抓取网页内容，转为纯文本返回。"""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    try:
        resp = httpx.get(
            url,
            timeout=FETCH_TIMEOUT,
            follow_redirects=True,
            headers=HEADERS,
        )
        resp.raise_for_status()
    except httpx.TimeoutException:
        return f"抓取超时（{FETCH_TIMEOUT}s）: {url}"
    except httpx.HTTPStatusError as e:
        return f"HTTP 错误 {e.response.status_code}: {url}"
    except Exception as e:
        return f"抓取失败: {e}"

    content_type = resp.headers.get("content-type", "")

    if "html" not in content_type and "text" not in content_type:
        size_kb = len(resp.content) / 1024
        return f"非文本内容 ({content_type}, {size_kb:.1f}KB): {url}"

    text = _html_to_text(resp.text)

    # 清理空行
    lines = [line for line in text.splitlines() if line.strip()]
    text = "\n".join(lines)

    truncated = ""
    if len(text) > max_chars:
        text = text[:max_chars]
        truncated = (
            f"\n\n（内容已截断，原文 {len(resp.text)} 字符，"
            f"显示前 {max_chars} 字符）"
        )

    return f"{url}\n\n{text}{truncated}"
