"""
TokenSafe Web Page Reader & HTML-to-Markdown Compactor (v3.3.0).

Fetches web pages (documentation, API references, articles) and strips clutter:
- Strips <script>, <style>, <nav>, <footer>, <header>, <aside>, <svg>, <form>, <noscript>
- Converts content into clean, readable Markdown
- Protects against SSRF / local address probing
- Stashes full raw HTML in TokenSafe reversible stash if content is large.
"""

import os
import re
import ipaddress
import urllib.request
import urllib.parse
import urllib.error
import sqlite3
import time
import hashlib
import threading
from collections import OrderedDict
from html.parser import HTMLParser
from typing import Tuple, Optional, Dict, Any, List

from src.stash import stash_block
from src.paths import resolve_data_dir


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


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Blocks redirect hops that leave the safe-URL zone (public → localhost/DMZ)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not WebReader.is_safe_url(newurl):
            raise urllib.error.HTTPError(newurl, code, "TokenSafe: redirect to a non-public URL blocked", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class WebReader:
    """Safely fetches web pages and returns clean Markdown with Local First (L1/L2) + Team Sync (L3) caching."""

    _BLOCKED_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "169.254.169.254"}
    _SENSITIVE_QUERY_PARAMS = {
        "token", "auth", "key", "signature", "sig", "secret", "credential",
        "session", "access_token", "api_key", "x-amz-signature", "x-amz-credential"
    }

    _TTL_SECONDS = 72 * 3600  # 72 hours
    _MAX_DB_BYTES = 50 * 1024 * 1024  # 50 MB
    _mem_cache: "OrderedDict[str, str]" = OrderedDict()
    _mem_lock = threading.Lock()
    _db_init_done = False
    _db_init_lock = threading.Lock()
    last_tier: str = "live"

    @classmethod
    def sanitize_url(cls, url: str) -> str:
        """Strips sensitive authentication & signature tokens from query strings for clean canonical caching."""
        try:
            parsed = urllib.parse.urlparse(url.strip())
            qs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
            cleaned_qs = [
                (k, v) for k, v in qs
                if k.lower() not in cls._SENSITIVE_QUERY_PARAMS and not k.lower().startswith("x-amz-")
            ]
            cleaned_qs.sort(key=lambda x: x[0])
            new_query = urllib.parse.urlencode(cleaned_qs)
            return urllib.parse.urlunparse((
                parsed.scheme.lower(),
                parsed.netloc.lower(),
                parsed.path,
                parsed.params,
                new_query,
                ""  # drop fragments
            ))
        except Exception:
            return url.strip()

    @classmethod
    def _get_db_path(cls) -> str:
        data_dir = resolve_data_dir()
        os.makedirs(data_dir, exist_ok=True)
        return os.path.join(data_dir, "web_cache.db")

    @classmethod
    def _init_db(cls):
        db_path = cls._get_db_path()
        with cls._db_init_lock:
            if cls._db_init_done == db_path and os.path.exists(db_path):
                return
            try:
                with sqlite3.connect(db_path, timeout=5.0) as conn:
                    conn.execute("PRAGMA journal_mode=WAL;")
                    conn.execute("PRAGMA busy_timeout=5000;")
                    conn.execute("""
                        CREATE TABLE IF NOT EXISTS web_cache (
                            url_hash TEXT PRIMARY KEY,
                            canonical_url TEXT NOT NULL,
                            markdown TEXT NOT NULL,
                            fetched_at INTEGER NOT NULL,
                            size_bytes INTEGER NOT NULL,
                            hit_count INTEGER DEFAULT 1
                        );
                    """)
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_web_cache_fetched ON web_cache(fetched_at);")
                    conn.commit()
                cls._db_init_done = db_path
            except Exception:
                pass

    @classmethod
    def clear_memory_cache(cls):
        with cls._mem_lock:
            cls._mem_cache.clear()

    @classmethod
    def _maybe_evict_l2(cls, conn: sqlite3.Connection):
        """Maintains 50MB hard storage bound using LRU eviction."""
        try:
            db_path = cls._get_db_path()
            if os.path.exists(db_path) and os.path.getsize(db_path) > cls._MAX_DB_BYTES:
                conn.execute("""
                    DELETE FROM web_cache WHERE url_hash IN (
                        SELECT url_hash FROM web_cache ORDER BY fetched_at ASC LIMIT 100
                    );
                """)
        except Exception:
            pass

    @classmethod
    def get_cached(
        cls,
        url: str,
        custom_headers: Optional[Dict[str, str]] = None
    ) -> Tuple[Optional[str], str]:
        """
        Queries L1 memory, L2 SQLite, and L3 Team Cache in sequence.
        Returns (content, tier) where tier in ("L1", "L2", "L3") or (None, "miss").
        """
        clean_url = cls.sanitize_url(url)
        url_hash = hashlib.sha256(clean_url.encode("utf-8")).hexdigest()

        # 1. Check L1 Memory
        with cls._mem_lock:
            if url_hash in cls._mem_cache:
                cls._mem_cache.move_to_end(url_hash)
                cls.last_tier = "L1"
                return cls._mem_cache[url_hash], "L1"

        # 2. Check L2 SQLite
        cls._init_db()
        db_path = cls._get_db_path()
        now = int(time.time())
        try:
            with sqlite3.connect(db_path, timeout=5.0) as conn:
                conn.execute("PRAGMA busy_timeout=5000;")
                row = conn.execute(
                    "SELECT markdown, fetched_at FROM web_cache WHERE url_hash = ?",
                    (url_hash,)
                ).fetchone()
                if row:
                    md, fetched_at = row[0], int(row[1])
                    if now - fetched_at < cls._TTL_SECONDS:
                        conn.execute("UPDATE web_cache SET hit_count = hit_count + 1 WHERE url_hash = ?", (url_hash,))
                        conn.commit()
                        with cls._mem_lock:
                            cls._mem_cache[url_hash] = md
                            if len(cls._mem_cache) > 50:
                                cls._mem_cache.popitem(last=False)
                        cls.last_tier = "L2"
                        return md, "L2"
        except Exception:
            pass

        # 3. Check L3 Team Sync (Only if request carried no custom auth/cookie headers)
        has_auth = bool(custom_headers and any(
            k.lower() in ("authorization", "cookie", "x-api-key")
            for k in custom_headers
        ))
        if not has_auth:
            try:
                from src.team_cache import TeamCache
                team_val = TeamCache.get("WEB_DOCS", clean_url, timeout=0.6)
                if team_val:
                    cls.set_cached(url, team_val, sync_team=False)
                    cls.last_tier = "L3"
                    return team_val, "L3"
            except Exception:
                pass

        cls.last_tier = "live"
        return None, "miss"

    @classmethod
    def set_cached(cls, url: str, markdown: str, sync_team: bool = True, custom_headers: Optional[Dict[str, str]] = None) -> None:
        """Stores clean Markdown into L1, L2 SQLite, and asynchronously broadcasts to L3 Team Cache."""
        if not url or not markdown:
            return
        clean_url = cls.sanitize_url(url)
        url_hash = hashlib.sha256(clean_url.encode("utf-8")).hexdigest()
        now = int(time.time())
        size_bytes = len(markdown.encode("utf-8"))

        # 1. Save L1
        with cls._mem_lock:
            cls._mem_cache[url_hash] = markdown
            cls._mem_cache.move_to_end(url_hash)
            if len(cls._mem_cache) > 50:
                cls._mem_cache.popitem(last=False)

        # 2. Save L2 SQLite
        cls._init_db()
        db_path = cls._get_db_path()
        try:
            with sqlite3.connect(db_path, timeout=5.0) as conn:
                conn.execute("PRAGMA busy_timeout=5000;")
                conn.execute("""
                    INSERT INTO web_cache (url_hash, canonical_url, markdown, fetched_at, size_bytes, hit_count)
                    VALUES (?, ?, ?, ?, ?, 1)
                    ON CONFLICT(url_hash) DO UPDATE SET
                        markdown = excluded.markdown,
                        fetched_at = excluded.fetched_at,
                        size_bytes = excluded.size_bytes;
                """, (url_hash, clean_url, markdown, now, size_bytes))
                cls._maybe_evict_l2(conn)
                conn.commit()
        except Exception:
            pass

        # 3. Broadcast to L3 Team Sync
        has_auth = bool(custom_headers and any(
            k.lower() in ("authorization", "cookie", "x-api-key")
            for k in custom_headers
        ))
        if sync_team and not has_auth:
            try:
                from src.team_cache import TeamCache
                est_saved = max(50, len(markdown) // 4)
                TeamCache.put("WEB_DOCS", clean_url, markdown, tokens_saved=est_saved)
            except Exception:
                pass

    @classmethod
    def is_safe_url(cls, url: str) -> bool:
        try:
            parsed = urllib.parse.urlparse(url)
            if parsed.scheme not in ("http", "https"):
                return False
            host = (parsed.hostname or "").lower().rstrip(".")
            if host in cls._BLOCKED_HOSTS or host.endswith(".local"):
                return False
            # Literal IPs (v4/v6) — exact range checks instead of string prefixes
            try:
                ip = ipaddress.ip_address(host)
                if (
                    ip.is_private
                    or ip.is_loopback
                    or ip.is_link_local
                    or ip.is_reserved
                    or ip.is_unspecified
                    or ip.is_multicast
                ):
                    return False
            except ValueError:
                pass  # hostname, not a literal IP
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
    def fetch_url(
        cls,
        url: str,
        timeout: float = 8.0,
        force_refresh: bool = False,
        custom_headers: Optional[Dict[str, str]] = None
    ) -> Tuple[str, bool, Optional[str]]:
        """
        Fetches URL or returns cached Markdown from L1/L2/L3.
        Returns (content_or_error, is_success, opt_error_msg).
        Sets cls.last_tier to "L1", "L2", "L3", or "live".
        """
        if not cls.is_safe_url(url):
            return "", False, f"Refused to fetch potentially unsafe or internal URL: {url}"

        # Check Local First & Team Sync caches unless force_refresh
        if not force_refresh:
            cached_val, tier = cls.get_cached(url, custom_headers=custom_headers)
            if cached_val:
                cls.last_tier = tier
                return cached_val, True, None

        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; TokenSafeBot/3.3; +https://tokensafe.dev)",
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9",
        }
        if custom_headers:
            headers.update(custom_headers)

        req = urllib.request.Request(url, headers=headers)

        try:
            _opener = urllib.request.build_opener(_SafeRedirectHandler)
            with _opener.open(req, timeout=timeout) as resp:
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
            else:
                md = raw_text

            cls.last_tier = "live"
            cls.set_cached(url, md, sync_team=True, custom_headers=custom_headers)
            return md, True, None
        except Exception as e:
            cls.last_tier = "error"
            return "", False, f"Fetch error: {e}"


sanitize_url = WebReader.sanitize_url
