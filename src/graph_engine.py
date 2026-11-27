"""
TokenSafe Graph Engine: Dependency-Aware Graph Analysis & Skeletonization.
Builds a zero-LLM deterministic code graph for Python, TypeScript/JavaScript, Go,
Rust, Java, C#, C/C++, PHP, Kotlin, Ruby, and Swift.

Uses the optional Tree-sitter backend when available (`src.treesitter_backend`)
and falls back seamlessly to the Python standard library `ast` and `BraceLexer`
state-machine parsers with zero extra required runtime dependencies.

Powers:
1. Graph-Guided Skeletonization (target + 1-hop full/signature, 0-hop isolated).
2. God Nodes Detection (architectural hubs via import- & call-aware centrality).
3. API Surface Map Generation (tokensafe-out/API_SURFACE.md).
4. Incremental `(mtime, size)` scanning & optional persistent disk cache.
"""

import ast
import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
import threading
import time
from typing import Dict, Set, List, Optional, Tuple, Any

from src.paths import resolve_data_dir

IGNORED_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "dist", "build", "venv", ".venv",
    "__pycache__", ".idea", ".vscode", "data", "coverage", ".pytest_cache",
    "Library", "Applications", "Pictures", "Movies", "Music", "Downloads",
    ".cache", ".npm", ".bun", ".rustup", ".cargo", ".local", ".system_generated",
    "target", "tokensafe-out",
}

SUPPORTED_EXTS = {
    ".py", ".ts", ".js", ".tsx", ".jsx", ".mjs", ".cjs",
    ".go", ".rs", ".java", ".cs",
    ".c", ".cpp", ".cc", ".cxx", ".h", ".hpp",
    ".php", ".kt", ".kts", ".swift", ".rb",
}

_SKIP_CALLS = {
    "if", "else", "for", "while", "do", "switch", "case", "catch", "try",
    "return", "throw", "yield", "await", "sizeof", "typeof", "instanceof",
    "new", "delete", "import", "export", "require", "print", "len", "str",
    "int", "float", "bool", "list", "dict", "set", "tuple", "range",
    "div", "span", "button", "input", "label", "form", "svg", "path",
    "true", "false", "null", "undefined", "None", "True", "False",
}

# v7: endpoint extraction learned chained router calls (Elysia/Fastify `.post("/x")`),
# file-level framework prefixes and app.use('/p', router) mounts; older caches hold
# an empty endpoint set for those stacks.
_CACHE_SCHEMA_VERSION = 7

_STATIC_ASSET_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp",
    ".css", ".scss", ".sass", ".less",
    ".woff", ".woff2", ".ttf", ".eot",
    ".map", ".pdf", ".mp4", ".mp3",
}

_BACKEND_ROUTE_PATTERNS = [
    # (regex, default_method). Group 1 is always the route literal; the declaring
    # receiver (if any) is read back from the match text with _RECEIVER_RE.
    # Python: @app.get("/users"), @router.post('/login'), @bp.route('/data')
    (re.compile(r'@(?:app|router|api|bp)\.(?:get|post|put|delete|patch|route)\(\s*["\']([^"\']+)["\']', re.IGNORECASE), None),
    # JS/TS: app.get("/users", ...), router.post('/login', ...)
    (re.compile(r'(?<!@)\b(?:app|router|server|fastify)\.(?:get|post|put|delete|patch)\(\s*["\']([^"\']+)["\']', re.IGNORECASE), None),
    # Named routers: usersRouter.get("/users", ...), wheelRoutes.post("/spin", ...).
    # The suffix carries the intent, so a bare `axios.get("/x")` stays a client call.
    (re.compile(r'\b[A-Za-z_$][\w$]*(?:Router|Routes|App|Server)\.(?:get|post|put|delete|patch)\(\s*[`"\'](/[^`"\'?#]*)'), None),
    # Chained builders: Elysia/Fastify `new Elysia().post("/spin", h)` and
    # `router\n  .get("/status", h)`. The verb must be preceded by whitespace or
    # ")" (a chain continuation) and the literal must start with "/", so
    # `axios.get(...)` / `map.get(...)` receivers stay excluded.
    (re.compile(r'(?<![\w$.])\.(?:get|post|put|delete|patch)\(\s*[`"\'](/[^`"\'?#]*)'), None),
    # Java/Kotlin Spring: @GetMapping("/users"), @PostMapping(value = "/login"), @RequestMapping("/api")
    (re.compile(r'@(?:Get|Post|Put|Delete|Patch|Request)Mapping\(\s*(?:value\s*=\s*)?["\']([^"\']+)["\']', re.IGNORECASE), None),
    # Go Gin/Echo/Fiber: r.GET("/users", ...), router.POST('/login', ...)
    (re.compile(r'\b(?:r|router|g|e|app)\.(?:GET|POST|PUT|DELETE|PATCH)\(\s*["\']([^"\']+)["\']'), None),
    # Go net/http: http.HandleFunc("/users", ...)
    (re.compile(r'http\.HandleFunc\(\s*["\']([^"\']+)["\']'), None),
    # C# ASP.NET Core: [HttpGet("/users")], [Route("api/users")]
    (re.compile(r'\[(?:Http(?:Get|Post|Put|Delete|Patch)|Route)\(\s*["\']([^"\']+)["\']'), None),
    # PHP Laravel: Route::get('/users', ...), Route::post('/login', ...)
    (re.compile(r'Route::(?:get|post|put|delete|patch)\(\s*["\']([^"\']+)["\']'), None),
]

# Call targets that are language/runtime builtins rather than code this graph
# could resolve: reported as unresolved would drown the real gaps.
_STATIC_FLOW_NOISE = {
    "Boolean", "Number", "String", "Object", "Array", "Date", "Promise", "Map",
    "Set", "JSON", "Math", "Error", "RegExp", "Symbol", "Buffer", "console",
    "process", "require", "setTimeout", "setInterval", "clearTimeout",
    "clearInterval", "requestAnimationFrame", "cancelAnimationFrame",
    "parseInt", "parseFloat", "isNaN", "fetch", "len", "str", "int", "float",
    "list", "dict", "set", "tuple", "range", "print", "open", "super",
    "self", "this", "None", "True", "False",
}

# The object a route is declared on, read from the start of the matched text:
# `app.get(...)` -> "app", `.post(...)` -> None (chained builder).
_RECEIVER_RE = re.compile(r'^\s*([A-Za-z_$][\w$]*)\.')


_FRONTEND_CALL_PATTERNS = [
    # fetch("/api/users") or fetch(`/api/users/${id}`)  (method unknown: depends on init options)
    (re.compile(r'\bfetch\(\s*[`"\']([^`"\'?#]+)'), None),
    # axios.get("/api/users"), api.post(`/api/users/${id}`), client.get, etc.
    (re.compile(r'\b(?:axios|api|client|httpClient|ky)\.(?:get|post|put|delete|patch)\(\s*[`"\']([^`"\'?#]+)'), None),
    # useSWR("/api/users") — semantically always GET
    (re.compile(r'\buseSWR\(\s*[`"\']([^`"\'?#]+)'), "GET"),
    # axios("/api/users")  (method unknown: depends on config)
    (re.compile(r'\baxios\(\s*[`"\']([^`"\'?#]+)'), None),
    # Local HTTP wrappers: request('/wheel/spin') and TypeScript generics
    # (request<SpinWheelResponse>('/wheel/spin', ...)) carrying a literal absolute
    # route were invisible to cross-stack linking. Requiring a leading "/" keeps
    # arg-of-something calls such as request('users') out.
    (re.compile(r'\b(?:request|apiRequest|httpRequest|sendRequest|requestJson|fetchJson)'
                r'(?:\s*<[^<>()]{1,120}>)?\s*\(\s*[`"\'](/[^`"\'?#]*)'), None),
]

# HTTP method extraction from route-declaration / API-call matched text.
# File-level route prefixes declared by the framework constructor, so a route
# literal in that file is not the route clients call: Elysia
# `new Elysia({ prefix: "/wheel" })`, FastAPI/Flask `APIRouter(prefix=...)`,
# `Blueprint(..., url_prefix=...)`, Fastify plugin `{ prefix: "/v1" }`.
_FRAMEWORK_PREFIX_PATTERNS = [
    re.compile(r'\bnew\s+Elysia\s*\([^)]*?\bprefix\s*:\s*[`"\']([^`"\']+)[`"\']'),
    re.compile(r'\b(?:APIRouter|FastAPI|Blueprint|Flask)\s*\([^)]*?\b(?:url_)?prefix\s*=\s*["\']([^"\']+)["\']'),
    re.compile(r'\bregister\s*\(\s*[A-Za-z_$][\w$]*\s*,\s*\{[^}]*?\bprefix\s*:\s*[`"\']([^`"\']+)[`"\']'),
]

# Router mounts: app.use("/wheel", wheelRoutes) / server.register("/v1", routes).
_MOUNT_RE = re.compile(
    r'\b(?:app|server|api|router|fastify|r)\.(?:use|register)\(\s*[`"\'](/[^`"\']*)[`"\']\s*,\s*([A-Za-z_$][\w$]*)'
)

# Where a mounted router identifier comes from: import/require bindings, so the
# mount prefix can be applied to the file that actually declares the routes.
_IMPORT_BINDING_PATTERNS = [
    # import { a, b as c } from "./mod" / import def from "./mod"
    re.compile(r'\bimport\s+(?:\{([^}]*)\}|([A-Za-z_$][\w$]*))\s*from\s*[`"\']([^`"\']+)[`"\']'),
    # const { a, b: c } = require("./mod") / const def = require("./mod")
    re.compile(r'\b(?:const|let|var)\s+(?:\{([^}]*)\}|([A-Za-z_$][\w$]*))\s*=\s*require\(\s*[`"\']([^`"\']+)[`"\']'),
]


def _import_bindings(content: str) -> Dict[str, str]:
    """Maps a local identifier to the module specifier it was imported from."""
    bindings: Dict[str, str] = {}
    for rx in _IMPORT_BINDING_PATTERNS:
        for m in rx.finditer(content):
            names_blob, default_name, module = m.group(1), m.group(2), m.group(3)
            if default_name:
                bindings[default_name] = module
                continue
            for part in (names_blob or "").split(","):
                part = part.strip()
                if not part:
                    continue
                # `a as b` -> local b; `a: b` -> local b
                local = re.split(r'\s+as\s+|:\s*', part)[-1].strip()
                if local:
                    bindings[local] = module
    return bindings


def _join_route(prefix: str, route: str) -> str:
    """Compose a mount/constructor prefix with a declared route, without doubling it."""
    p = (prefix or "").rstrip("/")
    r = route if route.startswith("/") else "/" + route
    if not p or p == "/":
        return r
    if r == p or r.startswith(p + "/"):
        return r
    return p + r


_HTTP_METHOD_ANNOTATION_RE = re.compile(r'(?i)@(get|post|put|delete|patch)mapping\b')
_HTTP_METHOD_BRACKET_RE = re.compile(r'(?i)\[\s*http(get|post|put|delete|patch)\b')
_HTTP_METHOD_CALL_RE = re.compile(r'(?i)(?:\.|::)\s*(get|post|put|delete|patch|head|options)\s*\(')


def _match_http_method(matched_text: str) -> Optional[str]:
    """Extracts the HTTP verb from a matched route-declaration or client-call fragment.

    Searches only the matched fragment (method word always precedes the route string),
    so route segments like `/get-started` never produce a false method.
    """
    if not matched_text:
        return None
    m = _HTTP_METHOD_ANNOTATION_RE.search(matched_text)
    if m:
        return m.group(1).upper()
    m = _HTTP_METHOD_BRACKET_RE.search(matched_text)
    if m:
        return m.group(1).upper()
    m = _HTTP_METHOD_CALL_RE.search(matched_text)
    if m:
        return m.group(1).upper()
    return None

_NEXT_APP_ROUTE_RE = re.compile(r'^(?:src/)?app/(api/.+?)/route\.[a-zA-Z0-9]+$')
_NEXT_PAGES_ROUTE_RE = re.compile(r'^(?:src/)?pages/(api/.+?)\.[a-zA-Z0-9]+$')
_CONTROLLER_PREFIX_RE = re.compile(r'@RequestMapping\(\s*(?:value\s*=\s*)?["\']([^"\']+)["\']|\[Route\(\s*["\']([^"\']+)["\']\)', re.IGNORECASE)


def _normalize_route_path(p: str) -> str:
    if not p or not isinstance(p, str):
        return ""
    p = p.strip()
    if not p:
        return ""
    p = re.sub(r'^https?://[^/]+', '', p).strip()
    p = p.split('?')[0].split('#')[0].strip()
    if not p:
        return ""
    if not p.startswith('/'):
        p = '/' + p
    # Replace template expressions ${...} or {param}
    p = re.sub(r'\$?\{[^}]+\}', '*', p)
    # Replace Next.js / bracket params [...param] or [param]
    p = re.sub(r'\[[^]]+\]', '*', p)
    # Replace Express / path params :param
    p = re.sub(r':[a-zA-Z_][a-zA-Z0-9_]*', '*', p)
    # Collapse multiple slashes
    p = re.sub(r'/+', '/', p)
    if len(p) > 1 and p.endswith('/'):
        p = p[:-1]
    _, ext = os.path.splitext(p)
    if ext.lower() in _STATIC_ASSET_EXTS:
        return ""
    if p in ("/", "*", "/*"):
        return ""
    return p


def _is_test_file(rel_path: str) -> bool:
    norm = rel_path.replace("\\", "/").lower()
    base = os.path.basename(norm)
    parts = norm.split("/")
    if any(p in ("test", "tests", "__tests__", "spec", "specs") for p in parts[:-1]):
        return True
    return (
        base.startswith("test_")
        or base.endswith(("_test.py", "_test.go", ".test.ts", ".test.tsx", ".test.js", ".test.jsx", ".spec.ts", ".spec.tsx", ".spec.js"))
    )


def _is_generated_file(rel_path: str) -> bool:
    norm = rel_path.replace("\\", "/").lower()
    base = os.path.basename(norm)
    parts = norm.split("/")
    if any(p in ("generated", "__generated__", ".gen", "gen", "swagger", "openapi") for p in parts[:-1]):
        return True
    if any(p.endswith((".gen", "__generated__")) for p in parts[:-1]):
        return True
    return (
        base.startswith(("swagger", "openapi", "generated"))
        or base.endswith((".swagger.ts", ".swagger.js", ".gen.ts", ".gen.js", ".gen.go", ".pb.go", "_pb2.py"))
        or "swaggerapi" in base
        or "openapi" in base
    )

class CodeSymbol:
    """Represents a structural code entity (file, class, or function)."""
    __slots__ = ("name", "kind", "file_path", "line_start", "line_end", "signature", "body")

    def __init__(
        self,
        name: str,
        kind: str,
        file_path: str,
        line_start: int = 0,
        line_end: int = 0,
        signature: str = "",
        body: str = ""
    ):
        self.name = name
        self.kind = kind
        self.file_path = file_path
        self.line_start = line_start
        self.line_end = line_end
        self.signature = signature
        self.body = body

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "file_path": self.file_path,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "signature": self.signature
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CodeSymbol":
        return cls(
            name=data.get("name", ""),
            kind=data.get("kind", "function"),
            file_path=data.get("file_path", ""),
            line_start=int(data.get("line_start", 0)),
            line_end=int(data.get("line_end", 0)),
            signature=data.get("signature", ""),
        )


class CodeGraph:
    """
    Deterministic In-Memory Knowledge Graph of a software repository.
    Nodes: File (`file:...`), Module (`mod:...`), Symbol (`rel_path::qual_name`), Call (`call:...`).
    Edges: Directed dependencies (`CALLS`, `IMPORTS`, `CONTAINS`, resolved `file -> file`).
    """

    def __init__(self, root_dir: str = ".", use_disk_cache: bool = False):
        self.root_dir = os.path.abspath(root_dir)
        self.use_disk_cache = use_disk_cache
        self.symbols: Dict[str, CodeSymbol] = {}
        self.file_symbols: Dict[str, List[str]] = {}       # file_rel_path -> [symbol_keys]
        self.edges: Dict[str, Set[str]] = {}               # caller/importer -> set(callees/imported)
        self.rev_edges: Dict[str, Set[str]] = {}           # callee/imported -> set(callers/importers)
        self.file_mtimes: Dict[str, float] = {}            # file_rel_path -> mtime
        self.file_stats: Dict[str, Tuple[float, int]] = {} # file_rel_path -> (mtime, size)
        self.file_imports: Dict[str, Set[str]] = {}        # file_rel_path -> raw imported modules
        self.file_endpoints: Dict[str, List[Tuple[str, Optional[str], Optional[str]]]] = {}  # rel_path -> [(norm_route, sym_key, http_method)]
        self.file_api_calls: Dict[str, List[Tuple[str, Optional[str], Optional[str]]]] = {}  # rel_path -> [(norm_route, sym_key, http_method)]
        self.file_mounts: Dict[str, List[Tuple[str, str, str]]] = {}  # rel_path -> [(url_prefix, router_ident, module_or_".")]
        self.api_links: List[Dict[str, Any]] = []  # cross-stack edges: frontend call -> backend endpoint
        self.last_scanned: float = 0.0
        self.backend: str = self._detect_backend()

        if self.use_disk_cache and os.environ.get("TOKENSAFE_GRAPH_DISK_CACHE", "1") != "0":
            self._load_disk_cache()

    @staticmethod
    def _detect_backend() -> str:
        try:
            from src.treesitter_backend import TreeSitterBackend
            if TreeSitterBackend.is_available():
                return "treesitter"
        except Exception:
            pass
        return "stdlib"

    def _cache_path(self) -> str:
        cache_dir = os.path.join(resolve_data_dir(), "graph_cache")
        key = hashlib.md5(self.root_dir.encode("utf-8")).hexdigest()[:12]
        return os.path.join(cache_dir, f"{key}.db")

    def _legacy_json_cache_path(self) -> str:
        cache_dir = os.path.join(resolve_data_dir(), "graph_cache")
        key = hashlib.md5(self.root_dir.encode("utf-8")).hexdigest()[:12]
        return os.path.join(cache_dir, f"{key}.json")

    def _clear_graph_state(self) -> None:
        self.symbols.clear()
        self.file_symbols.clear()
        self.edges.clear()
        self.rev_edges.clear()
        self.file_stats.clear()
        self.file_mtimes.clear()
        self.file_imports.clear()
        self.file_endpoints.clear()
        self.file_api_calls.clear()
        self.file_mounts.clear()
        self.api_links.clear()

    def _load_disk_cache(self) -> bool:
        path = self._cache_path()
        if os.path.exists(path):
            try:
                con = sqlite3.connect(path, timeout=5.0)
                try:
                    cur = con.cursor()
                    meta = dict(cur.execute("SELECT key, val FROM graph_meta").fetchall())
                    if (
                        int(meta.get("version", 0)) != _CACHE_SCHEMA_VERSION
                        or meta.get("root_dir") != self.root_dir
                        or meta.get("backend") != self.backend
                    ):
                        return False

                    self.symbols = {}
                    for row in cur.execute("SELECT sym_key, rel_path, name, kind, line_start, line_end, signature FROM symbols").fetchall():
                        self.symbols[row[0]] = CodeSymbol(
                            name=row[2],
                            kind=row[3],
                            file_path=row[1],
                            line_start=int(row[4]),
                            line_end=int(row[5]),
                            signature=row[6],
                        )

                    self.file_symbols = {}
                    for row in cur.execute("SELECT rel_path, sym_key FROM file_symbols ORDER BY rel_path, idx").fetchall():
                        self.file_symbols.setdefault(row[0], []).append(row[1])

                    self.file_stats = {
                        row[0]: (float(row[1]), int(row[2]))
                        for row in cur.execute("SELECT rel_path, mtime, size FROM file_stats").fetchall()
                    }
                    self.file_mtimes = {k: v[0] for k, v in self.file_stats.items()}

                    self.file_imports = {}
                    for row in cur.execute("SELECT rel_path, imported FROM file_imports").fetchall():
                        self.file_imports.setdefault(row[0], set()).add(row[1])

                    self.edges = {}
                    self.rev_edges = {}
                    for row in cur.execute("SELECT source, target FROM edges").fetchall():
                        s, t = row[0], row[1]
                        self.edges.setdefault(s, set()).add(t)
                        self.rev_edges.setdefault(t, set()).add(s)

                    self.file_endpoints = {}
                    try:
                        for row in cur.execute("SELECT rel_path, route, sym_key, method FROM api_endpoints").fetchall():
                            method = row[3] if len(row) > 3 else None
                            self.file_endpoints.setdefault(row[0], []).append((row[1], row[2], method))
                    except sqlite3.OperationalError:
                        pass

                    self.file_api_calls = {}
                    try:
                        for row in cur.execute("SELECT rel_path, route, sym_key, method FROM api_calls").fetchall():
                            method = row[3] if len(row) > 3 else None
                            self.file_api_calls.setdefault(row[0], []).append((row[1], row[2], method))
                    except sqlite3.OperationalError:
                        pass

                    self.file_mounts = {}
                    try:
                        for row in cur.execute("SELECT rel_path, prefix, ident, module FROM file_mounts").fetchall():
                            self.file_mounts.setdefault(row[0], []).append((row[1], row[2], row[3] or ""))
                    except sqlite3.OperationalError:
                        pass

                    self.api_links = []
                    try:
                        for row in cur.execute("SELECT link_json FROM api_links").fetchall():
                            link_obj = json.loads(row[0])
                            if isinstance(link_obj, dict):
                                self.api_links.append(link_obj)
                    except Exception:
                        pass

                    self.last_scanned = float(meta.get("last_scanned", 0.0))
                    return True
                finally:
                    con.close()
            except Exception:
                self._clear_graph_state()
                return False

        # Fallback to legacy JSON cache if present
        legacy_path = self._legacy_json_cache_path()
        if os.path.exists(legacy_path):
            try:
                with open(legacy_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if (
                    data.get("root_dir") == self.root_dir
                    and data.get("backend") == self.backend
                ):
                    self.symbols = {k: CodeSymbol.from_dict(v) for k, v in data.get("symbols", {}).items()}
                    self.file_symbols = {k: list(v) for k, v in data.get("file_symbols", {}).items()}
                    self.edges = {k: set(v) for k, v in data.get("edges", {}).items()}
                    self.rev_edges = {k: set(v) for k, v in data.get("rev_edges", {}).items()}
                    self.file_stats = {
                        k: (float(v[0]), int(v[1]))
                        for k, v in data.get("file_stats", {}).items()
                        if isinstance(v, (list, tuple)) and len(v) == 2
                    }
                    self.file_mtimes = {k: v[0] for k, v in self.file_stats.items()}
                    self.file_imports = {k: set(v) for k, v in data.get("file_imports", {}).items()}
                    self.last_scanned = float(data.get("last_scanned", 0.0))
                    # Migrate to SQLite format
                    self._save_disk_cache()
                    try:
                        os.remove(legacy_path)
                    except OSError:
                        pass
                    return True
            except Exception:
                self._clear_graph_state()
                return False

        return False

    def _save_disk_cache(self) -> None:
        if not self.use_disk_cache or os.environ.get("TOKENSAFE_GRAPH_DISK_CACHE", "1") == "0":
            return
        path = self._cache_path()
        cache_dir = os.path.dirname(path)
        try:
            os.makedirs(cache_dir, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(prefix="graph_", suffix=".db.tmp", dir=cache_dir)
            os.close(fd)
            try:
                con = sqlite3.connect(tmp_path, timeout=5.0)
                try:
                    cur = con.cursor()
                    cur.execute("PRAGMA journal_mode = OFF")
                    cur.execute("PRAGMA synchronous = OFF")
                    cur.execute("CREATE TABLE graph_meta (key TEXT PRIMARY KEY, val TEXT)")
                    cur.execute("CREATE TABLE file_stats (rel_path TEXT PRIMARY KEY, mtime REAL, size INTEGER)")
                    cur.execute("CREATE TABLE symbols (sym_key TEXT PRIMARY KEY, rel_path TEXT, name TEXT, kind TEXT, line_start INTEGER, line_end INTEGER, signature TEXT)")
                    cur.execute("CREATE TABLE file_symbols (rel_path TEXT, sym_key TEXT, idx INTEGER, PRIMARY KEY (rel_path, sym_key))")
                    cur.execute("CREATE TABLE file_imports (rel_path TEXT, imported TEXT, PRIMARY KEY (rel_path, imported))")
                    cur.execute("CREATE TABLE edges (source TEXT, target TEXT, PRIMARY KEY (source, target))")
                    cur.execute("CREATE TABLE api_endpoints (rel_path TEXT, route TEXT, sym_key TEXT, method TEXT)")
                    cur.execute("CREATE TABLE api_calls (rel_path TEXT, route TEXT, sym_key TEXT, method TEXT)")
                    cur.execute("CREATE TABLE file_mounts (rel_path TEXT, prefix TEXT, ident TEXT, module TEXT)")
                    cur.execute("CREATE TABLE api_links (link_json TEXT)")

                    cur.executemany(
                        "INSERT INTO graph_meta VALUES (?, ?)",
                        [
                            ("version", str(_CACHE_SCHEMA_VERSION)),
                            ("root_dir", self.root_dir),
                            ("backend", self.backend),
                            ("last_scanned", str(self.last_scanned)),
                        ],
                    )
                    cur.executemany(
                        "INSERT INTO file_stats VALUES (?, ?, ?)",
                        [(k, v[0], v[1]) for k, v in self.file_stats.items()],
                    )
                    cur.executemany(
                        "INSERT INTO symbols VALUES (?, ?, ?, ?, ?, ?, ?)",
                        [
                            (k, s.file_path, s.name, s.kind, s.line_start, s.line_end, s.signature)
                            for k, s in self.symbols.items()
                        ],
                    )
                    fs_rows = []
                    for rel_path, sym_keys in self.file_symbols.items():
                        for idx, sym_key in enumerate(sym_keys):
                            fs_rows.append((rel_path, sym_key, idx))
                    cur.executemany("INSERT INTO file_symbols VALUES (?, ?, ?)", fs_rows)

                    cur.executemany(
                        "INSERT INTO file_imports VALUES (?, ?)",
                        [(rel_path, imp) for rel_path, imps in self.file_imports.items() for imp in imps],
                    )
                    cur.executemany(
                        "INSERT INTO edges VALUES (?, ?)",
                        [(src, tgt) for src, tgts in self.edges.items() for tgt in tgts],
                    )
                    cur.executemany(
                        "INSERT INTO api_endpoints VALUES (?, ?, ?, ?)",
                        [(rel, ep[0], ep[1], ep[2] if len(ep) > 2 else None) for rel, eps in self.file_endpoints.items() for ep in eps],
                    )
                    cur.executemany(
                        "INSERT INTO api_calls VALUES (?, ?, ?, ?)",
                        [(rel, call[0], call[1], call[2] if len(call) > 2 else None) for rel, calls in self.file_api_calls.items() for call in calls],
                    )
                    cur.executemany(
                        "INSERT INTO file_mounts VALUES (?, ?, ?, ?)",
                        [(rel, prefix, ident, module) for rel, mounts in self.file_mounts.items() for (prefix, ident, module) in mounts],
                    )
                    cur.executemany(
                        "INSERT INTO api_links VALUES (?)",
                        [(json.dumps(lk, ensure_ascii=False),) for lk in self.api_links],
                    )
                    con.commit()
                finally:
                    con.close()
                os.replace(tmp_path, path)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        except OSError:
            pass

    def add_edge(self, source: str, target: str):
        if not source or not target or source == target:
            return
        if source not in self.edges:
            self.edges[source] = set()
        self.edges[source].add(target)

        if target not in self.rev_edges:
            self.rev_edges[target] = set()
        self.rev_edges[target].add(source)

    def _remove_edge(self, source: str, target: str):
        if source in self.edges:
            self.edges[source].discard(target)
        if target in self.rev_edges:
            self.rev_edges[target].discard(source)

    def scan(self, force: bool = False) -> int:
        """
        Fast incremental scan of the workspace.
        Parses modified files only (checked via `(mtime, size)`); returns number of files re-indexed.
        """
        system_roots = {"/", "/Users", "/home", "/root", os.path.expanduser("~")}
        if self.root_dir in system_roots:
            return 0

        current_backend = self._detect_backend()
        if current_backend != self.backend:
            self.backend = current_backend
            force = True

        reindexed = 0
        current_files = set()

        for dirpath, dirnames, filenames in os.walk(self.root_dir):
            dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS and not d.startswith(".")]
            for filename in filenames:
                _, ext = os.path.splitext(filename)
                if ext.lower() in SUPPORTED_EXTS:
                    full_path = os.path.join(dirpath, filename)
                    rel_path = os.path.relpath(full_path, self.root_dir).replace("\\", "/")
                    current_files.add(rel_path)

                    try:
                        st = os.stat(full_path)
                        if not stat.S_ISREG(st.st_mode) or st.st_size > 2 * 1024 * 1024:
                            if rel_path in self.file_symbols or rel_path in self.file_stats:
                                self._purge_file(rel_path)
                                self.file_stats.pop(rel_path, None)
                                self.file_mtimes.pop(rel_path, None)
                                reindexed += 1
                            continue
                        sig = (st.st_mtime, st.st_size)
                    except OSError:
                        continue

                    prev_sig = self.file_stats.get(rel_path)
                    prev_mtime = self.file_mtimes.get(rel_path)
                    changed = (
                        force
                        or prev_sig is None
                        or prev_sig != sig
                        or prev_mtime is None
                        or prev_mtime < st.st_mtime
                    )
                    if changed:
                        self._index_file(full_path, rel_path)
                        self.file_stats[rel_path] = sig
                        self.file_mtimes[rel_path] = st.st_mtime
                        reindexed += 1

        # Purge removed files
        deleted = (set(self.file_stats.keys()) | set(self.file_mtimes.keys())) - current_files
        for del_path in deleted:
            self._purge_file(del_path)
            self.file_stats.pop(del_path, None)
            self.file_mtimes.pop(del_path, None)

        if reindexed > 0 or deleted or force:
            self._resolve_local_imports()
            self._link_api_routes()

        self.last_scanned = time.time()
        if reindexed > 0 or deleted:
            self._save_disk_cache()
        return reindexed

    def _purge_file(self, rel_path: str):
        """Removes symbols, imports, and edges belonging to a file."""
        sym_keys = self.file_symbols.pop(rel_path, [])
        self.file_imports.pop(rel_path, None)
        self.file_endpoints.pop(rel_path, None)
        self.file_api_calls.pop(rel_path, None)
        self.file_mounts.pop(rel_path, None)
        for k in sym_keys:
            self.symbols.pop(k, None)
            targets = self.edges.pop(k, set())
            for t in targets:
                if t in self.rev_edges:
                    self.rev_edges[t].discard(k)
            sources = self.rev_edges.pop(k, set())
            for s in sources:
                if s in self.edges:
                    self.edges[s].discard(k)

        # File-level node
        file_node = f"file:{rel_path}"
        for t in self.edges.pop(file_node, set()):
            if t in self.rev_edges:
                self.rev_edges[t].discard(file_node)
        for s in self.rev_edges.pop(file_node, set()):
            if s in self.edges:
                self.edges[s].discard(file_node)

    def _index_file(self, full_path: str, rel_path: str):
        """Indexes a single code file using Tree-sitter (if available) or stdlib parsers."""
        self._purge_file(rel_path)
        try:
            st = os.stat(full_path)
            if not stat.S_ISREG(st.st_mode) or st.st_size > 2 * 1024 * 1024:
                return
            with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError:
            return

        file_node = f"file:{rel_path}"
        self.file_symbols[rel_path] = []
        self.file_imports[rel_path] = set()

        _, ext = os.path.splitext(full_path)
        ext = ext.lower()

        if self._try_index_treesitter(content, rel_path, file_node, ext):
            self._extract_api_routes_and_calls(content, rel_path)
            return

        if ext == ".py":
            self._parse_python(content, rel_path, file_node)
        elif ext == ".rb":
            self._parse_ruby(content, rel_path, file_node)
        elif ext in SUPPORTED_EXTS:
            self._parse_braces(content, rel_path, file_node, ext)

        self._extract_api_routes_and_calls(content, rel_path)

    def _find_enclosing_symbol(self, rel_path: str, line_no: int) -> Optional[str]:
        sym_keys = self.file_symbols.get(rel_path, [])
        if not sym_keys:
            return None
        candidates = []
        for sk in sym_keys:
            s = self.symbols.get(sk)
            if s and s.line_start <= line_no <= s.line_end:
                candidates.append((sk, s))
        if candidates:
            candidates.sort(key=lambda item: (0 if item[1].kind in ("function", "method") else 1, item[1].line_end - item[1].line_start))
            return candidates[0][0]

        for sk in sym_keys:
            s = self.symbols.get(sk)
            if s and 0 <= s.line_start - line_no <= 4:
                return sk
        return None

    def _extract_api_routes_and_calls(self, content: str, rel_path: str) -> None:
        self.file_endpoints[rel_path] = []
        self.file_api_calls[rel_path] = []
        self.file_mounts[rel_path] = []

        norm_rel = rel_path.replace("\\", "/")

        class_prefix = ""
        m_prefix = _CONTROLLER_PREFIX_RE.search(content)
        if m_prefix:
            class_prefix = m_prefix.group(1) or m_prefix.group(2) or ""

        # Framework-level prefix: routes are declared relative to it.
        file_prefix = ""
        for rx in _FRAMEWORK_PREFIX_PATTERNS:
            m = rx.search(content)
            if m:
                file_prefix = (m.group(1) or "").strip()
                break

        # Mounted sub-routers: the prefix lives where the router is mounted,
        # the routes where it is declared, so record the pair plus where the
        # identifier comes from ("." = declared in this same file).
        mount_matches = list(_MOUNT_RE.finditer(content))
        if mount_matches:
            bindings = _import_bindings(content)
            for m in mount_matches:
                prefix = (m.group(1) or "").strip()
                ident = (m.group(2) or "").strip()
                if not (prefix and ident):
                    continue
                module = bindings.get(ident) or ""
                if not module and re.search(r'\b(?:const|let|var|function|class)\s+' + re.escape(ident) + r'\b', content):
                    module = "."
                self.file_mounts[rel_path].append((prefix, ident, module))

        # 1. Next.js file-based API routes
        m_app = _NEXT_APP_ROUTE_RE.match(norm_rel)
        m_pages = _NEXT_PAGES_ROUTE_RE.match(norm_rel)
        if m_app or m_pages:
            route_raw = "/" + (m_app.group(1) if m_app else m_pages.group(1))
            norm_route = _normalize_route_path(route_raw)
            if norm_route:
                sym_keys = self.file_symbols.get(rel_path, [])
                matched_any = False
                for sk in sym_keys:
                    s = self.symbols.get(sk)
                    if s and s.name.upper() in {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS", "HANDLER", "DEFAULT"}:
                        method = s.name.upper() if s.name.upper() in {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"} else None
                        self.file_endpoints[rel_path].append((norm_route, sk, method))
                        matched_any = True
                if not matched_any:
                    self.file_endpoints[rel_path].append((norm_route, None, None))

        # 2. Backend route definitions
        same_file_mounts: Dict[str, List[str]] = {}
        if not file_prefix:
            for (prefix, ident, module) in self.file_mounts[rel_path]:
                if module == ".":
                    same_file_mounts.setdefault(ident, []).append(prefix)
        for rx, _default_method in _BACKEND_ROUTE_PATTERNS:
            for match in rx.finditer(content):
                route_raw = match.group(1)
                if class_prefix and not route_raw.startswith(class_prefix):
                    full_route = class_prefix.rstrip("/") + "/" + route_raw.lstrip("/")
                else:
                    full_route = route_raw
                if file_prefix:
                    full_route = _join_route(file_prefix, full_route)
                # Same-file mount: app.use("/api", router) prefixes only the routes
                # declared on `router`, so app.get("/health") in the same file keeps
                # its own path. A mounted route is not addressable unprefixed, so
                # only the composed form is recorded.
                line_no = content[:match.start()].count("\n") + 1
                method = _match_http_method(match.group(0))
                receiver_match = _RECEIVER_RE.match(match.group(0))
                mount_prefixes = same_file_mounts.get(receiver_match.group(1), []) if receiver_match else []
                if mount_prefixes:
                    for mount_prefix in mount_prefixes:
                        norm_composed = _normalize_route_path(_join_route(mount_prefix, full_route))
                        if norm_composed:
                            self.file_endpoints[rel_path].append(
                                (norm_composed, self._find_enclosing_symbol(rel_path, line_no), method)
                            )
                    continue
                norm_route = _normalize_route_path(full_route)
                if not norm_route:
                    continue
                self.file_endpoints[rel_path].append((norm_route, self._find_enclosing_symbol(rel_path, line_no), method))

        # 3. Frontend API call sites
        for rx, default_method in _FRONTEND_CALL_PATTERNS:
            for match in rx.finditer(content):
                route_raw = match.group(1)
                norm_route = _normalize_route_path(route_raw)
                if not norm_route:
                    continue
                line_no = content[:match.start()].count("\n") + 1
                sym_key = self._find_enclosing_symbol(rel_path, line_no)
                method = _match_http_method(match.group(0)) or default_method
                self.file_api_calls[rel_path].append((norm_route, sym_key, method))

    def _resolve_relative_module(self, from_rel: str, spec: str) -> Optional[str]:
        """Resolves a relative import specifier to an indexed file, if any."""
        if not spec or not spec.startswith("."):
            return None
        base = os.path.normpath(os.path.join(os.path.dirname(from_rel), spec)).replace("\\", "/")
        candidates = [base]
        for ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py", ".go", ".rs", ".php", ".java"):
            candidates.append(base + ext)
        for ext in (".ts", ".tsx", ".js", ".jsx"):
            candidates.append(base + "/index" + ext)
        for cand in candidates:
            if cand in self.file_symbols:
                return cand
        return None

    @staticmethod
    def _route_matches(path: str, pattern_route: str) -> bool:
        if not path or not pattern_route:
            return False
        if path == pattern_route:
            return True
        try:
            pattern = "^" + re.escape(pattern_route).replace(r"\*", r"[^/]+") + "$"
            if re.match(pattern, path):
                return True
        except re.error:
            pass
        try:
            pattern2 = "^" + re.escape(path).replace(r"\*", r"[^/]+") + "$"
            if re.match(pattern2, pattern_route):
                return True
        except re.error:
            pass
        return False

    def _link_api_routes(self) -> None:
        """
        Cross-boundary semantic linking between frontend client API calls
        and backend route endpoints across languages and frameworks.
        Also records OUMV cross-stack relationships (with evidence) in `api_links`.
        """
        self.api_links = []
        try:
            # Mounted routers: `app.use("/wheel", wheelRoutes)` prefixes the routes
            # declared in the router's own file. Resolve the module binding first
            # (a router constant is not always a graph symbol), then fall back to
            # a uniquely-named symbol.
            name_to_files: Dict[str, Set[str]] = {}
            for sym in self.symbols.values():
                name_to_files.setdefault(sym.name, set()).add(sym.file_path)
            mounted: Dict[str, List[str]] = {}
            for mount_rel, mounts in self.file_mounts.items():
                for prefix, ident, module in mounts:
                    target = None
                    if module == ".":
                        target = mount_rel
                    elif module:
                        target = self._resolve_relative_module(mount_rel, module)
                    if not target:
                        owners = name_to_files.get(ident) or set()
                        if len(owners) == 1:
                            target = next(iter(owners))
                    if target and target != mount_rel:
                        mounted.setdefault(target, []).append(prefix)

            endpoint_map: Dict[str, List[Tuple[str, Optional[str], Optional[str]]]] = {}
            for rel_path, endpoints in self.file_endpoints.items():
                for norm_route, sym_key, method in endpoints:
                    if not norm_route:
                        continue
                    effective = [norm_route] + [
                        _join_route(prefix, norm_route) for prefix in mounted.get(rel_path, [])
                    ]
                    for eff_route in dict.fromkeys(effective):
                        endpoint_map.setdefault(eff_route, []).append((rel_path, sym_key, method))

            if not endpoint_map:
                return

            seen_links = set()
            for caller_rel, calls in self.file_api_calls.items():
                caller_file_node = f"file:{caller_rel}"
                for norm_route, caller_sym, caller_method in calls:
                    if not norm_route:
                        continue
                    matched_endpoints: List[Tuple[str, str, Optional[str], Optional[str]]] = []
                    if norm_route in endpoint_map:
                        matched_endpoints.extend((norm_route, rel, sym, m) for (rel, sym, m) in endpoint_map[norm_route])

                    for ep_route, eps in endpoint_map.items():
                        if ep_route != norm_route and ("*" in ep_route or "*" in norm_route):
                            if self._route_matches(norm_route, ep_route):
                                matched_endpoints.extend((ep_route, rel, sym, m) for (rel, sym, m) in eps)

                    for ep_route, ep_rel, ep_sym, ep_method in matched_endpoints:
                        if ep_rel == caller_rel:
                            continue
                        ep_file_node = f"file:{ep_rel}"
                        self.add_edge(caller_file_node, ep_file_node)

                        src = caller_sym or caller_file_node
                        dst = ep_sym or ep_file_node
                        if src != dst:
                            self.add_edge(src, dst)

                        link_key = (caller_rel, caller_sym or "", norm_route, ep_rel, ep_sym or "", ep_route)
                        if link_key not in seen_links:
                            seen_links.add(link_key)
                            self.api_links.append({
                                "source": {
                                    "type": "frontend.action",
                                    "route": norm_route,
                                    "method": caller_method,
                                    "file": caller_rel,
                                    "symbol": caller_sym,
                                },
                                "target": {
                                    "type": "backend.endpoint",
                                    "route": ep_route,
                                    "method": ep_method,
                                    "file": ep_rel,
                                    "symbol": ep_sym,
                                },
                            })
        except Exception:
            pass

    def _register_symbol(self, rel_path: str, file_node: str, symbol: CodeSymbol, sym_key: str):
        self.symbols[sym_key] = symbol
        if sym_key not in self.file_symbols[rel_path]:
            self.file_symbols[rel_path].append(sym_key)
        self.add_edge(file_node, sym_key)

    def _try_index_treesitter(self, code: str, rel_path: str, file_node: str, ext: str) -> bool:
        if self.backend != "treesitter":
            return False
        try:
            from src.treesitter_backend import TreeSitterBackend
            data = TreeSitterBackend.extract_graph_data(code, ext, rel_path)
        except Exception:
            return False
        if data is None:
            return False

        for mod in data.get("imports", []):
            self.file_imports[rel_path].add(mod)
            self.add_edge(file_node, f"mod:{mod}")
        for sym in data.get("imported_symbols", []):
            self.add_edge(file_node, f"sym:{sym}")

        for item in data.get("symbols", []):
            symbol = CodeSymbol(
                name=item["name"],
                kind=item["kind"],
                file_path=rel_path,
                line_start=item["line_start"],
                line_end=item["line_end"],
                signature=item["signature"],
            )
            self._register_symbol(rel_path, file_node, symbol, item["sym_key"])

        for caller_key, callee_name in data.get("calls", []):
            self.add_edge(caller_key, f"call:{callee_name}")

        return True

    def _parse_python(self, code: str, rel_path: str, file_node: str):
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return

        lines = code.splitlines()

        def walk_py(node: ast.AST, class_name: Optional[str] = None, fn_key: Optional[str] = None):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.Import):
                    for alias in child.names:
                        self.file_imports[rel_path].add(alias.name)
                        self.add_edge(file_node, f"mod:{alias.name}")
                elif isinstance(child, ast.ImportFrom):
                    mod_prefix = "." * (child.level or 0)
                    mod_name = f"{mod_prefix}{child.module or ''}"
                    if mod_name:
                        self.file_imports[rel_path].add(mod_name)
                        self.add_edge(file_node, f"mod:{mod_name}")
                    for alias in child.names:
                        if alias.name != "*":
                            self.add_edge(file_node, f"sym:{alias.name}")
                elif isinstance(child, ast.ClassDef):
                    start = getattr(child, "lineno", 1)
                    end = getattr(child, "end_lineno", start)
                    qual = f"{class_name}::{child.name}" if class_name else child.name
                    sym_key = f"{rel_path}::{qual}"
                    sig = lines[start - 1].strip() if 0 < start <= len(lines) else child.name
                    symbol = CodeSymbol(
                        name=child.name,
                        kind="class",
                        file_path=rel_path,
                        line_start=start,
                        line_end=end,
                        signature=sig,
                    )
                    self._register_symbol(rel_path, file_node, symbol, sym_key)
                    walk_py(child, class_name=qual, fn_key=None)
                elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    start = getattr(child, "lineno", 1)
                    end = getattr(child, "end_lineno", start)
                    qual = f"{class_name}::{child.name}" if class_name else child.name
                    sym_key = f"{rel_path}::{qual}"
                    sig = lines[start - 1].strip() if 0 < start <= len(lines) else child.name
                    symbol = CodeSymbol(
                        name=child.name,
                        kind="function",
                        file_path=rel_path,
                        line_start=start,
                        line_end=end,
                        signature=sig,
                    )
                    self._register_symbol(rel_path, file_node, symbol, sym_key)
                    walk_py(child, class_name=class_name, fn_key=sym_key)
                else:
                    if isinstance(child, ast.Call) and fn_key:
                        callee_name = None
                        if isinstance(child.func, ast.Name):
                            callee_name = child.func.id
                        elif isinstance(child.func, ast.Attribute):
                            callee_name = child.func.attr
                        if callee_name and callee_name not in _SKIP_CALLS:
                            self.add_edge(fn_key, f"call:{callee_name}")
                    walk_py(child, class_name=class_name, fn_key=fn_key)

        walk_py(tree)

    def _parse_braces(self, code: str, rel_path: str, file_node: str, ext: str):
        from src.skeletonizer import BraceLexer, Skeletonizer

        lines = code.splitlines()
        count_lines = Skeletonizer.blank_template_literals(lines)
        lexer = BraceLexer()

        # Strip comments only (keep string literals intact for import path extraction)
        no_block_comments = re.sub(r"/\*.*?\*/", "", code, flags=re.DOTALL)
        cleaned_text = re.sub(r"//.*$", "", no_block_comments, flags=re.MULTILINE)
        if ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
            imp_re = re.compile(
                r"""(?:(?:import|export)\s+(?:.*?\s+from\s+)?['"]([^'"]+)['"]|require\(\s*['"]([^'"]+)['"]\s*\))"""
            )
            for m in imp_re.finditer(cleaned_text):
                mod = m.group(1) or m.group(2)
                if mod:
                    self.file_imports[rel_path].add(mod)
                    self.add_edge(file_node, f"mod:{mod}")
        elif ext == ".go":
            for m in re.finditer(r'^\s*import\s+(?:[A-Za-z_.]+\s+)?"([^"]+)"', cleaned_text, re.MULTILINE):
                self.file_imports[rel_path].add(m.group(1))
                self.add_edge(file_node, f"mod:{m.group(1)}")
            for blk in re.finditer(r'import\s*\((.*?)\)', cleaned_text, re.DOTALL):
                for m in re.finditer(r'"([^"]+)"', blk.group(1)):
                    self.file_imports[rel_path].add(m.group(1))
                    self.add_edge(file_node, f"mod:{m.group(1)}")
        elif ext in (".c", ".h", ".cpp", ".cc", ".cxx", ".hpp"):
            for m in re.finditer(r'^\s*#include\s+[<"]([^>"]+)[>"]', cleaned_text, re.MULTILINE):
                self.file_imports[rel_path].add(m.group(1))
                self.add_edge(file_node, f"mod:{m.group(1)}")
        else:
            for m in re.finditer(r'^\s*(?:pub\s+)?(?:use|import|using)\s+([^;\n]+)', cleaned_text, re.MULTILINE):
                mod = m.group(1).strip().rstrip(";")
                if mod:
                    self.file_imports[rel_path].add(mod)
                    self.add_edge(file_node, f"mod:{mod}")

        container_re = re.compile(
            r'^(?:export\s+(?:default\s+)?)?(?:pub(?:\([^)]*\))?\s+|public\s+|private\s+|protected\s+|internal\s+|abstract\s+|final\s+|sealed\s+|open\s+|data\s+)*'
            r'(?:class|interface|struct|impl(?:\s+[A-Za-z0-9_<>,\s]+?\s+for)?|trait|enum|record|protocol|extension|namespace|type)\s+([A-Za-z0-9_]+)'
        )
        go_struct_re = re.compile(r'^type\s+([A-Za-z0-9_]+)\s+(?:struct|interface)\b')
        ts_method_re = re.compile(
            r'^(?:(?:public|private|protected|static|readonly|async|override|get|set)\s+)*([A-Za-z_$][A-Za-z0-9_$]*)\s*(?:<[^>]+>)?\s*\([^)]*\)\s*(?::\s*[^{;]+)?\{'
        )
        call_re = re.compile(r'\b([a-zA-Z_$][a-zA-Z0-9_$]{1,})\s*\(')

        depth = 0
        sym_stack: List[Tuple[str, int, str]] = []
        pending_header: Optional[Tuple[str, str, int, str]] = None

        for idx, (raw_line, count_src) in enumerate(zip(lines, count_lines), 1):
            clean = lexer.clean_line(count_src).strip()
            open_b, close_b = lexer.count_braces(count_src)

            matched_name: Optional[str] = None
            matched_kind: Optional[str] = None
            matched_sig = raw_line.strip()
            matched_start = idx

            if clean:
                cm = container_re.match(clean) or (go_struct_re.match(clean) if ext == ".go" else None)
                if cm:
                    matched_name = cm.group(1)
                    matched_kind = "class"
                else:
                    fn_info = Skeletonizer._extract_fn(clean)
                    if fn_info and fn_info[0] != "anonymous":
                        matched_name, raw_kind = fn_info
                        matched_kind = "function"
                        if raw_kind in ("arrow_multiline", "method_multiline", "fn_decl", "go_func", "rust_fn") and "{" not in clean:
                            if not clean.endswith(";"):
                                pending_header = (matched_name, matched_kind, idx, matched_sig)
                            matched_name = None
                    elif ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs") and any(k == "class" for _, _, k in sym_stack):
                        mm = ts_method_re.match(clean)
                        if mm and mm.group(1) not in _SKIP_CALLS:
                            matched_name = mm.group(1)
                            matched_kind = "function"

            if pending_header and not matched_name:
                if "{" in clean:
                    matched_name, matched_kind, matched_start, matched_sig = pending_header
                    pending_header = None
                elif ";" in clean:
                    pending_header = None

            if matched_name and matched_kind:
                class_ctx = [sk.split("::", 1)[-1] for sk, _, k in sym_stack if k == "class"]
                qual = f"{class_ctx[-1]}::{matched_name}" if (class_ctx and matched_kind == "function") else matched_name
                sym_key = f"{rel_path}::{qual}"
                symbol = CodeSymbol(
                    name=matched_name,
                    kind=matched_kind,
                    file_path=rel_path,
                    line_start=matched_start,
                    line_end=idx,
                    signature=matched_sig,
                )
                self._register_symbol(rel_path, file_node, symbol, sym_key)
                if open_b > 0:
                    sym_stack.append((sym_key, depth + 1, matched_kind))

            active_fn = next((sk for sk, _, k in reversed(sym_stack) if k == "function"), None)
            if active_fn and clean:
                for cm in call_re.finditer(clean):
                    callee = cm.group(1)
                    if callee != matched_name and callee not in _SKIP_CALLS:
                        self.add_edge(active_fn, f"call:{callee}")

            depth += open_b - close_b
            while sym_stack and depth < sym_stack[-1][1]:
                finished_key, _, _ = sym_stack.pop()
                if finished_key in self.symbols:
                    self.symbols[finished_key].line_end = idx

    def _parse_ruby(self, code: str, rel_path: str, file_node: str):
        """Stdlib Ruby parser: keyword-delimited blocks (`def`/`class`/`module` ...
        `end`) instead of braces. Mirrors the brace-parser conventions (kinds,
        quals, call edges) so both backends stay in parity."""
        lines = code.splitlines()
        require_re = re.compile(r'^\s*require(?:_relative)?\s+["\']([^"\']+)["\']')
        opener_re = re.compile(
            r'^\s*(?:def|class|module|if|unless|while|until|case|for|begin)\b|\bdo\s*(?:\|[^|]*\|)?\s*$'
        )
        container_re = re.compile(r'^\s*(?:class|module)\s+([A-Z][A-Za-z0-9_:]*)')
        method_re = re.compile(r'^\s*def\s+(?:self\.)?([a-zA-Z_][a-zA-Z0-9_]*[?!]?)')
        call_re = re.compile(r'\b([a-zA-Z_][a-zA-Z0-9_]*[?!]?)\s*\(')
        end_re = re.compile(r'\bend\b')

        # Stack items: (sym_key|None, kind|None, qual|None). None entries track
        # non-symbol keyword blocks (if/while/do/...) so `end` counting balances.
        stack: List[Tuple[Optional[str], Optional[str], Optional[str]]] = []

        for idx, raw in enumerate(lines, 1):
            no_comment = raw.split("#", 1)[0]
            # Blank out string literals so `end`-like words inside strings never count
            no_str = re.sub(r'"[^"]*"', '""', no_comment)
            no_str = re.sub(r"'[^']*'", "''", no_str)
            if not no_str.strip():
                continue

            rm = require_re.match(raw)
            if rm:
                self.file_imports[rel_path].add(rm.group(1))
                self.add_edge(file_node, f"mod:{rm.group(1)}")

            opens = len(opener_re.findall(no_str))
            closes = len(end_re.findall(no_str))

            matched_name: Optional[str] = None
            matched_kind: Optional[str] = None
            cm = container_re.match(no_str)
            if cm:
                matched_kind, matched_name = "class", cm.group(1)
            else:
                mm = method_re.match(no_str)
                if mm:
                    matched_kind, matched_name = "function", mm.group(1)

            if matched_name and matched_kind:
                class_ctx = [e[2] for e in stack if e[1] == "class"]
                qual = f"{class_ctx[-1]}::{matched_name}" if class_ctx else matched_name
                sym_key = f"{rel_path}::{qual}"
                symbol = CodeSymbol(
                    name=matched_name,
                    kind=matched_kind,
                    file_path=rel_path,
                    line_start=idx,
                    line_end=idx,
                    signature=raw.strip(),
                )
                self._register_symbol(rel_path, file_node, symbol, sym_key)
                stack.append((sym_key, matched_kind, qual))
            elif opens:
                stack.append((None, None, None))

            active_fn = next((e for e in reversed(stack) if e[1] == "function"), None)
            if active_fn and active_fn[0]:
                for cmm in call_re.finditer(no_str):
                    callee = cmm.group(1)
                    if callee != matched_name and callee not in _SKIP_CALLS:
                        self.add_edge(active_fn[0], f"call:{callee}")

            for _ in range(closes):
                if not stack:
                    break
                entry = stack.pop()
                if entry[0]:
                    sym = self.symbols.get(entry[0])
                    if sym:
                        sym.line_end = idx

    def _resolve_local_imports(self) -> None:
        """Resolves raw `file_imports` strings to real `file:<rel_path>` edges."""
        indexed_files = set(self.file_symbols.keys())
        if not indexed_files:
            return

        for rel_path in indexed_files:
            src_node = f"file:{rel_path}"
            existing = [t for t in self.edges.get(src_node, set()) if t.startswith("file:")]
            for t in existing:
                self._remove_edge(src_node, t)

        dir_to_files: Dict[str, List[str]] = {}
        for fp in indexed_files:
            d = os.path.dirname(fp)
            dir_to_files.setdefault(d, []).append(fp)

        for rel_path, mods in self.file_imports.items():
            src_node = f"file:{rel_path}"
            src_dir = os.path.dirname(rel_path)
            ext = os.path.splitext(rel_path)[1].lower()

            for mod in mods:
                for target_rel in self._resolve_single_import(mod, src_dir, ext, indexed_files, dir_to_files):
                    if target_rel != rel_path:
                        self.add_edge(src_node, f"file:{target_rel}")

    @staticmethod
    def _resolve_single_import(
        mod: str,
        src_dir: str,
        ext: str,
        indexed_files: Set[str],
        dir_to_files: Dict[str, List[str]],
    ) -> List[str]:
        if not mod:
            return []

        if ext == ".py":
            if mod.startswith("."):
                dots = len(mod) - len(mod.lstrip("."))
                rest = mod[dots:].replace(".", "/")
                base_dir = src_dir
                for _ in range(dots - 1):
                    base_dir = os.path.dirname(base_dir)
                prefix = os.path.normpath(os.path.join(base_dir, rest)).replace("\\", "/") if rest else base_dir
            else:
                prefix = mod.replace(".", "/")
            prefix = prefix.lstrip("./")
            for cand in (f"{prefix}.py", f"{prefix}/__init__.py"):
                if cand in indexed_files:
                    return [cand]
            return []

        if ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
            if not mod.startswith("."):
                return []
            base = os.path.normpath(os.path.join(src_dir, mod)).replace("\\", "/").lstrip("./")
            for cand in (
                base,
                f"{base}.ts", f"{base}.tsx", f"{base}.js", f"{base}.jsx", f"{base}.mjs", f"{base}.cjs",
                f"{base}/index.ts", f"{base}/index.tsx", f"{base}/index.js", f"{base}/index.mjs",
            ):
                if cand in indexed_files:
                    return [cand]
            return []

        if ext == ".go":
            mod_clean = mod.strip("/")
            for d, files in dir_to_files.items():
                if d and (mod_clean == d or mod_clean.endswith(f"/{d}")):
                    return [f for f in files if f.endswith(".go")]
            return []

        if ext in (".c", ".h", ".cpp", ".cc", ".cxx", ".hpp"):
            cand = os.path.normpath(os.path.join(src_dir, mod)).replace("\\", "/").lstrip("./")
            return [cand] if cand in indexed_files else []

        if ext == ".rs" and mod.startswith(("crate::", "super::", "self::")):
            parts = mod.split("::")[1:]
            if parts:
                rel = "/".join(parts)
                for cand in (f"{rel}.rs", f"{rel}/mod.rs", f"src/{rel}.rs", f"src/{rel}/mod.rs"):
                    if cand in indexed_files:
                        return [cand]
            return []

        return []

    # -------------------------------------------------------------
    # 1. God Nodes Analysis (Import & Call Centrality)
    # -------------------------------------------------------------
    def get_god_nodes(self, top_n: int = 5) -> List[Dict[str, Any]]:
        """
        Calculates architectural hub centrality across files.
        Prioritizes resolved file-to-file imports and import-verified symbol calls,
        and downweights test files when production files are present.
        """
        has_prod_files = any(not _is_test_file(p) and not _is_generated_file(p) for p in self.file_symbols.keys())
        has_resolved_imports = any(
            t.startswith("file:")
            for src, targets in self.edges.items()
            if src.startswith("file:")
            for t in targets
        )

        scores: Dict[str, Dict[str, Any]] = {}

        for rel_path in self.file_symbols.keys():
            file_node = f"file:{rel_path}"
            out_degree = len(self.edges.get(file_node, set()))

            importing_files = {
                s[5:] for s in self.rev_edges.get(file_node, set()) if s.startswith("file:")
            }
            in_degree = len(importing_files)

            for sym_key in self.file_symbols.get(rel_path, []):
                sym = self.symbols.get(sym_key)
                sym_name = sym.name if sym else sym_key.split("::")[-1].split(".")[-1]
                for caller_key in self.rev_edges.get(f"call:{sym_name}", set()):
                    caller_file = caller_key.split("::", 1)[0]
                    if caller_file == rel_path:
                        continue
                    if not has_resolved_imports or caller_file in importing_files:
                        in_degree += 1

            raw_centrality = in_degree * 2 + out_degree
            if has_prod_files and _is_test_file(rel_path):
                rank_score = raw_centrality * 0.1
            elif has_prod_files and _is_generated_file(rel_path):
                rank_score = raw_centrality * 0.01
            elif has_prod_files and in_degree == 0:
                rank_score = raw_centrality * 0.15
            else:
                rank_score = float(in_degree * 3 + out_degree)
            scores[rel_path] = {
                "file": rel_path,
                "centrality": raw_centrality,
                "dependents": in_degree,
                "dependencies": out_degree,
                "symbols_count": len(self.file_symbols.get(rel_path, [])),
                "_rank": rank_score,
            }

        ranked = sorted(scores.values(), key=lambda x: (x["_rank"], x["centrality"]), reverse=True)
        result = []
        for item in ranked[:top_n]:
            item.pop("_rank", None)
            result.append(item)
        return result

    # -------------------------------------------------------------
    # 2. Neighborhood & Graph-Guided Skeletonization
    # -------------------------------------------------------------
    def _match_target_keys(self, target_symbol: str, target_file: Optional[str] = None) -> List[str]:
        if target_symbol in self.symbols:
            return [target_symbol]

        norm_target = target_symbol.replace(".", "::")
        candidates = []
        for k, sym in self.symbols.items():
            if sym.name == target_symbol or k.endswith(f"::{target_symbol}") or k.endswith(f"::{norm_target}"):
                candidates.append(k)

        if target_file:
            in_file = [k for k in candidates if k.startswith(f"{target_file}::")]
            if in_file:
                return in_file
        return candidates

    def _resolve_callee_keys(self, callee_name: str, caller_file: str) -> List[str]:
        imported_files = {
            t[5:] for t in self.edges.get(f"file:{caller_file}", set()) if t.startswith("file:")
        }
        same_file = []
        imported = []
        others = []

        for k, sym in self.symbols.items():
            if sym.name == callee_name or k.endswith(f"::{callee_name}"):
                if sym.file_path == caller_file:
                    same_file.append(k)
                elif sym.file_path in imported_files:
                    imported.append(k)
                else:
                    others.append(k)

        if same_file or imported:
            return same_file + imported
        return others if len(others) <= 3 else []

    def get_neighborhood(
        self,
        target_symbol: str,
        target_file: Optional[str] = None,
        include_two_hop: bool = False,
    ) -> Dict[str, Set[str]]:
        """
        Returns 0-hop (`target`), 1-hop (`one_hop`), and optionally 2-hop (`two_hop`) related symbols.
        Prioritizes same-file and imported-file edges to eliminate bare-name collisions.
        """
        target_keys = self._match_target_keys(target_symbol, target_file=target_file)
        one_hop: Set[str] = set()

        def _expand_one(tk: str) -> Set[str]:
            found: Set[str] = set()
            sym = self.symbols.get(tk)
            caller_file = sym.file_path if sym else tk.split("::", 1)[0]
            short_name = sym.name if sym else tk.split("::")[-1].split(".")[-1]

            for callee in self.edges.get(tk, set()):
                if callee.startswith("call:"):
                    found.update(self._resolve_callee_keys(callee[5:], caller_file))
                elif callee in self.symbols:
                    found.add(callee)

            importing_files = {
                s[5:] for s in self.rev_edges.get(f"file:{caller_file}", set()) if s.startswith("file:")
            }
            scoped_callers = []
            fallback_callers = []
            for caller in self.rev_edges.get(f"call:{short_name}", set()):
                if caller not in self.symbols or caller == tk:
                    continue
                cf = self.symbols[caller].file_path
                if cf == caller_file or cf in importing_files:
                    scoped_callers.append(caller)
                else:
                    fallback_callers.append(caller)
            if scoped_callers:
                found.update(scoped_callers)
            elif len(fallback_callers) <= 3:
                found.update(fallback_callers)

            # Direct incoming edges (e.g. cross-boundary API caller symbols)
            for caller in self.rev_edges.get(tk, set()):
                if caller in self.symbols and caller != tk:
                    found.add(caller)

            found.discard(tk)
            return found

        for tk in target_keys:
            one_hop.update(_expand_one(tk))
        one_hop -= set(target_keys)

        result: Dict[str, Set[str]] = {
            "target": set(target_keys),
            "one_hop": one_hop,
        }

        if include_two_hop:
            two_hop: Set[str] = set()
            for ok in one_hop:
                two_hop.update(_expand_one(ok))
            two_hop -= set(target_keys)
            two_hop -= one_hop
            result["two_hop"] = two_hop

        return result

    def graph_guided_skeletonize(
        self,
        full_path: str,
        target_symbol: Optional[str] = None,
        include_deps: bool = True,
        dep_mode: str = "full",
        collapse_imports: bool = False,
    ) -> str:
        """
        Surgically folds code based on graph connectivity:
        - target_symbol: Retains FULL function body.
        - 1-hop dependencies:
            * include_deps=True, dep_mode='full': retains full 1-hop bodies in file.
            * include_deps=True, dep_mode='signature': folds 1-hop bodies into signatures
              and prepends compact 1-line external dependency signatures.
            * include_deps=False: 0-hop surgical single-symbol isolation.
        - 2-hop / Unrelated functions in the same file: Stripped to AST signatures (ellipsis).
        """
        try:
            with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                code = f.read()
        except OSError:
            return ""

        from src.skeletonizer import Skeletonizer
        _, ext = os.path.splitext(full_path)
        ext = ext.lower()

        if not target_symbol:
            return Skeletonizer.skeletonize_code(code, ext, collapse_imports=collapse_imports)

        try:
            target_rel = os.path.relpath(os.path.abspath(full_path), self.root_dir).replace("\\", "/")
        except ValueError:
            target_rel = ""

        keep_full_names: Set[str] = {target_symbol}
        neighborhood = self.get_neighborhood(target_symbol, target_file=target_rel or None)
        for tk in neighborhood.get("target", set()):
            qual = tk.split("::", 1)[-1]
            keep_full_names.add(qual)
            keep_full_names.add(qual.replace("::", "."))
            sym = self.symbols.get(tk)
            if sym:
                keep_full_names.add(sym.name)

        external_dep_lines: List[str] = []
        mode = (dep_mode or "full").strip().lower()

        if include_deps:
            one_hop = sorted(neighborhood.get("one_hop", set()))
            if mode == "signature":
                comment_prefix = "#" if ext == ".py" else "//"
                for k in one_hop:
                    sym = self.symbols.get(k)
                    if sym and sym.file_path != target_rel and len(external_dep_lines) < 10:
                        external_dep_lines.append(
                            f"{comment_prefix}   - {sym.file_path} (L{sym.line_start}): {sym.signature}"
                        )
            else:
                for k in one_hop:
                    qual = k.split("::", 1)[-1]
                    keep_full_names.add(qual)
                    keep_full_names.add(qual.replace("::", "."))
                    sym = self.symbols.get(k)
                    if sym:
                        keep_full_names.add(sym.name)

        skel = Skeletonizer.skeletonize_code(
            code,
            ext,
            keep_symbols=keep_full_names,
            collapse_imports=collapse_imports,
        )
        if external_dep_lines:
            comment_prefix = "#" if ext == ".py" else "//"
            dep_header = f"{comment_prefix} 1-hop external dependencies (signatures):\n" + "\n".join(external_dep_lines)
            return dep_header + "\n\n" + skel
        return skel

    # -------------------------------------------------------------
    # 3. API Surface Generator (tokensafe-out/API_SURFACE.md)
    # -------------------------------------------------------------
    # API-surface map guards (QA sweep finding: on real repos the map exploded to
    # 114M tokens — minified/generated bundles leaked WHOLE LINES into symbol
    # signatures (157KB each) and testdata/vendor files were not filtered).
    _SURFACE_SKIP_DIR_SEGMENTS = frozenset((
        "tests", "test", "testdata", "test_fixtures", "__tests__", "__mocks__",
        "spec", "specs", "fixtures", "mocks", "examples", "docs", "doc",
        "vendor", "third_party", "thirdparty", "node_modules", "dist", "build",
        "out", "target", "__pycache__", ".venv", "venv", "generated",
    ))
    _SURFACE_SKIP_FILE_PATTERNS = (".min.js", ".min.css", ".bundle.js", ".map", ".d.ts")
    _SURFACE_SIG_MAX = 160        # per-symbol signature cap (chars)
    _SURFACE_BODY_BUDGET = 60_000  # whole-map body budget (chars, ~15k tokens)

    @classmethod
    def _surface_skip_path(cls, rel_path: str) -> bool:
        low = rel_path.lower()
        if any(p in low for p in cls._SURFACE_SKIP_FILE_PATTERNS):
            return True
        if low.endswith((".lock", ".sum", ".lockb")):
            return True
        return any(seg in cls._SURFACE_SKIP_DIR_SEGMENTS
                   for seg in low.replace("\\", "/").split("/")[:-1])

    @classmethod
    def _surface_signature(cls, sig: str) -> str:
        sig = (sig or "").strip()
        if not sig:
            return ""
        # Cut minified/generated one-liners at the declaration boundary: the
        # text right before the first '{' of a function declaration ends with
        # ')' (e.g. 'var X=function(A){'); interior dict literals keep intact.
        cut = re.search(r"[{;]", sig)
        if cut and cut.start() >= 1 and sig[: cut.start()].rstrip().endswith(")"):
            sig = sig[: cut.start()].rstrip()
        if len(sig) > cls._SURFACE_SIG_MAX:
            sig = sig[: cls._SURFACE_SIG_MAX - 3] + "..."
        return sig

    def generate_api_surface(self, output_path: Optional[str] = None) -> str:
        """
        Generates a high-density, agent-friendly Markdown map of all classes,
        types, and exported interfaces in the project. Bounded: signatures are
        capped, testdata/vendor/minified paths skipped, whole map capped at
        _SURFACE_BODY_BUDGET chars (~15k tokens) — god-node files listed first.
        Writes a file ONLY when output_path is passed explicitly (the MCP tool
        and benchmarks consume the returned text; no implicit repo mutation).
        """
        god_nodes = self.get_god_nodes(top_n=5)
        god_files = {g["file"] for g in god_nodes}
        lines = [
            "# TokenSafe Architecture & API Surface",
            f"*Generated locally at {time.strftime('%Y-%m-%d %H:%M:%S')} • Zero LLM Cost*",
            "",
            "## Architectural Hubs (God Nodes)",
            "The foundational modules that connect the largest portion of the project:",
            ""
        ]

        for g in god_nodes:
            lines.append(f"- **`{g['file']}`**: {g['dependents']} dependents, {g['dependencies']} dependencies ({g['symbols_count']} exported symbols)")

        lines.extend([
            "",
            "## Interface Surface Map",
            ""
        ])

        sections: List[Tuple[str, List[str]]] = []
        for rel_path, sym_keys in sorted(self.file_symbols.items()):
            if not sym_keys or self._surface_skip_path(rel_path):
                continue
            sym_lines = []
            for k in sym_keys:
                sym = self.symbols.get(k)
                if sym:
                    sym_lines.append(
                        f"- `{sym.kind}` **`{sym.name}`** (line {sym.line_start}): "
                        f"`{self._surface_signature(sym.signature)}`")
            if sym_lines:
                sections.append((rel_path, sym_lines))

        # God-node files first (highest architectural value), then the rest.
        ordered = ([s for s in sections if s[0] in god_files]
                   + [s for s in sections if s[0] not in god_files])
        omitted_files = 0
        used = 0
        for rel_path, sym_lines in ordered:
            block = len(rel_path) + sum(len(l) for l in sym_lines)
            if used + block > self._SURFACE_BODY_BUDGET:
                omitted_files += 1
                continue
            used += block
            lines.append(f"### `{rel_path}`")
            lines.extend(sym_lines)
            lines.append("")

        if omitted_files:
            lines.append(f"*+ {omitted_files} more source files omitted (API surface budget)*")

        output_content = "\n".join(lines)

        if output_path:
            try:
                os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
                with open(output_path, "w", encoding="utf-8") as f:
                    f.write(output_content)
            except OSError:
                pass

        return output_content

    def format_topology_banner(self) -> str:
        """Creates a compact 1-line prompt injection banner for coding agents."""
        god_nodes = self.get_god_nodes(top_n=6)
        if not god_nodes:
            return ""
        # In multi-file projects, Core Hubs must have incoming dependents (dependents > 0)
        hubs = [g for g in god_nodes if g.get("dependents", 0) > 0]
        nodes_to_show = hubs[:4] if hubs else god_nodes[:4]
        items = [f"{g['file']} ({g['dependents']} deps)" for g in nodes_to_show]
        return f"[TokenSafe Architecture Map | Core Hubs: {', '.join(items)}]"

    def get_static_oumv_slice(self, level: str = "L0", target: Optional[str] = None) -> Dict[str, Any]:
        """
        Level 1 Static OUMV Inspection:
        Returns statically discoverable components, API endpoints, routes, and call sites
        directly from the AST graph when runtime dev-servers are disconnected.
        """
        evidence = {
            "provenance": "static",
            "sourceAdapter": "tokensafe-graph-engine",
            "confidence": "confirmed",
        }

        lvl = (level or "L0").upper()
        if lvl == "L2" and target:
            # L2 is the flow contract, and it is answerable offline: handler ->
            # callees -> literal client route.
            return self.get_static_flow(target)
        if lvl == "L0":
            god_nodes = self.get_god_nodes(top_n=5)
            endpoints_count = sum(len(eps) for eps in self.file_endpoints.values())
            calls_count = sum(len(c) for c in self.file_api_calls.values())
            return {
                "level": "L0",
                "mode": "STATIC_OFFLINE_PULSE",
                "status": "OFFLINE_STATIC",
                "summary": f"Indexed {len(self.file_symbols)} files, {len(self.symbols)} symbols, {endpoints_count} API endpoints, {calls_count} client calls, {len(self.api_links)} cross-stack links",
                "totalFiles": len(self.file_symbols),
                "totalSymbols": len(self.symbols),
                "totalEndpoints": endpoints_count,
                "totalClientCalls": calls_count,
                "totalRelationships": len(self.api_links),
                "topGodNodes": [g["file"] for g in god_nodes],
                "evidence": evidence,
            }

        # L1: Discovered API surface & Routes
        endpoints = []
        for rel_path, eps in self.file_endpoints.items():
            for norm_route, sym, method in eps:
                if not target or target.lower() in norm_route.lower() or target.lower() in rel_path.lower():
                    endpoints.append({
                        "route": norm_route,
                        "method": method,
                        "file": rel_path,
                        "symbol": sym,
                        "type": "backend.endpoint",
                    })

        client_calls = []
        for rel_path, calls in self.file_api_calls.items():
            for norm_route, sym, method in calls:
                if not target or target.lower() in norm_route.lower() or target.lower() in rel_path.lower():
                    client_calls.append({
                        "route": norm_route,
                        "method": method,
                        "file": rel_path,
                        "symbol": sym,
                        "type": "frontend.action",
                    })

        # Cross-stack relationships (frontend call -> backend endpoint) with evidence
        relationships = []
        for link in self.api_links:
            if target:
                t = target.lower()
                hay = " ".join([
                    link["source"]["route"] or "", link["source"]["file"] or "",
                    link["target"]["route"] or "", link["target"]["file"] or "",
                ]).lower()
                if t not in hay:
                    continue
            relationships.append({
                "source": link["source"],
                "target": link["target"],
                "evidence": {
                    "provenance": "static",
                    "sourceAdapter": "tokensafe-graph-engine",
                    "confidence": "probable",
                },
            })

        matching_symbols = []
        if target:
            t = target.lower()
            for sk, sym in self.symbols.items():
                # Match both names and file paths, like get_static_impact: a target
                # that is a file path ("src/api/wheel.ts") must resolve to that
                # file's symbols, or every path-shaped target silently matches none.
                if t in sym.name.lower() or t in sym.file_path.lower():
                    matching_symbols.append({
                        "name": sym.name,
                        "kind": sym.kind,
                        "file": sym.file_path,
                        "lines": [sym.line_start, sym.line_end],
                    })

        result = {
            "level": "L1",
            "target": target,
            "mode": "STATIC_SURFACE",
            "endpoints": endpoints[:30],
            "clientCalls": client_calls[:30],
            "relationships": relationships[:30],
            "matchingSymbols": matching_symbols[:20],
            "evidence": evidence,
        }
        if lvl != "L1":
            # Asking for L2-L4 offline used to return this L1 payload with
            # confidence "confirmed" and no marker, so callers could not tell a
            # served level from a substituted one.
            result["requestedLevel"] = lvl
            result["degraded"] = True
            result["degradedReason"] = (
                "L2 flow needs a target symbol"
                if lvl == "L2"
                else f"static engine serves L1 surface only; {lvl} requires a live OUMV runtime"
            )
        return result

    def get_static_impact(self, target: str, max_depth: int = 3) -> Dict[str, Any]:
        """
        Static impact analysis over the AST graph: reverse-dependency walk
        ("who depends on the target") plus cross-stack frontend callers of
        matching backend endpoints. Honest about missing runtime evidence.
        """
        evidence = {
            "provenance": "static",
            "sourceAdapter": "tokensafe-graph-engine",
            "confidence": "probable",
        }
        t = (target or "").strip()
        if not t:
            return {"target": t, "mode": "STATIC_IMPACT", "error": "target is required", "evidence": evidence}

        start_nodes: Set[str] = set()
        if t.startswith("file:"):
            start_nodes.add(t)
        for sk, sym in self.symbols.items():
            if t.lower() in sym.name.lower() or t.lower() in sym.file_path.lower():
                start_nodes.add(sk)
        for rel_path in self.file_symbols:
            if t.lower() in rel_path.lower():
                start_nodes.add(f"file:{rel_path}")

        visited: Dict[str, int] = {}
        frontier = [(n, 0) for n in start_nodes]
        while frontier:
            node, depth = frontier.pop(0)
            if node in visited:
                continue
            visited[node] = depth
            if depth >= max_depth:
                continue
            for dep in self.rev_edges.get(node, ()):
                if dep not in visited:
                    frontier.append((dep, depth + 1))

        def _node_info(node: str) -> Dict[str, Any]:
            if node.startswith("file:"):
                return {"type": "file", "file": node[5:]}
            sym = self.symbols.get(node)
            if sym:
                return {"type": "symbol", "name": sym.name, "kind": sym.kind, "file": sym.file_path}
            return {"type": "node", "id": node}

        direct = [_node_info(n) for n, d in visited.items() if d == 1 and n not in start_nodes]
        transitive = [_node_info(n) for n, d in visited.items() if d > 1]

        frontend_callers = []
        for link in self.api_links:
            hay = " ".join([link["target"]["route"] or "", link["target"]["file"] or ""])
            if t.lower() in hay.lower():
                frontend_callers.append({
                    "source": link["source"],
                    "target": link["target"],
                    "evidence": {
                        "provenance": "static",
                        "sourceAdapter": "tokensafe-graph-engine",
                        "confidence": "probable",
                    },
                })

        return {
            "target": t,
            "mode": "STATIC_IMPACT",
            "directDependents": direct[:20],
            "transitiveDependents": transitive[:20],
            "crossStack": {
                "frontendCallers": frontend_callers[:20],
                "count": len(frontend_callers),
            },
            "evidence": evidence,
            "missingEvidence": [
                "Runtime-confirmed dependencies are not included while dev-servers are offline.",
            ],
        }

    def get_static_flow(self, target: str, max_depth: int = 3) -> Dict[str, Any]:
        """Static L2 flow: handler -> callee symbols -> literal client route.

        Answers "what does this handler reach", not "what does it do": the walk is
        the forward call graph (handler, local helpers, imported functions) and the
        API call sites declared in the reached symbols. Dynamic dispatch, call
        order and runtime values stay out — the evidence says so.
        """
        evidence = {
            "provenance": "static",
            "sourceAdapter": "tokensafe-graph-engine",
            "confidence": "probable",
        }
        t = (target or "").strip()
        if not t:
            return {"target": t, "mode": "STATIC_FLOW", "error": "target is required", "evidence": evidence}

        name_to_syms: Dict[str, List[str]] = {}
        for sk, sym in self.symbols.items():
            name_to_syms.setdefault(sym.name, []).append(sk)

        start: Set[str] = set()
        exact: Set[str] = set()
        for sk, sym in self.symbols.items():
            if sym.name.lower() == t.lower():
                exact.add(sk)
            elif t.lower() in sym.name.lower() or t.lower() in sym.file_path.lower():
                start.add(sk)
        # An exact symbol name beats substring hits: `spinWheel` must not pull in
        # SpinWheelRequestProtoDto and everything else that merely contains it.
        start = exact or start
        if not start:
            return {
                "target": t,
                "mode": "STATIC_FLOW",
                "error": "NO_MATCHING_SYMBOL",
                "matchedSymbols": [],
                "hint": "Pass a symbol or file name (e.g. 'spinWheel', 'src/api/wheel.ts'), not a prose question.",
                "evidence": evidence,
            }

        depth_of: Dict[str, int] = {sk: 0 for sk in start}
        frontier = list(start)
        unresolved: Set[str] = set()
        truncated = False

        def _pick(name: str, caller_file: Optional[str]) -> Tuple[List[str], bool]:
            """Callee candidates scoped like a compiler would: same file first, then
            files the caller imports, and only then a globally unique name.

            Name-only resolution used to drop every edge to a helper whose name
            recurs (four `loadStatus` closures in one app), which cut the flow at
            exactly the local-closure hop it was supposed to follow.
            """
            targets = name_to_syms.get(name) or []
            if not targets:
                return [], False
            if len(targets) == 1:
                return targets, False
            if caller_file:
                same = [tk for tk in targets if self.symbols[tk].file_path == caller_file]
                if same:
                    return same, False
                imported: Set[str] = set()
                for spec in self.file_imports.get(caller_file, set()):
                    resolved = self._resolve_relative_module(caller_file, spec)
                    if resolved:
                        imported.add(resolved)
                near = [tk for tk in targets if self.symbols[tk].file_path in imported]
                if near:
                    return near, False
            return [], True

        while frontier:
            node = frontier.pop(0)
            d = depth_of[node]
            caller_sym = self.symbols.get(node)
            caller_file = caller_sym.file_path if caller_sym else None
            for nxt in self.edges.get(node, ()):
                if nxt.startswith("call:"):
                    callee_name = nxt[5:]
                    # Language builtins are not code this graph could resolve.
                    if callee_name in _STATIC_FLOW_NOISE or callee_name[:1].isupper():
                        continue
                    targets, ambiguous = _pick(callee_name, caller_file)
                    if ambiguous:
                        unresolved.add(callee_name + " (ambiguous)")
                        continue
                    if not targets:
                        unresolved.add(callee_name)
                        continue
                elif nxt.startswith("sym:"):
                    targets, _ambiguous = _pick(nxt[4:], caller_file)
                    if not targets:
                        continue
                elif nxt.startswith(("file:", "mod:")):
                    continue
                elif nxt in self.symbols:
                    targets = [nxt]
                else:
                    continue
                for tk in targets:
                    if tk in depth_of or tk in start:
                        continue
                    if d >= max_depth:
                        truncated = True
                        continue
                    depth_of[tk] = d + 1
                    frontier.append(tk)

        def _sym_info(sk: str) -> Dict[str, Any]:
            sym = self.symbols[sk]
            return {
                "name": sym.name,
                "kind": sym.kind,
                "file": sym.file_path,
                "lines": [sym.line_start, sym.line_end],
                "depth": depth_of[sk],
            }

        callees = sorted(
            (_sym_info(sk) for sk in depth_of if depth_of[sk] > 0),
            key=lambda item: (item["depth"], item["file"], item["name"]),
        )

        # Client route literals reached by the walk: the handler itself or any
        # callee that declares the call. Declared routes of the start symbol's own
        # file are included only when that symbol declares them.
        routes: List[Dict[str, Any]] = []
        seen_routes = set()
        for rel_path, calls in self.file_api_calls.items():
            for norm_route, caller_sym, method in calls:
                if not norm_route:
                    continue
                if caller_sym is not None and caller_sym not in depth_of:
                    continue
                if caller_sym is None and rel_path != self.symbols[next(iter(start))].file_path:
                    continue
                key = (norm_route, method, rel_path, caller_sym)
                if key in seen_routes:
                    continue
                seen_routes.add(key)
                routes.append({
                    "route": norm_route,
                    "method": method,
                    "file": rel_path,
                    "via": self.symbols[caller_sym].name if caller_sym else None,
                })

        return {
            "target": t,
            "level": "L2",
            "mode": "STATIC_FLOW",
            "matchedSymbols": [
                {"name": self.symbols[sk].name, "file": self.symbols[sk].file_path}
                for sk in sorted(start)
            ][:10],
            "callees": callees[:40],
            "clientCalls": routes[:20],
            "unresolvedCalls": sorted(unresolved)[:20],
            "truncated": truncated,
            "evidence": evidence,
            "missingEvidence": [
                "Runtime-confirmed call order and dynamic dispatch are not included while dev-servers are offline.",
            ],
        }


class GraphEngine:
    """Multi-Project Manager for automatic background graph execution."""

    _instances: Dict[str, CodeGraph] = {}
    _instances_lock = threading.RLock()

    @classmethod
    def find_project_root(cls, start_path: str) -> str:
        """Walks up from start_path to locate project boundary markers."""
        curr = os.path.abspath(start_path)
        if os.path.isfile(curr):
            curr = os.path.dirname(curr)

        root = os.path.abspath(os.sep)
        while curr and curr != root:
            for marker in (".git", "package.json", "pyproject.toml", "go.mod", "Cargo.toml", "requirements.txt"):
                if os.path.exists(os.path.join(curr, marker)):
                    return curr
            parent = os.path.dirname(curr)
            if parent == curr:
                break
            curr = parent
        return os.path.abspath(start_path if os.path.isdir(start_path) else os.path.dirname(start_path))

    @classmethod
    def get_instance(cls, root_dir: str = ".") -> CodeGraph:
        root_abs = os.path.abspath(root_dir)
        # Rescans are full AST crawls; a 3s TTL caused near-constant rescans on every
        # smart_read_file(symbol=...) call. 60s keeps graphs fresh without the churn.
        rescan_ttl = float(os.environ.get("TOKENSAFE_GRAPH_TTL", "60.0"))
        with cls._instances_lock:
            if root_abs not in cls._instances:
                graph = CodeGraph(root_abs, use_disk_cache=True)
                graph.scan()
                cls._instances[root_abs] = graph
            else:
                graph = cls._instances[root_abs]
                if time.time() - graph.last_scanned > rescan_ttl:
                    graph.scan()
            return graph

    @classmethod
    def for_file(cls, file_path: str) -> Tuple[CodeGraph, str]:
        """Resolves the owning project graph and relative file path for any arbitrary file."""
        root = cls.find_project_root(file_path)
        graph = cls.get_instance(root)
        rel_path = os.path.relpath(os.path.abspath(file_path), root).replace("\\", "/")
        return graph, rel_path

def main():
    import sys
    args = sys.argv[1:]
    cmd = args[0] if args else "graph"

    target_dir = "."
    graph = GraphEngine.get_instance(target_dir)

    if cmd in ("graph", "nodes", "god-nodes"):
        print(f"\n🔍 TokenSafe Graph Engine — Knowledge Graph & Centrality Analysis")
        print(f"   Indexed: {len(graph.file_symbols)} code files, {len(graph.symbols)} symbols\n")
        god_nodes = graph.get_god_nodes(top_n=7)
        print("⚡ Architectural Hubs (God Nodes):")
        for idx, g in enumerate(god_nodes, 1):
            print(f"  {idx}. {g['file']}")
            print(f"     Degree Centrality: {g['centrality']} | Dependents: {g['dependents']} | Dependencies: {g['dependencies']} | Symbols: {g['symbols_count']}")
        print(f"\n💡 Tip: Run 'tokensafe map' to generate tokensafe-out/API_SURFACE.md\n")

    elif cmd in ("map", "surface"):
        out_file = os.path.join(target_dir, "tokensafe-out", "API_SURFACE.md")
        graph.generate_api_surface(out_file)
        print(f"\n✓ Generated API Surface Map: {out_file} ({os.path.getsize(out_file):,} bytes)")
        print(f"  AI agents can read this file in ~1.5k tokens instead of reading the entire repo.\n")

    elif cmd == "skeleton":
        if len(args) < 2:
            print("Usage: tokensafe skeleton <file_path> [target_symbol]")
            sys.exit(1)
        file_path = args[1]
        target_symbol = args[2] if len(args) > 2 else None
        res = graph.graph_guided_skeletonize(file_path, target_symbol)
        print(res)

    else:
        print(f"Unknown command: {cmd}. Available: graph, map, skeleton")

if __name__ == "__main__":
    main()

