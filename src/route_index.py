"""Repo-wide route registration index for `smart_read_file(endpoint=...)`.

An agent that knows the failing route ("POST /api/products") should not have to
guess which file registers it, then read that file, then read the handler. One
call resolves route -> registration site -> handler body -> path + line range +
sha256 version, which is the unit `apply_edits` consumes.

The index is a bounded text scan, not a compiler front end: it recognises the
idiomatic registration shapes of the stacks TokenSafe targets (Go net/http,
gin, chi, tokensafe; Express/Fastify/Koa; FastAPI/Flask) and normalises path
templates (`{id}`, `:id`, `<int:id>`) so a concrete target still matches. Every
candidate file comes from the already-scanned code graph, so this adds no walk
of its own.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# Callee names that are syntax, not project functions.
_NON_CALLEES = frozenset({
    "if", "for", "switch", "select", "case", "return", "func", "function", "defer", "go",
    "make", "new", "len", "cap", "append", "copy", "delete", "panic", "recover", "print",
    "println", "range", "var", "const", "type", "struct", "map", "chan", "interface",
    "catch", "while", "with", "require", "import", "await", "async", "new", "typeof",
    "JSON", "Error", "String", "Number", "Promise", "Object", "Array", "console", "this",
    "super", "self", "cls", "str", "int", "float", "bool", "list", "dict", "set", "tuple",
    "isinstance", "hasattr", "getattr", "super", "print", "range", "enumerate", "zip",
})

_GO_VERB_CALL = re.compile(
    r"\b(?P<recv>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*\.\s*"
    r"(?P<method>Get|Post|Put|Patch|Delete|Head|Options|Handle|HandleFunc|Route|Group)\s*\(",
    re.IGNORECASE,
)
_JS_VERB_CALL = re.compile(
    r"\b(?P<recv>[A-Za-z_$][\w$.]*)\s*\.\s*"
    r"(?P<method>get|post|put|patch|delete|del|all|route)\s*\(",
    re.IGNORECASE,
)
_PY_DECORATOR = re.compile(r"^\s*@\s*(?P<recv>[A-Za-z_][\w.]*)\s*\.\s*"
                           r"(?P<method>get|post|put|patch|delete|route|api_route)\s*\(")
_STRING_LIT = re.compile(r"""[rbuRBU]{0,2}(?P<q>"|'|`)(?P<body>[^"'`\n]*)(?P=q)""")
_METHODS_KW = re.compile(r"methods\s*=\s*\[(?P<body>[^\]]*)\]", re.IGNORECASE)
_VERB_WORDS = {
    "get": "GET", "post": "POST", "put": "PUT", "patch": "PATCH", "delete": "DELETE",
    "del": "DELETE", "head": "HEAD", "options": "OPTIONS", "all": "ALL",
}
_TEMPLATE_SEG = re.compile(r"^(?:\{[^/}]*\}|:[A-Za-z_]\w*|<[^/>]*>|\$[A-Za-z_]\w*|\*)$")
_ID_SEG = re.compile(r"^(?:\d+|[0-9a-fA-F]{8,}(?:-[0-9a-fA-F]{4,})*)$")


def parse_endpoint(target: str) -> Optional[Tuple[str, str]]:
    """`"POST /api/products"` / `"post:/api/products"` / `"/api/products"` -> (verb, path).

    A bare path has no verb; callers that need one receive `""` and match any verb.
    """
    raw = (target or "").strip().strip("`\"'")
    if not raw:
        return None
    match = re.match(r"^(?:(?P<verb>[A-Za-z]+)\s*[:\s]\s*)?(?P<path>/\S*)", raw)
    if not match:
        return None
    verb = (match.group("verb") or "").strip().upper()
    path = match.group("path").strip().rstrip(".,;")
    if verb and verb not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "ALL"}:
        return None
    if not path.startswith("/"):
        return None
    return verb, path


def normalize_route(path: str) -> str:
    """Collapse every template placeholder to `*` so declared routes compare cheaply."""
    clean = (path or "").split("?", 1)[0].split("#", 1)[0].strip()
    if len(clean) > 1:
        clean = clean.rstrip("/")
    return "/" + "/".join(
        "*" if _TEMPLATE_SEG.match(seg) else seg.lower() for seg in clean.split("/") if seg
    )


def _segments(path: str) -> List[str]:
    clean = (path or "").split("?", 1)[0].split("#", 1)[0].strip()
    if len(clean) > 1:
        clean = clean.rstrip("/")
    return [seg for seg in clean.split("/") if seg]


def route_matches(declared: str, target: str) -> Tuple[bool, bool]:
    """(matches, exact) for a declared template against a concrete target path.

    Segment-wise on purpose: `POST /api/orders/123/cancel` must resolve the
    registration `POST /api/orders/{id}/cancel`, which a flat string compare of
    normalised paths cannot do.
    """
    left, right = _segments(declared), _segments(target)
    if len(left) != len(right):
        return False, False
    exact = True
    for a, b in zip(left, right):
        if a == b:
            continue
        if _TEMPLATE_SEG.match(a) or _TEMPLATE_SEG.match(b) or _ID_SEG.match(a) or _ID_SEG.match(b):
            exact = False
            continue
        return False, False
    return True, exact


def _strip_code_noise(text: str) -> str:
    """Blank out string literals and // comments so bracket counting is honest."""
    out = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in "\"'`":
            quote = ch
            i += 1
            while i < n and text[i] != quote:
                i += 2 if text[i] == "\\" else 1
            i += 1
            out.append(" ")
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            break
        out.append(ch)
        i += 1
    return "".join(out)


def _statement_end(lines: Sequence[str], start: int, max_lines: int = 40) -> int:
    """Exclusive end index of the call statement starting at `lines[start]`."""
    depth = 0
    opened = False
    last = min(len(lines), start + max_lines)
    for idx in range(start, last):
        for ch in _strip_code_noise(lines[idx]):
            if ch in "([{":
                depth += 1
                opened = True
            elif ch in ")]}":
                depth -= 1
        if opened and depth <= 0:
            return idx + 1
    return last


def _first_route_string(text: str) -> Optional[Tuple[str, int, int]]:
    """First path-like string literal in `text` -> (value, match_start, match_end).

    `match_end` is past the closing quote, which is where the handler argument
    begins; using the body end instead leaves a stray quote that defeats the
    handler parse.
    """
    for match in _STRING_LIT.finditer(text):
        body = match.group("body")
        if body.startswith("/") and not body.startswith("//"):
            return body, match.start(), match.end()
    return None


def _verbs_for(method: str, statement: str) -> List[str]:
    lowered = method.lower()
    if lowered in _VERB_WORDS:
        return [_VERB_WORDS[lowered]]
    if lowered in {"handlefunc", "handle", "route", "api_route"}:
        found: List[str] = []
        methods = _METHODS_KW.search(statement)
        if methods:
            found = [word.upper() for word in re.findall(r"[A-Za-z]+", methods.group("body"))]
        if not found:
            inline = re.search(r"""["']([A-Z]{3,7})\s+(/\S*)""", statement)
            if inline:
                found = [inline.group(1)]
        return found or ["ALL"]
    return []


def _route_literal(route: str) -> str:
    """Cheap prefilter needle: the first two concrete path segments, quoted."""
    segments = [seg for seg in route.split("/") if seg]
    if not segments:
        return ""
    return "/" + "/".join(segments[:2])


def _handler_after(statement: str, path_end: int) -> Tuple[str, str, int]:
    """(kind, expression, body_offset) of the handler argument after the route literal."""
    tail = statement[path_end:]
    lead = len(tail) - len(tail.lstrip(" ,:)\t\r\n"))
    body_start = path_end + lead
    head = tail[lead:]
    if not head:
        return "", "", body_start
    if head.startswith("func(") or head.startswith("func (") or head.startswith("function"):
        brace = head.find("{")
        return "inline", head[:brace if brace > 0 else 60].strip(), body_start
    if head.startswith("(") or head.startswith("async") or "=>" in head.split("{", 1)[0]:
        brace = head.find("{")
        return "inline", head[:brace if brace > 0 else 60].strip()[:80], body_start
    named = re.match(r"(?P<name>[A-Za-z_$][\w$.]*)", head)
    if named:
        # `cancelOrder(store)` is a handler factory: resolve the function name,
        # not the call expression, so the declaration can be located.
        return "named", named.group("name"), body_start
    return "", "", body_start


def _line_starts_call(line: str) -> bool:
    """Cheap anchor: the registration call must begin on this very line.

    Scanning from an arbitrary line merged a `})` line with the registration
    below it and then skipped the real registration — every parse is anchored to
    the line that opens the call.
    """
    stripped = line.lstrip()
    if not stripped or stripped.startswith(("//", "#", "*")):
        return False
    if _PY_DECORATOR.match(line):
        return True
    return bool(_GO_VERB_CALL.search(line) or _JS_VERB_CALL.search(line))


@dataclass
class RouteHit:
    file: str                      # workspace-relative path
    line: int                      # 1-based registration line
    end_line: int                  # 1-based last line of the registration statement
    verb: str                      # "POST" / "ALL"
    route: str                     # declared template, e.g. "/api/products/{id}"
    handler_kind: str              # "inline" | "named" | ""
    handler: str                   # "db.CreateProduct" / "func(c *gin.Context)"
    via: str                       # "tokensafe.Post" / "app.get" / "@router.post"
    body: List[str] = field(default_factory=list)
    handler_offset: int = 0        # char offset of the handler inside the body text
    exact: bool = True             # route matched without a template placeholder

    def header(self, version: str = "") -> str:
        label = f"{self.verb} {self.route}" if self.verb not in ("", "ALL") else self.route
        handler = f" {self.handler}" if self.handler and self.handler_kind == "named" else ""
        ver = f" ver={version}" if version else ""
        return f"{label} → {self.file} L{self.line}-L{self.end_line}{handler}{ver}"

    def callees(self) -> List[str]:
        """Names called inside the handler, in first-seen order.

        Empty for a named handler: the registration statement names the handler
        but does not contain its body — the caller resolves that file first.
        """
        if self.handler_kind != "inline":
            return []
        text = "\n".join(self.body)[self.handler_offset:]
        return extract_callees(text)


def extract_callees(text: str) -> List[str]:
    seen: Dict[str, bool] = {}
    for match in re.finditer(r"\b(?:(?P<qual>[A-Za-z_]\w*)\.)?(?P<name>[A-Za-z_]\w*)\s*\(", text):
        name = match.group("name")
        if name in _NON_CALLEES or name in seen:
            continue
        seen[name] = True
    return list(seen)


def _match_statement(line: str, statement: str) -> Optional[Tuple[str, str, str, str, str, int]]:
    """(verb, route, handler_kind, handler, via, handler_offset) for one statement."""
    candidates: List[Tuple[int, str, str]] = []  # (position, via, method)
    py = _PY_DECORATOR.match(line)
    if py:
        candidates.append((0, f"@{py.group('recv')}.{py.group('method')}", py.group("method")))
    go = _GO_VERB_CALL.search(statement)
    if go:
        candidates.append((go.start(), f"{go.group('recv')}.{go.group('method')}", go.group("method")))
    js = _JS_VERB_CALL.search(statement)
    if js:
        candidates.append((js.start(), f"{js.group('recv')}.{js.group('method')}", js.group("method")))
    if not candidates:
        return None
    candidates.sort()
    _pos, via, method = candidates[0]
    head_offset = statement.find("(")
    found = _first_route_string(statement[head_offset:])
    if not found:
        return None
    route, _start, match_end = found
    verbs = _verbs_for(method, statement)
    if not verbs:
        return None
    kind, handler, offset = _handler_after(statement, head_offset + match_end)
    return verbs[0], route, kind, handler, via, offset


def _py_handler(lines: Sequence[str], start: int) -> Tuple[str, int, int]:
    """A decorated Python handler: (name, first_line_index, last_line_index)."""
    idx = start + 1
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    if idx >= len(lines):
        return "", start, start
    match = re.match(r"\s*(?:async\s+)?def\s+(?P<name>[A-Za-z_]\w*)", lines[idx])
    if not match:
        return "", start, start
    indent = len(lines[idx]) - len(lines[idx].lstrip())
    last = idx
    for probe in range(idx + 1, min(len(lines), idx + 200)):
        text = lines[probe]
        if not text.strip():
            continue
        if len(text) - len(text.lstrip()) <= indent and not text.lstrip().startswith((")", "]", "}")):
            break
        last = probe
    return match.group("name"), idx, last


def find_registrations(
    root: str,
    verb: str,
    route: str,
    *,
    files: Optional[Iterable[str]] = None,
    max_files: int = 3000,
    max_hits: int = 6,
    max_file_bytes: int = 2_000_000,
) -> Tuple[List[RouteHit], int]:
    """All registrations matching (verb, route). Returns (hits, files_scanned).

    `files` are workspace-relative paths (the code graph's `file_stats` keys);
    without it a bounded walk is used, which keeps the function testable.
    Ordering is deterministic — exact routes first, then route-named files, real
    code before tests, then alphabetical — so two runs answer identically.
    """
    if files is None:
        files = _walk_code_files(root)
    needle = _route_literal(route)
    hits: List[RouteHit] = []
    scanned = 0
    seen: set = set()
    for rel in _prioritized(list(files)):
        if scanned >= max_files or len(hits) >= max_hits:
            break
        full = os.path.join(root, rel)
        real = os.path.realpath(full)
        if real in seen:  # the same file under two spellings must not be reported twice
            continue
        seen.add(real)
        try:
            if os.path.getsize(full) > max_file_bytes:
                continue
            with open(full, "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError:
            continue
        scanned += 1
        if needle and needle not in text:
            continue
        lines = text.splitlines()
        idx = 0
        while idx < len(lines):
            if not _line_starts_call(lines[idx]):
                idx += 1
                continue
            end = _statement_end(lines, idx)
            statement = "\n".join(lines[idx:end])
            parsed = _match_statement(lines[idx], statement)
            if not parsed:
                idx += 1
                continue
            hit_verb, hit_route, kind, handler, via, offset = parsed
            matched, exact = route_matches(hit_route, route)
            if not matched or (verb and hit_verb != verb and hit_verb != "ALL"):
                idx = max(end, idx + 1)
                continue
            body_end = end
            if _PY_DECORATOR.match(lines[idx]):
                name, _def_start, def_end = _py_handler(lines, idx)
                if name:
                    kind, handler = "named", name
                    body_end = def_end + 1
            hits.append(RouteHit(
                file=rel, line=idx + 1, end_line=body_end, verb=hit_verb, route=hit_route,
                handler_kind=kind, handler=handler, via=via,
                body=lines[idx:body_end], handler_offset=offset if kind == "inline" else 0,
                exact=exact,
            ))
            if len(hits) >= max_hits:
                break
            idx = max(body_end, idx + 1)
    hits.sort(key=lambda h: (not h.exact, h.file, h.line))
    return hits, scanned


def _prioritized(files: List[str]) -> List[str]:
    hints = ("route", "router", "routes", "server", "app", "api", "url", "urls", "view", "handler", "main", "endpoint")
    def key(rel: str) -> Tuple[int, int, str]:
        lowered = rel.lower()
        base = os.path.basename(lowered)
        named = 0 if any(hint in base for hint in hints) else 1
        # A test that registers a route is evidence, not the app: rank it last so a
        # real registration is found inside the hit budget.
        test = 1 if (base.endswith("_test.go") or base.startswith("test_")
                     or ".test." in base or ".spec." in base
                     or "/tests/" in f"/{lowered}" or "/__tests__/" in f"/{lowered}") else 0
        return named + test, test, rel
    return sorted(files, key=key)


_NOISE_DIRS = frozenset({
    "node_modules", ".git", "vendor", "dist", "build", "target", ".venv", "venv",
    "__pycache__", ".next", ".nuxt", "coverage", ".mypy_cache", ".pytest_cache",
})
_CODE_EXTS = frozenset({
    ".go", ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte",
    ".rb", ".php", ".java", ".kt", ".rs", ".cs",
})


def _walk_code_files(root: str, limit: int = 3000) -> List[str]:
    out: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _NOISE_DIRS and not d.startswith(".")]
        for name in filenames:
            if os.path.splitext(name)[1].lower() in _CODE_EXTS:
                out.append(os.path.relpath(os.path.join(dirpath, name), root).replace("\\", "/"))
                if len(out) >= limit:
                    return out
    return out


def file_version(path: str) -> str:
    """Whole-file sha256 as OCC token (12 hex is enough to detect drift)."""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 16), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()[:12]
