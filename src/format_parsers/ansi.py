"""
TokenSafe ANSI Escape Sequence Stripper (v3.3.0).

Cleans terminal control characters, color codes (SGR), cursor movements,
and OSC hyperlink sequences from bash, test, and compiler outputs.
Eliminates 10-25% invisible token bloat in command and test logs.
"""

import re

# Comprehensive ANSI escape regex covering:
# 1. CSI sequences: \x1b\[[0-9;]*[A-Za-z] (colors, cursor movements, erase line)
# 2. OSC sequences: \x1b\][^\x07\x1b]*(\x07|\x1b\\) (hyperlinks, window titles)
# 3. Simple escapes: \x1b[()][AB012], \x1b[=>]
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-9;?=<>*!]*[ -/]*[@-~]")
_ANSI_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(\x07|\x1b\\)")
_ANSI_OTHER_RE = re.compile(r"\x1b[()][AB012]|\x1b[=>]")


def has_ansi(text: str) -> bool:
    """Returns True if text contains ANSI escape sequences."""
    if not text or not isinstance(text, str):
        return False
    return "\x1b" in text or "\033" in text


def strip_ansi(text: str) -> str:
    """
    Strips all ANSI color, style, cursor, and control sequences from text.
    Returns clean plain text.
    """
    if not text or not isinstance(text, str):
        return ""
    if not has_ansi(text):
        return text

    clean = _ANSI_CSI_RE.sub("", text)
    clean = _ANSI_OSC_RE.sub("", clean)
    clean = _ANSI_OTHER_RE.sub("", clean)
    clean = clean.replace("\x1b", "")
    return clean
