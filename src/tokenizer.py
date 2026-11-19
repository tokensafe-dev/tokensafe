"""
Zero-dependency, high-accuracy BPE/LLM Token Estimator.
Optimized for Gemini, Claude, and OpenAI tokenization patterns.
Supports English, Cyrillic, CJK, and code-heavy payloads.
"""

import re
from typing import Optional

_TIKTOKEN_ENCODING = None
_TIKTOKEN_PROBED = False


def _get_tiktoken_encoding():
    global _TIKTOKEN_ENCODING, _TIKTOKEN_PROBED
    if _TIKTOKEN_PROBED:
        return _TIKTOKEN_ENCODING
    _TIKTOKEN_PROBED = True
    try:
        import tiktoken  # type: ignore
        _TIKTOKEN_ENCODING = tiktoken.get_encoding("cl100k_base")
    except Exception:
        _TIKTOKEN_ENCODING = None
    return _TIKTOKEN_ENCODING


class TokenCounter:
    """Estimates or calculates token counts across languages and code structures."""

    # Robust regex splitting on words (incl. extended Latin & Cyrillic), contractions,
    # numbers, punctuation runs, CJK, and whitespace.
    _WORD_REGEX = re.compile(
        r"""'s|'t|'re|'ve|'m|'ll|'d|[a-zA-Z\u00C0-\u024F\u0400-\u04FF_]+|\d{1,3}|[\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF\u3040-\u30FF\u31F0-\u31FF\uAC00-\uD7AF]+|[^\w\s]+|\s+"""
    )

    # Script detection via C-level regex scans — replaces per-char Python loops
    # that previously dominated the hot path (~46% of runtime on mixed text).
    _CYRILLIC_RE = re.compile(r"[\u0400-\u04FF]")
    _CJK_RE = re.compile(
        r"[\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF\u3040-\u30FF\u31F0-\u31FF\uAC00-\uD7AF]"
    )
    # English / code identifier subword split (hoisted: avoids the re-module
    # cache lookup on every word of every payload).
    _SUBWORD_REGEX = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z][a-z]|\d|\W|$)|\d+")

    @classmethod
    def count(cls, text: Optional[str]) -> int:
        """Calculate or estimate token count for any text."""
        if not text:
            return 0

        # If tiktoken is installed, use exact OpenAI BPE cl100k (allowing literal special tokens)
        enc = _get_tiktoken_encoding()
        if enc is not None:
            try:
                return len(enc.encode(text, disallowed_special=()))
            except Exception:
                pass

        # High-precision heuristic BPE emulation
        return cls._estimate_tokens(text)

    @classmethod
    def _estimate_tokens(cls, text: str) -> int:
        tokens = 0
        chunks = cls._WORD_REGEX.findall(text)

        for chunk in chunks:
            # Check if whitespace
            if chunk.isspace():
                if "\n" in chunk:
                    newlines = chunk.count("\n")
                    tokens += newlines
                    tokens += (len(chunk) - newlines) // 4
                else:
                    # In BPE, single spaces are merged into adjacent word tokens (e.g. " world").
                    # Only indentations (e.g., 4 spaces) add extra tokens.
                    tokens += len(chunk) // 4
                continue

            # Check if punctuation / code symbol
            if not chunk.isalnum() and "_" not in chunk:
                # Group of symbols: e.g. "();" -> 1 token per 1-2 symbols
                tokens += max(1, (len(chunk) + 1) // 2)
                continue

            # Check if numeric
            if chunk.isdigit():
                tokens += max(1, (len(chunk) + 2) // 3)
                continue

            # Alphabetic words: Check script. ASCII chunks (the dominant case for
            # code and English payloads) cannot be Cyrillic or CJK — skip the scan.
            if chunk.isascii():
                is_cyrillic = is_cjk = False
            else:
                is_cyrillic = cls._CYRILLIC_RE.search(chunk) is not None
                is_cjk = cls._CJK_RE.search(chunk) is not None

            if is_cjk:
                # CJK: typically ~1.8 tokens per character
                tokens += int(len(chunk) * 1.8) + 1
            elif is_cyrillic:
                # Cyrillic: BPE splits into ~2-3 char chunks
                if len(chunk) <= 3:
                    tokens += 1
                elif len(chunk) <= 6:
                    tokens += 2
                elif len(chunk) <= 9:
                    tokens += 3
                else:
                    tokens += max(3, (len(chunk) + 2) // 3)
            else:
                # English / Code identifier
                subwords = cls._SUBWORD_REGEX.findall(chunk)
                if subwords:
                    for sub in subwords:
                        if len(sub) <= 7:
                            tokens += 1
                        elif len(sub) <= 12:
                            tokens += 2
                        else:
                            tokens += max(2, (len(sub) + 4) // 5)
                else:
                    if len(chunk) <= 7:
                        tokens += 1
                    else:
                        tokens += max(1, (len(chunk) + 4) // 5)

        return max(1, tokens) if text.strip() else 0


def count_tokens(text: Optional[str]) -> int:
    """Convenience helper."""
    return TokenCounter.count(text)
