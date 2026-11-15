"""
TokenSafe Web Page Reader & HTML-to-Markdown Compactor (v3.3.0).

Fetches web pages (documentation, API references, articles) and strips clutter:
- Strips <script>, <style>, <nav>, <footer>, <header>, <aside>, <svg>, <form>, <noscript>
- Converts content into clean, readable Markdown
- Protects against SSRF / local address probing
- Stashes full raw HTML in TokenSafe reversible stash if content is large.
"""

import re
import urllib.request
import urllib.parse
from html.parser import HTMLParser
from typing import Tuple, Optional, Dict, Any, List

from src.stash import stash_block


class _HTMLToMarkdownParser(HTMLParser):
    """Pure-Python zero-dependency HTML to clean Markdown extractor."""

    IGNORE_TAGS = {
        "script", "style", "nav", "footer", "header", "aside",
        "svg", "form", "noscript", "iframe", "object", "embed", "canvas"
    }

    BLOCK_TAGS = {"p", "div", "section", "article", "blockquote", "tr"}
    HEADING_TAGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}

    def __init__(self):
        super().__init__()
        self._ignore_depth = 0
        self._pieces: List[str] = []
        self._in_pre = False
        self._in_code = False
        self._current_link: Optional[str] = None
        self._link_text: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]):
        tag_lower = tag.lower()
        if tag_lower in self.IGNORE_TAGS:
            self._ignore_depth += 1
            return
        if self._ignore_depth > 0:
            return

        attr_dict = {k.lower(): (v or "") for k, v in attrs}

        # Filter out common ad/nav/cookie banner classes/ids
        cls_and_id = (attr_dict.get("class", "") + " " + attr_dict.get("id", "")).lower()
        if any(w in cls_and_id for w in ("cookie", "banner", "advertisement", "sidebar", "nav-menu")):
            self._ignore_depth += 1
            return

        if tag_lower in self.HEADING_TAGS:
            level = self.HEADING_TAGS[tag_lower]
            self._pieces.append(f"\n\n{'#' * level} ")
        elif tag_lower in self.BLOCK_TAGS:
            self._pieces.append("\n")
        elif tag_lower == "br":
            self._pieces.append("\n")
        elif tag_lower == "hr":
            self._pieces.append("\n\n---\n\n")
        elif tag_lower in ("pre",):
            self._in_pre = True
            self._pieces.append("\n```\n")
        elif tag_lower in ("code",):
            if not self._in_pre:
                self._in_code = True
                self._pieces.append("`")
        elif tag_lower == "li":
            self._pieces.append("\n- ")
        elif tag_lower == "a":
            href = attr_dict.get("href", "").strip()
            if href and not href.startswith(("javascript:", "mailto:", "#")):
                self._current_link = href
                self._link_text = []

    def handle_endtag(self, tag: str):
        tag_lower = tag.lower()
        if tag_lower in self.IGNORE_TAGS:
            self._ignore_depth = max(0, self._ignore_depth - 1)
            return
        if self._ignore_depth > 0:
            return

        if tag_lower in self.HEADING_TAGS:
            self._pieces.append("\n\n")
        elif tag_lower in self.BLOCK_TAGS:
            self._pieces.append("\n")
        elif tag_lower == "pre":
            self._in_pre = False
            self._pieces.append("\n```\n")
        elif tag_lower == "code":
            if not self._in_pre and self._in_code:
                self._in_code = False
                self._pieces.append("`")
        elif tag_lower == "a":
            if self._current_link:
                text = "".join(self._link_text).strip()
                if text:
                    self._pieces.append(f"[{text}]({self._current_link})")
                self._current_link = None
                self._link_text = []

    def handle_data(self, data: str):
        if self._ignore_depth > 0:
            return
        if self._current_link is not None:
            self._link_text.append(data)
            return

        if self._in_pre:
            self._pieces.append(data)
        else:
            # Clean up whitespace
            cleaned = re.sub(r"[ \t]+", " ", data)
            self._pieces.append(cleaned)

    def get_markdown(self) -> str:
        raw_text = "".join(self._pieces)
        # Collapse multiple empty lines
        cleaned = re.sub(r"\n{3,}", "\n\n", raw_text).strip()
        return cleaned


class WebReader:
    """Safely fetches web pages and returns clean Markdown."""

    _BLOCKED_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "169.254.169.254"}

    @classmethod
    def is_safe_url(cls, url: str) -> bool:
        try:
            parsed = urllib.parse.urlparse(url)
            if parsed.scheme not in ("http", "https"):
                return False
            host = (parsed.hostname or "").lower()
            if host in cls._BLOCKED_HOSTS or host.startswith(("10.", "192.168.", "172.16.")):
                return False
            return True
        except Exception:
            return False

    @classmethod
    def html_to_markdown(cls, html_content: str, url: str = "") -> Tuple[str, bool]:
        """Converts raw HTML string to clean Markdown."""
        parser = _HTMLToMarkdownParser()
        try:
            parser.feed(html_content)
            md = parser.get_markdown()
        except Exception:
            # Basic regex fallback if HTMLParser fails on corrupt tags
            cleaned = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html_content, flags=re.DOTALL | re.IGNORECASE)
            cleaned = re.sub(r"<[^>]+>", " ", cleaned)
            md = re.sub(r"\s+", " ", cleaned).strip()

        if len(html_content) > 3000 and len(md) < len(html_content) * 0.7:
            hash8, marker = stash_block(html_content, source=f"web:{url[:60]}")
            header = f"# [TokenSafe Web Extract: {url}]\n\n" if url else ""
            return f"{header}{md}\n\n{marker}", True

        return md, False

    @classmethod
    def fetch_url(cls, url: str, timeout: float = 8.0) -> Tuple[str, bool, Optional[str]]:
        """
        Fetches URL, converts to clean Markdown.
        Returns (content_or_error, is_success, opt_error_msg).
        """
        if not cls.is_safe_url(url):
            return "", False, f"Refused to fetch potentially unsafe or internal URL: {url}"

        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; TokenSafeBot/3.3; +https://tokensafe.dev)",
                "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9",
            }
        )

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = resp.status
                if status >= 400:
                    return "", False, f"HTTP Error {status}"
                content_type = resp.headers.get("Content-Type", "").lower()
                raw_bytes = resp.read(2 * 1024 * 1024)  # 2MB max
                charset = "utf-8"
                if "charset=" in content_type:
                    charset = content_type.split("charset=")[-1].split(";")[0].strip()

                raw_text = raw_bytes.decode(charset, errors="replace")

            if "text/html" in content_type or "<html" in raw_text.lower():
                md, _ = cls.html_to_markdown(raw_text, url=url)
                return md, True, None
            else:
                return raw_text, True, None
        except Exception as e:
            return "", False, f"Fetch error: {e}"
