"""
Universal Model Context Protocol (MCP) Server for Token Optimizer.
Provides MCP stdio JSON-RPC 2.0 interface compatible with Claude Desktop, Cursor, Windsurf, Cline, and Zed.
Exposes tools for smart file reading, dependency prefetching, prompt compression, and command sanitizing.
"""

import sys
import os
import stat
import time
import json
import re
import threading
import traceback
import fnmatch
from typing import List, Tuple, Optional, Any, Dict

# Ensure project root in sys.path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from src.compactor import PromptCompactor
from src.skeletonizer import Skeletonizer
from src.slicer import ToolSlicer
from src.schema_slicer import SchemaSlicer
from src.dependency_prefetcher import DependencyPrefetcher
from src.command_sanitizer import CommandSanitizer
from src.loop_detector import LoopDetector
from src.metrics import MetricsTracker
from src.dashboard import Dashboard
from src.graph_engine import GraphEngine
from src.tokenizer import count_tokens
from src.history_compactor import HistoryCompactor
from src.team_cache import TeamCache
from src.governor import ContextBudget
from src.agent_firewall import AgentFirewall
from src.diff_slicer import DiffSlicer
from src.guard import TokenSafeGuard
from src.scoped_search import ScopedSearch
from src.license_manager import LicenseManager
from src.paths import get_active_workspace, is_plugin_or_system_dir

SERVER_INFO = {
    "name": "tokensafe-mcp",
    "version": "4.5.0"
}

# MCP client attribution: the initialize handshake carries clientInfo, which
# lets every session-scoped metric name the REAL agent instead of a shared
# anonymous bucket (see METRICS.md by_agent).
_MCP_CLIENT_NAME = ""
_TOOLS_LISTED = False
_EPOCH_GEN = 0
_IN_PROCESS_DELIVERED = {}
_KNOWN_POINTER_CLIENTS = (
    "antigravity", "claude", "cursor", "windsurf", "cline", "roo",
    "codex", "zcode", "kimi", "copilot", "gemini", "zed", "vscode",
)


def _set_mcp_client(name: str):
    global _MCP_CLIENT_NAME
    clean = re.sub(r"[^a-z0-9_-]", "", str(name or "").lower())[:30]
    _MCP_CLIENT_NAME = clean


def _mcp_session_id() -> str:
    return f"mcp:{_MCP_CLIENT_NAME}" if _MCP_CLIENT_NAME else "mcp_session"


def _client_supports_pointers() -> bool:
    """Out-of-the-box capability detection (zero env vars): returns True once the
    MCP client has fetched tools/list (seeing force_full) or identified as an
    interactive AI coding host."""
    if _TOOLS_LISTED:
        return True
    return bool(_MCP_CLIENT_NAME and any(k in _MCP_CLIENT_NAME for k in _KNOWN_POINTER_CLIENTS))


TOOLS = [   {   'annotations': {'readOnlyHint': True},
        'description': 'AST-skeleton file reader: keeps the target function/class body + 1-line 1-hop dependency '
                       'signatures, folds inert code (-90-99% tokens). Relative or absolute paths. Schema files '
                       "(OpenAPI/Prisma/GraphQL/SQL) auto-slice via 'symbol'. For 3+ related symbols use smart_bundle "
                       'instead.',
        'inputSchema': {   'properties': {   'dep_mode': {   'description': "'signature' (default) = 1-line dep "
                                                                            "signatures; 'full' = inline dependency "
                                                                            'bodies',
                                                             'enum': ['signature', 'full'],
                                                             'type': 'string'},
                                             'end_line': {   'description': 'Optional: ending line number',
                                                             'type': 'integer'},
                                             'force_full': {   'description': 'Bypass pointer dedup — use only after '
                                                                              'context compaction or as a fresh '
                                                                              'subagent',
                                                               'type': 'boolean'},
                                             'include_deps': {   'description': 'Include 1-hop dependency signatures '
                                                                                '(default true)',
                                                                 'type': 'boolean'},
                                             'path': {   'description': 'File path (relative to workspace or absolute)',
                                                         'type': 'string'},
                                             'start_line': {   'description': 'Optional: starting line number',
                                                               'type': 'integer'},
                                             'symbol': {   'description': 'Optional: function/method/class/component '
                                                                          'or schema model/type/table to extract (omit '
                                                                          'for whole-file skeleton)',
                                                           'type': 'string'}},
                           'required': ['path'],
                           'type': 'object'},
        'name': 'smart_read_file'},
    {   'annotations': {'readOnlyHint': True},
        'description': 'Reads 2-8 related symbols across files as ONE merged bundle: per-file skeletons, deduplicated '
                       'shared imports, merged 1-hop dep signatures. Replaces 3+ separate smart_read_file calls in a '
                       'single roundtrip.',
        'inputSchema': {   'properties': {   'dep_mode': {   'description': 'Parity only; bundles always merge '
                                                                            'signatures',
                                                             'enum': ['signature', 'full'],
                                                             'type': 'string'},
                                             'force_full': {   'description': 'Bypass session pointer dedup',
                                                               'type': 'boolean'},
                                             'include_deps': {   'description': 'Include merged 1-hop dep signatures '
                                                                                '(default true)',
                                                                 'type': 'boolean'},
                                             'targets': {   'description': 'Up to 8 {path, symbol} targets; several '
                                                                           'symbols of one file = separate entries',
                                                            'items': {   'properties': {   'path': {   'description': 'File '
                                                                                                                      'path '
                                                                                                                      '(relative '
                                                                                                                      'or '
                                                                                                                      'absolute)',
                                                                                                       'type': 'string'},
                                                                                           'symbol': {   'description': 'Optional '
                                                                                                                        'symbol '
                                                                                                                        'to '
                                                                                                                        'keep '
                                                                                                                        'unfolded',
                                                                                                         'type': 'string'}},
                                                                         'required': ['path'],
                                                                         'type': 'object'},
                                                            'maxItems': 8,
                                                            'type': 'array'}},
                           'required': ['targets'],
                           'type': 'object'},
        'name': 'smart_bundle'},
    {   'annotations': {'readOnlyHint': True},
        'description': 'Returns top architectural God Nodes and dependency topology for the target project in < 250 '
                       'tokens without file crawling. Call this BEFORE reading files or searching.',
        'inputSchema': {   'properties': {   'project_path': {   'description': 'Target project root directory. '
                                                                                'IMPORTANT: Always pass the absolute '
                                                                                "directory path (e.g. '/path/to/repo') "
                                                                                'when working in multi-workspace '
                                                                                'setups or repositories outside CWD.',
                                                                 'type': 'string'}},
                           'type': 'object'},
        'name': 'get_architecture_graph'},
    {   'annotations': {'readOnlyHint': True},
        'description': 'Generates and returns a compact markdown map of all exported interfaces, classes, and types in '
                       'the project without crawling files.',
        'inputSchema': {   'properties': {   'project_path': {   'description': 'Target project root directory. '
                                                                                'IMPORTANT: Always pass the absolute '
                                                                                "directory path (e.g. '/path/to/repo') "
                                                                                'when working in multi-workspace '
                                                                                'setups or repositories outside CWD.',
                                                                 'type': 'string'}},
                           'type': 'object'},
        'name': 'get_api_surface'},
    {   'annotations': {'readOnlyHint': True},
        'description': 'Compresses a verbose developer prompt into an ultra-dense TOCP S-Expression via DeepSeek-V3 / '
                       'Regex.',
        'inputSchema': {   'properties': {   'prompt': {   'description': 'Natural language request from developer',
                                                           'type': 'string'}},
                           'required': ['prompt'],
                           'type': 'object'},
        'name': 'compress_prompt'},
    {   'annotations': {'readOnlyHint': True},
        'description': 'Context Budget HUD & Context Quality Score (Grade S/A/B/C/F): estimates context window usage, '
                       'headroom %, static tool/skill tax, and outputs a unified health grade. Call before large '
                       'multi-step tasks. When warning/critical or Grade C/F, call tokensafe:compact_conversation.',
        'inputSchema': {   'properties': {   'model': {   'description': 'Optional: model name to resolve window size '
                                                                         '(default: env TOKENSAFE_CONTEXT_WINDOW or '
                                                                         '200k)',
                                                          'type': 'string'},
                                             'session_id': {   'description': 'Optional: session identifier (default: '
                                                                              'mcp_session)',
                                                               'type': 'string'},
                                             'transcript_path': {   'description': 'Optional: path to the session '
                                                                                   'JSONL transcript; when provided, '
                                                                                   'REAL history mass is measured '
                                                                                   'instead of the instrumented floor '
                                                                                   '(recommended)',
                                                                    'type': 'string'},
                                             'window_tokens': {   'description': 'Optional: explicit context window '
                                                                                 'size in tokens',
                                                                  'type': 'integer'}},
                           'type': 'object'},
        'name': 'context_budget'},
    {   'annotations': {'destructiveHint': False, 'readOnlyHint': False},
        'description': 'On-demand context compaction: folds a long agent conversation transcript into a Consolidated '
                       'State Ledger with a 3-line Post-Compaction Orientation Digest (Active Goal, Last State, Next '
                       'Focus) so the agent immediately resumes without losing flow. Use when context_budget reports '
                       'warning/critical or Grade C/F.',
        'inputSchema': {   'properties': {   'invocation_num': {   'description': 'Optional: current turn number '
                                                                                  '(default 3; folding requires >= 3 '
                                                                                  'turns)',
                                                                   'type': 'integer'},
                                             'transcript_path': {   'description': 'Path to the JSONL transcript of '
                                                                                   'the current session',
                                                                    'type': 'string'}},
                           'required': ['transcript_path'],
                           'type': 'object'},
        'name': 'compact_conversation'},
    {   'annotations': {'readOnlyHint': True},
        'description': 'Per-stage shell command rewrites: prunes lockfiles, caps unbounded git log/show/blame/rg and '
                       'docker/kubectl logs, strips git push progress animations (--no-progress), wraps git commit '
                       'with tokensafe wrap when pre-commit/husky hooks exist, and quiets npm/pip installs, bounds '
                       'cat/curl output. Compound commands (a && b ; c) are optimized per stage without breaking '
                       'operators. Test commands (bun test, pytest, python -m unittest, vitest, jest) are '
                       'automatically wrapped or forced into run-once mode (CI=true, --watch=false, --forceExit) so '
                       'passing suites emit a clean 1-line summary and agents never hang on watch mode. Pass optional '
                       'cwd for test commands to unlock package.json runner detection for opaque yarn/npm test '
                       'scripts.',
        'inputSchema': {   'properties': {   'cmd': {'description': 'Alias for command', 'type': 'string'},
                                             'command': {'description': 'Raw bash command line', 'type': 'string'},
                                             'cwd': {   'description': 'Optional working directory the command will '
                                                                       'run in; enables the package.json peek that '
                                                                       'identifies the runner behind opaque yarn/npm '
                                                                       'test scripts',
                                                        'type': 'string'}},
                           'type': 'object'},
        'name': 'sanitize_command'},
    {   'annotations': {'readOnlyHint': True},
        'description': 'Slices unified diffs, omitting lockfiles and minimizing context lines to reduce tokens by up '
                       'to 80%.',
        'inputSchema': {   'properties': {   'diff': {'description': 'Raw git diff text', 'type': 'string'},
                                             'max_context': {   'description': 'Maximum surrounding context lines '
                                                                               '(default: 2)',
                                                                'type': 'integer'}},
                           'required': ['diff'],
                           'type': 'object'},
        'name': 'slice_diff'},
    {   'annotations': {'readOnlyHint': True},
        'description': 'Returns cumulative token and cost savings analytics.',
        'inputSchema': {'properties': {}, 'type': 'object'},
        'name': 'get_token_savings_metrics'},
    {   'annotations': {'readOnlyHint': True},
        'description': 'Pre-flight security & hygiene audit for commands or code snippets. Detects secret leaks, SQL '
                       'injection, TLS bypass (incl. git-level), CORS wildcard, destructive commands, raw grep, '
                       'in-place stream editing (sed -i, perl -i), and never-terminating streams (tail -f, watch, '
                       'docker attach).',
        'inputSchema': {   'properties': {   'cmd': {'description': 'Alias for command', 'type': 'string'},
                                             'code': {   'description': 'Optional: Code snippet, replacement diff, or '
                                                                        'file content to audit',
                                                         'type': 'string'},
                                             'command': {   'description': 'Optional: Terminal command line to audit',
                                                            'type': 'string'},
                                             'file_path': {   'description': 'Optional: File path being written or '
                                                                             'modified',
                                                              'type': 'string'}},
                           'type': 'object'},
        'name': 'guard_inspect'},
    {   'annotations': {'readOnlyHint': True},
        'description': 'Returns a clean, token-efficient directory tree up to depth N without permissions, timestamps, '
                       "or ignored folders (node_modules, .git, dist, .env). Replaces verbose ls -la. Pass '.' for "
                       "project root or a subfolder like 'src'.",
        'inputSchema': {   'properties': {   'depth': {   'description': 'Maximum traversal depth (default: 2, max: 4)',
                                                          'type': 'integer'},
                                             'max_entries': {   'description': 'Maximum entries to display before '
                                                                               'pruning (default: 80)',
                                                                'type': 'integer'},
                                             'path': {   'description': 'Path to the directory (relative to workspace '
                                                                        "or absolute, e.g. '.', './src')",
                                                         'type': 'string'}},
                           'required': ['path'],
                           'type': 'object'},
        'name': 'list_dir'},
    {   'annotations': {'readOnlyHint': True},
        'description': "Safe, token-bounded symbol/text search replacing raw grep/find (-90% tokens). '|' or \\d in "
                       'query auto-enables regex when use_regex is omitted; false forces literal.',
        'inputSchema': {   'properties': {   'case_sensitive': {   'description': 'Case-sensitive search (default '
                                                                                  'false)',
                                                                   'type': 'boolean'},
                                             'max_results': {   'description': 'Max matching lines (default 25, max '
                                                                               '100)',
                                                                'type': 'integer'},
                                             'path': {   'description': 'Directory to search (alias for project_path; '
                                                                        'relative or absolute)',
                                                         'type': 'string'},
                                             'project_path': {   'description': 'Optional root directory (defaults to '
                                                                                'current project)',
                                                                 'type': 'string'},
                                             'query': {   'description': 'Text, identifier, or symbol name',
                                                          'type': 'string'},
                                             'scope': {   'description': "Filter: 'code', 'config', 'all', extension "
                                                                         "('go', '*.py'), or subfolder "
                                                                         "('internal/promo') (default 'all')",
                                                          'type': 'string'},
                                             'use_regex': {   'description': "Omit = auto-detect ('|', \\d → regex); "
                                                                             'false = literal; true = regex',
                                                              'type': 'boolean'}},
                           'required': ['query'],
                           'type': 'object'},
        'name': 'scoped_search'},
    {   'annotations': {'readOnlyHint': True},
        'description': 'Expands a previously folded or stashed tool, command, file, or MCP result by its 8-character '
                       "hex hash anchor (e.g. 'a1b2c3d4' or '#a1b2c3d4'). Returns the full original text that "
                       'TokenSafe folded.',
        'inputSchema': {   'properties': {   'hash': {   'description': '8-character hex hash from the fold marker '
                                                                        "(e.g. 'a1b2c3d4' or '#a1b2c3d4')",
                                                         'type': 'string'}},
                           'required': ['hash'],
                           'type': 'object'},
        'name': 'expand_ref'},
    {   'annotations': {'readOnlyHint': True},
        'description': 'Fetches a web page URL and converts it to clean, readable Markdown with L1 memory, L2 SQLite '
                       '(web_cache.db), and L3 Team Sync caching (-80-95% tokens compared to raw HTML, 0ms on cache '
                       'hits).',
        'inputSchema': {   'properties': {   'force_refresh': {   'description': 'If true, bypasses L1/L2/L3 cache and '
                                                                                 'fetches fresh page content from '
                                                                                 'network',
                                                                  'type': 'boolean'},
                                             'url': {   'description': 'Web URL to fetch and convert to Markdown',
                                                        'type': 'string'}},
                           'required': ['url'],
                           'type': 'object'},
        'name': 'read_web_page'},
    {   'annotations': {'readOnlyHint': True},
        'description': 'Fast targeted lookup of a package in project lockfiles (package-lock.json, bun.lock, '
                       'Cargo.lock, pnpm-lock.yaml) returning exact version and direct dependencies in <20 tokens '
                       'without dumping massive lockfiles.',
        'inputSchema': {   'properties': {   'lockfile_path': {   'description': 'Optional explicit path to lockfile; '
                                                                                 'defaults to nearest lockfile in '
                                                                                 'project',
                                                                  'type': 'string'},
                                             'package_name': {   'description': 'Name of the dependency package to '
                                                                                "inspect (e.g. 'zod', 'react', "
                                                                                "'tokio')",
                                                                 'type': 'string'}},
                           'required': ['package_name'],
                           'type': 'object'},
        'name': 'query_dependency'},
    {   'annotations': {'readOnlyHint': True},
        'description': 'Extracts targeted type signatures, interfaces, or class declarations from 3rd-party library '
                       'declaration files (.d.ts) in node_modules without directory crawling or reading multi-megabyte '
                       'bundles.',
        'inputSchema': {   'properties': {   'package_name': {   'description': 'Name of the npm package (e.g. '
                                                                                "'@stripe/stripe-js', 'fastify', "
                                                                                "'zod')",
                                                                 'type': 'string'},
                                             'project_root': {   'description': 'Optional project root directory',
                                                                 'type': 'string'},
                                             'symbol': {   'description': 'Name of the interface, type, or function to '
                                                                          "extract (e.g. 'Stripe', 'FastifyReply')",
                                                           'type': 'string'}},
                           'required': ['package_name', 'symbol'],
                           'type': 'object'},
        'name': 'slice_external_type'},
    {   'annotations': {'destructiveHint': False, 'readOnlyHint': False},
        'description': 'Downscales oversized screenshots/diagrams before passing them to LLM vision endpoints. Resizes '
                       'images exceeding max_edge (default: 1024px) while preserving aspect ratio and crispness, '
                       'keeping the original image intact on disk (-50-75% vision tokens).',
        'inputSchema': {   'properties': {   'image_path': {   'description': 'Absolute or relative path to the image '
                                                                              'file',
                                                               'type': 'string'},
                                             'max_edge': {   'description': 'Maximum width/height in pixels (default: '
                                                                            '1024)',
                                                             'type': 'integer'}},
                           'required': ['image_path'],
                           'type': 'object'},
        'name': 'optimize_image'},
    {   'annotations': {'destructiveHint': False, 'readOnlyHint': False},
        'description': 'Prunes obsolete intermediate tool read outputs in long conversation transcripts that have '
                       'since been modified by subsequent edits or re-read in later steps (-30-40% context mass).',
        'inputSchema': {   'properties': {   'transcript_path': {   'description': 'Path to the JSONL conversation '
                                                                                   'transcript to prune',
                                                                    'type': 'string'}},
                           'required': ['transcript_path'],
                           'type': 'object'},
        'name': 'prune_superseded'},
    {   'annotations': {'readOnlyHint': True},
        'description': 'Returns known dead-ends, failed hypotheses, and reverted code changes for this project to '
                       'prevent repeating unsuccessful approaches.',
        'inputSchema': {   'properties': {   'clear': {   'description': 'If true, clears the mistakes ledger',
                                                          'type': 'boolean'},
                                             'project_root': {   'description': 'Optional project root directory '
                                                                                '(defaults to current project)',
                                                                 'type': 'string'}},
                           'type': 'object'},
        'name': 'get_mistakes'},
    {   'annotations': {'readOnlyHint': True},
        'description': 'Reports live health and capability status of connected Frontend (tokensafe-ui/React) and '
                       'Backend (FastAPI/Go/Node) OUMV runtimes.',
        'inputSchema': {   'properties': {   'workspace_dir': {   'description': 'Optional workspace root directory',
                                                                  'type': 'string'}},
                           'type': 'object'},
        'name': 'oumv_status'},
    {   'annotations': {'readOnlyHint': True},
        'description': 'Progressive semantic context observation across tokensafe-ui, React 18/19 '
                       '(Zustand/Redux/Context/useReducer), Vue 3, and Backend (L0 Pulse ~30t, L1 Surface ~150t, L2 '
                       'Contract ~80t, L3 Source, L4 Traces) without wasting tokens on raw dumps.',
        'inputSchema': {   'properties': {   'level': {   'default': 'L0',
                                                          'description': 'Observation level (L0: Pulse, L1: Surface, '
                                                                         'L2: Contract, L3: Source, L4: Traces)',
                                                          'enum': ['L0', 'L1', 'L2', 'L3', 'L4'],
                                                          'type': 'string'},
                                             'runtime': {   'default': 'auto',
                                                            'description': 'Target runtime (frontend, backend, or '
                                                                           'auto-detect)',
                                                            'enum': ['frontend', 'backend', 'auto'],
                                                            'type': 'string'},
                                             'target': {   'description': "Target component ID, route (e.g. 'POST "
                                                                          "/api/products'), or DOM element "
                                                                          "('#submit-btn')",
                                                           'type': 'string'}},
                           'type': 'object'},
        'name': 'oumv_observe'},
    {   'annotations': {'readOnlyHint': True},
        'description': "Answers 'Why is state X / why is button disabled?' (causality/why_state) or performs impact "
                       'analysis: causality merges both runtimes and adds static cross-stack edges (frontend action '
                       '-> backend endpoint) with evidence; impact walks reverse dependencies plus cross-stack '
                       'frontend callers of matching backend endpoints.',
        'inputSchema': {   'properties': {   'query': {   'default': 'causality',
                                                          'description': 'Query type (causality, impact, why_state)',
                                                          'enum': ['causality', 'impact', 'why_state'],
                                                          'type': 'string'},
                                             'runtime': {   'default': 'auto',
                                                            'description': 'Target runtime',
                                                            'enum': ['frontend', 'backend', 'auto'],
                                                            'type': 'string'},
                                             'target': {   'description': 'Target component ID, state key, endpoint, '
                                                                          'or UI element',
                                                           'type': 'string'}},
                           'type': 'object'},
        'name': 'oumv_understand'},
    {   'annotations': {'destructiveHint': False, 'readOnlyHint': False},
        'description': 'Applies Optimistic Concurrency Control (OCC) source edit with Babel AST JSX/TSX syntax guards, '
                       'TypeScript verification, and secret leak prevention. Offline fallback performs an OCC-verified '
                       'direct workspace edit with syntax pre-flight (.py via ast, .js via node --check) and an '
                       'automatic backup of the previous content.',
        'inputSchema': {   'properties': {   'expectedVersion': {   'description': 'Expected version token for '
                                                                                   'optimistic concurrency',
                                                                    'type': 'string'},
                                             'path': {'description': 'File path to modify', 'type': 'string'},
                                             'replacement': {   'description': 'Replacement source code',
                                                                'type': 'string'},
                                             'runtime': {   'default': 'auto',
                                                            'description': "Target runtime environment ('frontend', "
                                                                           "'backend', or 'auto')",
                                                            'enum': ['frontend', 'backend', 'auto'],
                                                            'type': 'string'},
                                             'symbol': {'description': 'Target symbol to edit', 'type': 'string'}},
                           'required': ['path'],
                           'type': 'object'},
        'name': 'oumv_mutate'},
    {   'annotations': {'readOnlyHint': True},
        'description': 'Instantly verifies actions in memory (UI synthetic actions, React reducers/Zustand/Redux '
                       'actions, Context updaters, or Backend transactional sandbox) with structured state and surface '
                       'diffs.',
        'inputSchema': {   'properties': {   'action': {   'default': 'invoke',
                                                           'description': 'Action name (invoke, click, submit, or '
                                                                          'store action)',
                                                           'type': 'string'},
                                             'payload': {   'description': 'Request body or action payload',
                                                            'type': 'object'},
                                             'runtime': {   'default': 'auto',
                                                            'description': "Target runtime environment ('frontend', "
                                                                           "'backend', or 'auto')",
                                                            'enum': ['frontend', 'backend', 'auto'],
                                                            'type': 'string'},
                                             'target': {   'description': "Action target (component ID, e.g. 'cart', "
                                                                          "or 'POST /api/v1/orders' or '#submit-btn')",
                                                           'type': 'string'}},
                           'required': ['target'],
                           'type': 'object'},
        'name': 'oumv_verify'},
    {   'annotations': {'readOnlyHint': False, 'destructiveHint': False},
        'description': 'Atomic or best-effort multi-hunk and multi-file code editor (up to 50 files) with optional '
                       'inline verification command (e.g. npm test, pytest). Replaces serial single-hunk edit turns. '
                       'Validates hunks in-memory before disk write and preserves CRLF/permissions.',
        'inputSchema': {   'properties': {   'best_effort': {   'description': 'If true, commits valid files while '
                                                                               'returning drift hints for failed files',
                                                                'type': 'boolean'},
                                             'cwd': {'description': 'Optional working directory', 'type': 'string'},
                                             'dry_run': {   'description': 'If true, validates all hunks without touching '
                                                                           'disk',
                                                            'type': 'boolean'},
                                             'edits': {   'description': 'Array of up to 50 file edit objects',
                                                          'items': {   'properties': {   'create': {   'description': 'Optional '
                                                                                                                     'content '
                                                                                                                     'for newly '
                                                                                                                     'created '
                                                                                                                     'file',
                                                                                                       'type': 'string'},
                                                                                         'hunks': {   'description': 'Array of '
                                                                                                                     'replacement '
                                                                                                                     'hunks',
                                                                                                      'items': {   'properties': {   'end_line': {   'description': 'Optional '
                                                                                                                                                                     '1-based '
                                                                                                                                                                     'end '
                                                                                                                                                                     'line '
                                                                                                                                                                     'bound',
                                                                                                                                                             'type': 'integer'},
                                                                                                                                     'replace_all': {   'description': 'Replace '
                                                                                                                                                                       'all '
                                                                                                                                                                       'occurrences',
                                                                                                                                                        'type': 'boolean'},
                                                                                                                                     'replacement': {   'description': 'Replacement '
                                                                                                                                                                       'string',
                                                                                                                                                        'type': 'string'},
                                                                                                                                     'start_line': {   'description': 'Optional '
                                                                                                                                                                       '1-based '
                                                                                                                                                                       'start '
                                                                                                                                                                       'line '
                                                                                                                                                                       'bound',
                                                                                                                                                                       'type': 'integer'},
                                                                                                                                     'target': {   'description': 'Exact '
                                                                                                                                                                  'string '
                                                                                                                                                                  'to '
                                                                                                                                                                  'be '
                                                                                                                                                                  'replaced',
                                                                                                                                                   'type': 'string'}},
                                                                                                                   'required': [   'target',
                                                                                                                                   'replacement'],
                                                                                                                   'type': 'object'},
                                                                                                      'type': 'array'},
                                                                                         'overwrite': {   'description': 'Allow '
                                                                                                                         'overwriting '
                                                                                                                         'existing '
                                                                                                                         'file '
                                                                                                                         'with '
                                                                                                                         'create',
                                                                                                          'type': 'boolean'},
                                                                                         'path': {   'description': 'File '
                                                                                                                    'path '
                                                                                                                    '(relative '
                                                                                                                    'or '
                                                                                                                    'absolute)',
                                                                                                     'type': 'string'}},
                                                                       'required': ['path'],
                                                                       'type': 'object'},
                                                          'maxItems': 50,
                                                          'type': 'array'},
                                             'verify': {   'description': 'Optional shell command to execute immediately '
                                                                          'after applying edits (e.g. npm test, pytest, '
                                                                          'cargo test)',
                                                           'type': 'string'},
                                             'verify_timeout_sec': {   'description': 'Verification timeout in seconds '
                                                                                      '(default 30)',
                                                                       'type': 'integer'}},
                           'required': ['edits'],
                           'type': 'object'},
        'name': 'apply_edits'},
    {   'annotations': {'readOnlyHint': True},
        'description': 'Concurrent multi-query search engine: executes up to 20 text, regex, glob, fuzzy, or '
                       'read-only git queries in ONE call (<70ms local) without paying per-command remote check latency.',
        'inputSchema': {   'properties': {   'cwd': {'description': 'Optional working directory', 'type': 'string'},
                                             'queries': {   'description': 'Array of up to 20 search query objects',
                                                            'items': {   'properties': {   'case_sensitive': {   'description': 'Case-sensitive '
                                                                                                                                'search '
                                                                                                                                '(default '
                                                                                                                                'false)',
                                                                                                                 'type': 'boolean'},
                                                                                           'file_type': {   'description': 'Optional '
                                                                                                                           'file '
                                                                                                                           'type '
                                                                                                                           'filter '
                                                                                                                           "(e.g. "
                                                                                                                           "'typescript', "
                                                                                                                           "'python')",
                                                                                                            'type': 'string'},
                                                                                           'glob': {   'description': 'Optional '
                                                                                                                      'glob '
                                                                                                                      'filter '
                                                                                                                      "(e.g. "
                                                                                                                      "'*.ts')",
                                                                                                       'type': 'string'},
                                                                                           'max_results': {   'description': 'Max '
                                                                                                                             'matches '
                                                                                                                             'per '
                                                                                                                             'query '
                                                                                                                             '(default '
                                                                                                                             '25)',
                                                                                                              'type': 'integer'},
                                                                                           'output_mode': {   'description': 'Output '
                                                                                                                             'mode: '
                                                                                                                             'content, '
                                                                                                                             'files_with_matches, '
                                                                                                                             'or '
                                                                                                                             'count',
                                                                                                              'enum': [   'content',
                                                                                                                          'files_with_matches',
                                                                                                                          'count'],
                                                                                                              'type': 'string'},
                                                                                           'path': {   'description': 'Optional '
                                                                                                                      'subpath '
                                                                                                                      'to '
                                                                                                                      'scope '
                                                                                                                      'query',
                                                                                                       'type': 'string'},
                                                                                           'pattern': {   'description': 'Search '
                                                                                                                         'pattern, '
                                                                                                                         'text, '
                                                                                                                         'regex, '
                                                                                                                         'glob, '
                                                                                                                         'or '
                                                                                                                         'read-only '
                                                                                                                         'git '
                                                                                                                         'command',
                                                                                                          'type': 'string'},
                                                                                           'type': {   'description': 'Query '
                                                                                                                      'type: '
                                                                                                                      'text, '
                                                                                                                      'regex, '
                                                                                                                      'glob, '
                                                                                                                      'fuzzy, '
                                                                                                                      'or '
                                                                                                                      'git',
                                                                                                       'enum': [   'text',
                                                                                                                   'regex',
                                                                                                                   'glob',
                                                                                                                   'fuzzy',
                                                                                                                   'git'],
                                                                                                       'type': 'string'}},
                                                                         'required': ['pattern'],
                                                                         'type': 'object'},
                                                            'maxItems': 20,
                                                            'type': 'array'}},
                           'required': ['queries'],
                           'type': 'object'},
        'name': 'batch_search'}]

def format_file_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f} MB"

def build_dir_tree(dir_path: str, max_depth: int = 2, max_entries: int = 80) -> str:
    """
    Builds a clean, compact markdown tree for dir_path.
    Ignores heavy folders (node_modules, dist, .git, etc.) and redacts sensitive files (.env*).
    """
    IGNORE_DIRS = {
        'node_modules', '.git', 'dist', 'build', '.next', 'coverage',
        '__pycache__', '.turbo', '.vite', '.output', 'data',
        '.venv', 'venv', '.cache', '.idea', '.vscode',
        'Library', 'Applications', 'Movies', 'Music', 'Pictures'
    }
    IGNORE_FILES = {
        '*.lock', '*.lockb', '*.map', '.DS_Store', 'Thumbs.db', 'package-lock.json',
        'pnpm-lock.yaml', 'yarn.lock', 'Cargo.lock', 'composer.lock'
    }
    SECRET_FILES = {
        '.env', '.env.local', '.env.production', '.env.staging', '.env.development'
    }

    if not os.path.exists(dir_path):
        return f"Error: Directory not found: {dir_path}"
    if not os.path.isdir(dir_path):
        return f"Error: Path is not a directory: {dir_path}"

    dirs_ignore_dots = {
        ".git", ".hg", ".svn", ".venv", ".cache", ".idea", ".vscode",
        ".next", ".turbo", ".pytest_cache", ".mypy_cache", ".DS_Store",
        ".system_generated", ".Trash",
    }
    safe_dot_prefixes = (".env.example", ".env.sample", ".env.template")

    base_name = os.path.basename(os.path.abspath(dir_path)) or dir_path
    lines = [f"📁 **{base_name}/** (max depth: {max_depth})"]
    entries_count = 0
    start_time = time.monotonic()
    visited_real_dirs = {os.path.realpath(dir_path)}

    def _traverse(curr_dir: str, prefix: str, current_depth: int):
        nonlocal entries_count
        if current_depth > max_depth or entries_count >= max_entries:
            return
        if time.monotonic() - start_time > 3.0:
            return

        try:
            items = sorted(os.listdir(curr_dir))
        except Exception as e:
            lines.append(f"{prefix}└── [Permission Denied: {e}]")
            return

        dirs = []
        files = []
        for it in items:
            full_p = os.path.join(curr_dir, it)
            if os.path.isdir(full_p):
                if it in IGNORE_DIRS or it in dirs_ignore_dots:
                    continue
                if it.startswith('.') and it not in (".github", ".gitlab", ".claude", ".gemini", ".agents", ".cursor", ".vscode-test"):
                    continue
                dirs.append(it)
            else:
                if it in SECRET_FILES or it.startswith(".env.") and not it.endswith((".example", ".sample", ".template")):
                    files.append(it)
                    continue
                if any(fnmatch.fnmatch(it, pat) for pat in IGNORE_FILES):
                    continue
                if it.startswith('.') and it not in ('.gitignore', '.gitattributes', '.dockerignore', '.editorconfig', '.npmrc', '.nvmrc', '.python-version', '.prettierrc', '.eslintrc', '.eslintrc.json', '.eslintrc.js') and not any(it.startswith(p) for p in safe_dot_prefixes):
                    continue
                files.append(it)

        all_items = [(d, True) for d in dirs] + [(f, False) for f in files]
        total_items = len(all_items)

        for i, (name, is_dir) in enumerate(all_items):
            if entries_count >= max_entries:
                lines.append(f"{prefix}└── ... ({total_items - i} more items omitted for token efficiency)")
                break

            is_last = (i == total_items - 1)
            pointer = "└── " if is_last else "├── "
            extension_prefix = "    " if is_last else "│   "
            item_path = os.path.join(curr_dir, name)

            if is_dir:
                entries_count += 1
                real_sub = os.path.realpath(item_path)
                if real_sub in visited_real_dirs:
                    lines.append(f"{prefix}{pointer}📁 {name}/ [symlink cycle]")
                    continue
                lines.append(f"{prefix}{pointer}📁 {name}/")
                visited_real_dirs.add(real_sub)
                _traverse(item_path, prefix + extension_prefix, current_depth + 1)
            else:
                if name in SECRET_FILES or (name.startswith(".env.") and not name.endswith((".example", ".sample", ".template"))):
                    lines.append(f"{prefix}{pointer}🔒 {name} [PROTECTED SECRET]")
                    entries_count += 1
                    continue
                try:
                    sz = os.path.getsize(item_path)
                    sz_str = format_file_size(sz)
                except Exception:
                    sz_str = ""
                entries_count += 1
                size_label = f" ({sz_str})" if sz_str else ""
                lines.append(f"{prefix}{pointer}{name}{size_label}")

    _traverse(dir_path, "", 1)
    lines.append("\n*Pruned noisy directories (node_modules, dist, .git) & lockfiles to conserve tokens.*")
    return "\n".join(lines)

def _resolve_project_root(args: dict) -> Tuple[Optional[str], Optional[str]]:
    """
    Safely resolves the target project root without falling back to system root dirs or plugin dirs.
    """
    target = args.get("project_path") or args.get("root_dir") or args.get("path") or args.get("directory")
    active_ws = get_active_workspace()

    if target:
        if not os.path.isabs(target):
            if active_ws and os.path.isdir(os.path.join(active_ws, target)):
                target_abs = os.path.abspath(os.path.join(active_ws, target))
            else:
                target_abs = os.path.abspath(target)
        else:
            target_abs = os.path.abspath(target)

        if os.path.isfile(target_abs):
            target_abs = os.path.dirname(target_abs)
        if is_plugin_or_system_dir(target_abs):
            return None, f"Error: Refusing to scan system root or plugin directory '{target}'. Please provide a specific project directory."
        if not os.path.isdir(target_abs):
            return None, f"Error: project_path not found or not a directory: '{target}'."
        return target_abs, None

    # Check active workspace recorded by lifecycle hooks
    if active_ws and not is_plugin_or_system_dir(active_ws) and os.path.isdir(active_ws):
        return active_ws, None

    # Check environment variables
    for env_var in ("AGY_PROJECT_PATH", "WORKSPACE_FOLDER", "PROJECT_DIR"):
        candidate = os.environ.get(env_var)
        if candidate:
            cand_abs = os.path.abspath(candidate)
            if not is_plugin_or_system_dir(cand_abs) and os.path.isdir(cand_abs):
                return cand_abs, None

    # Check cwd (only if it is not inside an agent plugin or system root directory)
    cwd = os.getcwd()
    if cwd:
        cwd_abs = os.path.abspath(cwd)
        if not is_plugin_or_system_dir(cwd_abs) and os.path.isdir(cwd_abs):
            return cwd_abs, None

    # Hint with active projects if found under ~/Documents/projects
    projects_dir = os.path.expanduser("~/Documents/projects")
    if os.path.isdir(projects_dir):
        try:
            subdirs = [d for d in os.listdir(projects_dir) if not d.startswith(".") and os.path.isdir(os.path.join(projects_dir, d))]
            if subdirs:
                examples = ", ".join([f"'{d}'" for d in subdirs[:4]])
                return None, f"Error: Please specify 'project_path' explicitly. Available projects in ~/Documents/projects: {examples}. Example: project_path='{os.path.join(projects_dir, subdirs[0])}'"
        except Exception:
            pass

    return None, "Error: Invalid or root project_path. Please specify an explicit 'project_path' pointing to your project directory (e.g. project_path='/path/to/project')."

MCP_TOOL_TIMEOUT = float(os.environ.get("TOKENSAFE_MCP_TIMEOUT", "8.0"))


def _fallback_error(tool_name: str, reason: str) -> dict:
    """Returns a non-blocking, actionable error telling the AI model to try another approach."""
    clean_reason = (reason or "unknown error").strip()
    return {
        "isError": True,
        "content": [{
            "type": "text",
            "text": (
                f"⚠️ TokenSafe MCP tool '{tool_name}' could not complete ({clean_reason}). "
                "Try an alternative approach instead of repeating this exact call: "
                "narrow the target 'path'/'project_path' or 'depth', inspect a specific file with 'smart_read_file', "
                "or fall back to standard 'read_file' / bounded 'run_command'."
            )
        }]
    }


class _NoOpTracker:
    """Fallback metrics tracker if SQLite DB is temporarily unavailable or locked."""
    db_path = ""
    def record_event(self, *args, **kwargs):
        return 0
    def get_summary(self, *args, **kwargs):
        return {}
    def get_last_event(self, *args, **kwargs):
        return None
    def get_monthly_saved_tokens(self, *args, **kwargs):
        return 0


class _SafeTrackerProxy:
    """Wraps MetricsTracker so transient SQLite errors never interrupt MCP tool output."""
    def __init__(self, inner):
        self._inner = inner
        self.db_path = getattr(inner, "db_path", "")

    def record_event(self, *args, **kwargs):
        try:
            return self._inner.record_event(*args, **kwargs)
        except Exception:
            return 0

    def get_monthly_saved_tokens(self, *args, **kwargs):
        try:
            return self._inner.get_monthly_saved_tokens(*args, **kwargs)
        except Exception:
            return 0

    def get_last_event(self, *args, **kwargs):
        try:
            return self._inner.get_last_event(*args, **kwargs)
        except Exception:
            return None

    def get_summary(self, *args, **kwargs):
        try:
            return self._inner.get_summary(*args, **kwargs)
        except Exception:
            return {}

    def get_recent_events(self, *args, **kwargs):
        try:
            return self._inner.get_recent_events(*args, **kwargs)
        except Exception:
            return []

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _get_safe_tracker():
    try:
        return _SafeTrackerProxy(MetricsTracker())
    except Exception:
        return _NoOpTracker()


def _team_cache_key(path: str, symbol: str, dep_mode: str = "signature") -> str:
    """Builds a portable, content-hash-validated key shared across machines."""
    abs_path = os.path.abspath(path)
    repo_root = GraphEngine.find_project_root(abs_path)
    try:
        rel_path = os.path.relpath(abs_path, repo_root).replace("\\", "/")
    except ValueError:
        rel_path = os.path.basename(abs_path)
    content_hash = AgentFirewall._file_content_hash(abs_path)[:16]
    return f"{rel_path}::{symbol or ''}::{dep_mode}::{content_hash}"


def _team_skeleton_lookup(path: str, symbol: str, dep_mode: str = "signature") -> Optional[str]:
    """Team cache pre-warm lookup: returns a teammate's cached skeleton or None.

    Never blocks the caller for more than ~450ms: the HTTP timeout matches
    the thread join so discarded timeouts don't inflate cloud hit_count.
    Non-team installs skip the network entirely."""
    try:
        is_team, lic_key = TeamCache._is_team_tier()
        if not is_team:
            return None
        cache_key = _team_cache_key(path, symbol, dep_mode=dep_mode)
        box: dict = {}

        def _fetch():
            try:
                box["value"] = TeamCache.get("SKELETON", cache_key, timeout=0.45, lic_key=lic_key)
            except Exception:
                pass

        t = threading.Thread(target=_fetch, daemon=True)
        t.start()
        t.join(0.45)
        return box.get("value")
    except Exception:
        return None


def _team_skeleton_prewarm(
    path: str,
    symbol: str,
    skeleton_text: str,
    tokens_saved: int,
    dep_mode: str = "signature",
):
    """Publishes a freshly built skeleton to the team cache (async, team tier only).

    Teammate B's later smart_read_file on the same symbol hits the cache instead
    of re-reading and re-skeletonizing the file."""
    try:
        cache_key = _team_cache_key(path, symbol, dep_mode=dep_mode)
        TeamCache.put("SKELETON", cache_key, skeleton_text, tokens_saved=tokens_saved)
    except Exception:
        pass


def _safe_int(value, default: int, lo: Optional[int] = None, hi: Optional[int] = None) -> int:
    if value is None or isinstance(value, bool):
        n = default
    else:
        try:
            n = int(value)
        except (TypeError, ValueError):
            n = default
    if lo is not None:
        n = max(lo, n)
    if hi is not None:
        n = min(hi, n)
    return n


def _is_binary_file(path: str, probe_bytes: int = 4096) -> bool:
    try:
        with open(path, "rb") as f:
            chunk = f.read(probe_bytes)
        return b"\x00" in chunk
    except OSError:
        return False


def _read_line_slice(path: str, start_line: Optional[int], end_line: Optional[int]) -> Tuple[Optional[str], Optional[str]]:
    """Reads 1-based [start_line..end_line] using bounded iteration. Returns (content, err_msg)."""
    if start_line is not None and start_line < 1:
        start_line = 1
    if end_line is not None and end_line < 1:
        return None, "end_line must be >= 1"
    if start_line is not None and end_line is not None and start_line > end_line:
        return None, f"start_line ({start_line}) cannot be greater than end_line ({end_line})"

    s = 1 if start_line is None else start_line
    selected: List[str] = []
    total_seen = 0
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for idx, line in enumerate(f, 1):
            total_seen = idx
            if idx < s:
                continue
            if end_line is not None and idx > end_line:
                break
            selected.append(line)
    if s > total_seen and total_seen > 0:
        return None, f"start_line {s} is beyond end of file ({total_seen} lines)"
    return "".join(selected), None


def _resolve_dep_mode(args: dict) -> Tuple[bool, str]:
    raw_inc = args.get("include_deps")
    inc_deps = True if raw_inc is None else bool(raw_inc)
    if not inc_deps:
        return False, "none"
    raw_mode = str(args.get("dep_mode") or "").strip().lower()
    if raw_mode in ("signature", "full"):
        return True, raw_mode
    return True, "signature"


def _repo_fingerprint(graph) -> str:
    """Stable Shared-Team-Symbol-Index cache key for an indexed repo: repo name +
    per-file relative paths + sizes. Machine-independent on purpose (mtimes differ
    across clones, sizes survive them); a 7-day TTL bounds staleness on the cloud."""
    import hashlib
    h = hashlib.sha256()
    root = os.path.abspath(getattr(graph, "root_dir", "") or "")
    h.update(os.path.basename(root).encode("utf-8", "ignore"))
    stats = getattr(graph, "file_stats", {}) or {}
    for rel in sorted(stats.keys()):
        _m, s = stats[rel]
        h.update(f"{rel}:{int(s)};".encode("utf-8", "ignore"))
    if not stats:
        h.update(f"symbols:{len(getattr(graph, 'symbols', {}) or {})}".encode("utf-8", "ignore"))
    return h.hexdigest()[:40]


def handle_tool_call(name: str, args: dict) -> dict:
    if args is None or not isinstance(args, dict):
        args = {}
    try:
        result = _dispatch_tool_call(name, args)
        if name == "smart_read_file" and isinstance(result, dict) and not result.get("isError"):
            content_parts = result.get("content") or []
            text_out = "".join(p.get("text", "") for p in content_parts if isinstance(p, dict))
            path = args.get("path") or args.get("file_path") or args.get("file") or ""
            symbol = (args.get("symbol") or args.get("sym") or args.get("target") or "").strip()
            _, eff_dep_mode = _resolve_dep_mode(args)
            cache_sym = f"{symbol}@{eff_dep_mode}" if (symbol and eff_dep_mode != "signature") else symbol
            is_ranged = args.get("start_line") is not None or args.get("end_line") is not None
            is_quota_truncated = "[TokenSafe Community 500k limit reached:" in text_out or "// Quota exceeded" in text_out
            if text_out and path and not is_ranged and not is_quota_truncated:
                abs_path = os.path.abspath(path)
                cur_hash = AgentFirewall._file_content_hash(abs_path)
                if cur_hash:
                    _IN_PROCESS_DELIVERED[(abs_path, symbol, eff_dep_mode)] = (cur_hash, _EPOCH_GEN)
                # Never overwrite cached skeleton with a firewall header or pointer hit
                if not text_out.startswith("// [TokenSafe Subagent Firewall:"):
                    try:
                        AgentFirewall.record_cache(
                            session_id=_mcp_session_id(),
                            file_path=path,
                            content=text_out,
                            symbol=cache_sym,
                            tracker=_get_safe_tracker()
                        )
                    except Exception:
                        pass
        return result
    except Exception as e:
        return _fallback_error(name, f"{type(e).__name__}: {str(e)}")


def _quota_banner_if_due(quota: dict, tracker, session_id: str) -> str:
    """Emits a concise quota CTA on turn #1 of exceedance and every 5 turns."""
    if not quota.get("is_quota_exceeded"):
        return ""
    if LicenseManager.should_emit_quota_banner(tracker, session_id=session_id, every_n_turns=5):
        return f"// {quota.get('warning', '')}\n"
    return ""


def _tier_quota_limit_msg(quota: dict) -> str:
    """Returns a concise description of the active tier's quota limit and activation CTA."""
    tier = str(quota.get("tier") or "guest").lower()
    limit = int(quota.get("monthly_limit") or (50000 if tier == "guest" else 500000))
    tier_name = "Guest 50k" if tier == "guest" else "Community 500k"
    cta = "Run 'tokensafe login' in your terminal for 500,000 free tokens/month." if tier == "guest" else "Upgrade to Solo: https://tokensafe.dev/pricing"
    return f"{tier_name} monthly limit ({limit:,} tokens) reached; optimizations paused ({cta})."


def _dispatch_tool_call(name: str, args: dict) -> dict:
    global _EPOCH_GEN
    tracker = _get_safe_tracker()
    quota = LicenseManager.check_quota(tracker)
    is_quota_exceeded = bool(quota.get("is_quota_exceeded", False))

    if name == "smart_read_file":
        path = args.get("path") or args.get("file_path") or args.get("file") or ""
        symbol = args.get("symbol") or args.get("sym") or args.get("target") or ""
        inc_deps, eff_dep_mode = _resolve_dep_mode(args)
        force_full = bool(args.get("force_full", False))
        start_line = args.get("start_line")
        end_line = args.get("end_line")
        # Clamp explicit line ranges to sane bounds so bad indices can never produce
        # empty output or negative-index slicing artifacts.
        try:
            if start_line is not None and not isinstance(start_line, bool):
                start_line = int(start_line)
            elif isinstance(start_line, bool):
                raise TypeError("start_line cannot be bool")
            if end_line is not None and not isinstance(end_line, bool):
                end_line = int(end_line)
            elif isinstance(end_line, bool):
                raise TypeError("end_line cannot be bool")
        except (TypeError, ValueError):
            return _fallback_error("smart_read_file", "start_line/end_line must be integers")

        if end_line is not None and end_line < 1:
            return _fallback_error("smart_read_file", "end_line must be >= 1")
        if start_line is not None:
            start_line = max(1, start_line)

        active_ws = get_active_workspace()
        if path and not os.path.isabs(path):
            if active_ws and os.path.exists(os.path.join(active_ws, path)):
                path = os.path.abspath(os.path.join(active_ws, path))
            else:
                path = os.path.abspath(path)
        elif path:
            path = os.path.abspath(path)

        if not path or not os.path.exists(path):
            return {"isError": True, "content": [{"type": "text", "text": f"File not found: {path}"}]}
        if is_plugin_or_system_dir(path):
            return _fallback_error("smart_read_file", f"refusing to read from system root or plugin dir '{path}'")
        try:
            st = os.stat(path)
            if not stat.S_ISREG(st.st_mode):
                return _fallback_error("smart_read_file", f"path is not a regular file: {path}")
        except OSError as e:
            return _fallback_error("smart_read_file", f"cannot stat path: {e}")

        if _is_binary_file(path):
            return _fallback_error("smart_read_file", f"'{os.path.basename(path)}' appears to be a binary file")

        # Bloated lockfiles (package-lock.json, Cargo.lock, poetry.lock, pnpm-lock.yaml):
        # Extract compact dependency bill with reversible hash anchor instead of a 30k token dump.
        from src.format_parsers.lockfiles import LockfileReducer
        if LockfileReducer.is_lockfile(path):
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    raw_lock_text = f.read()
                reduced_lock, was_reduced = LockfileReducer.reduce(raw_lock_text, os.path.basename(path))
                if was_reduced:
                    orig_t = count_tokens(raw_lock_text)
                    opt_t = count_tokens(reduced_lock)
                    tracker.record_event(
                        session_id=_mcp_session_id(),
                        event_type="LOCKFILE_REDUCE",
                        original_tokens=orig_t,
                        optimized_tokens=opt_t,
                        details={"file": os.path.basename(path), "saved": orig_t - opt_t}
                    )
                    return {"content": [{"type": "text", "text": reduced_lock}]}
            except Exception:
                pass

        # Tabular and database dump files (CSV, TSV, SQL, HAR):
        # Extract schema, data types, and sample rows instead of dumping 10k rows into context.
        from src.format_parsers.tabular import TabularReducer
        if TabularReducer.is_tabular_or_dump(path):
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    raw_data_text = f.read()
                ext_lower = os.path.splitext(path)[1].lower()
                if ext_lower in (".csv", ".tsv"):
                    reduced_tab, was_reduced = TabularReducer.reduce_csv(raw_data_text)
                elif ext_lower == ".sql":
                    reduced_tab, was_reduced = TabularReducer.reduce_sql_dump(raw_data_text)
                elif ext_lower == ".har":
                    reduced_tab, was_reduced = TabularReducer.reduce_har(raw_data_text)
                else:
                    reduced_tab, was_reduced = raw_data_text, False

                if was_reduced:
                    orig_t = count_tokens(raw_data_text)
                    opt_t = count_tokens(reduced_tab)
                    tracker.record_event(
                        session_id=_mcp_session_id(),
                        event_type="TABULAR_REDUCE",
                        original_tokens=orig_t,
                        optimized_tokens=opt_t,
                        details={"file": os.path.basename(path), "saved": orig_t - opt_t}
                    )
                    return {"content": [{"type": "text", "text": reduced_tab}]}
            except Exception:
                pass

        # Kubernetes manifests (.yaml / .yml with multiple resources):
        if path.lower().endswith((".yaml", ".yml")):
            from src.format_parsers.docker_k8s import ContainerAndK8sReducer
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    raw_k8s_text = f.read()
                reduced_k8s, was_k8s = ContainerAndK8sReducer.reduce_k8s_yaml(raw_k8s_text)
                if was_k8s:
                    orig_t = count_tokens(raw_k8s_text)
                    opt_t = count_tokens(reduced_k8s)
                    tracker.record_event(
                        session_id=_mcp_session_id(),
                        event_type="K8S_MANIFEST_REDUCE",
                        original_tokens=orig_t,
                        optimized_tokens=opt_t,
                        details={"file": os.path.basename(path), "saved": orig_t - opt_t}
                    )
                    return {"content": [{"type": "text", "text": reduced_k8s}]}
            except Exception:
                pass

        # Structured NDJSON / JSON log files (.ndjson, .log, .jsonl):
        if path.lower().endswith((".ndjson", ".log", ".jsonl")):
            from src.format_parsers.json_log import JSONLogReducer
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    raw_log_text = f.read()
                if JSONLogReducer.is_ndjson_log(raw_log_text):
                    reduced_log, was_log = JSONLogReducer.reduce(raw_log_text)
                    if was_log:
                        orig_t = count_tokens(raw_log_text)
                        opt_t = count_tokens(reduced_log)
                        tracker.record_event(
                            session_id=_mcp_session_id(),
                            event_type="JSON_LOG_REDUCE",
                            original_tokens=orig_t,
                            optimized_tokens=opt_t,
                            details={"file": os.path.basename(path), "saved": orig_t - opt_t}
                        )
                        return {"content": [{"type": "text", "text": reduced_log}]}
            except Exception:
                pass

        # Heavy declarative schema files (OpenAPI/Swagger/Prisma/GraphQL/SQL DDL):
        # generic AST skeletonization does not apply — route to the schema slicer.
        # `symbol` carries the target (a spec path, model, type or table); an empty
        # symbol yields a compact overview instead of a 20k-50k token dump.
        schema_kind = SchemaSlicer.detect(os.path.abspath(path))
        if schema_kind:
            sliced, sinfo = SchemaSlicer.slice_file(os.path.abspath(path), target=(symbol or ""))
            if sliced:
                orig_t = max(1000, st.st_size // 4)
                sliced_t = count_tokens(sliced)
                tracker.record_event(
                    session_id=_mcp_session_id(),
                    event_type="SCHEMA_SLICE",
                    original_tokens=orig_t,
                    optimized_tokens=sliced_t,
                    details={"kind": sinfo.get("kind"), "mode": sinfo.get("mode"), "target": (symbol or "")[:120]}
                )
                header = f"// [TokenSafe Schema Slice: {sinfo.get('kind')} | mode={sinfo.get('mode')} | target='{(symbol or '')[:80]}']\n"
                return {"content": [{"type": "text", "text": header + sliced}]}

        # Community 500k quota reached: soft-degrade to bounded raw read (disable Graph/AST/Pointer/Team Cache)
        if is_quota_exceeded:
            if start_line is not None or end_line is not None:
                raw_chunk, err = _read_line_slice(path, start_line, end_line)
                if err:
                    return _fallback_error("smart_read_file", err)
            else:
                cap = 250
                raw_lines: List[str] = []
                total_seen = 0
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    for idx, line in enumerate(f, 1):
                        total_seen = idx
                        if idx <= cap:
                            raw_lines.append(line)
                raw_chunk = "".join(raw_lines)
                if total_seen > cap:
                    raw_chunk += f"\n// [TokenSafe {_tier_quota_limit_msg(quota)} Showing raw lines 1-{cap} of {total_seen}. AST/graph slicing & pointer dedup are paused.]\n"
            banner = _quota_banner_if_due(quota, tracker, _mcp_session_id())
            return {"content": [{"type": "text", "text": banner + (raw_chunk or "")}]}

        abs_path = os.path.abspath(path)
        sym_clean = (symbol or "").strip()
        cache_sym = f"{sym_clean}@{eff_dep_mode}" if (sym_clean and eff_dep_mode != "signature") else sym_clean

        if start_line is None and end_line is None:
            cur_hash = AgentFirewall._file_content_hash(abs_path)
            delivered = _IN_PROCESS_DELIVERED.get((abs_path, sym_clean, eff_dep_mode))
            already_here = bool(delivered and cur_hash and delivered == (cur_hash, _EPOCH_GEN))
            allow_pointer = (not force_full) and _client_supports_pointers() and already_here
            is_hit, cached_text, saved_dedup = AgentFirewall.check_cache(
                _mcp_session_id(),
                path,
                symbol=cache_sym,
                tracker=tracker,
                allow_pointer=allow_pointer,
            )
            if is_hit and cached_text:
                return {"content": [{"type": "text", "text": cached_text}]}

        # Check loop detector (skip when force_full explicitly recovers from a pointer)
        if not force_full:
            is_loop, repeats, msg = LoopDetector.check_loop(_mcp_session_id(), "smart_read_file", args)
            if is_loop:
                return {"isError": True, "content": [{"type": "text", "text": f"⚠️ {msg}"}]}

        # Explicit line range (handles single-bound start_line or end_line as well as both)
        if start_line is not None or end_line is not None:
            content, err = _read_line_slice(path, start_line, end_line)
            if err:
                return _fallback_error("smart_read_file", err)
            slice_lines = (content or "").splitlines()
            slice_first = start_line if start_line is not None else 1
            slice_last = slice_first + len(slice_lines) - 1 if slice_lines else max(1, slice_first - 1)
            range_label = f"{slice_first}-{slice_last}" if slice_lines else str(slice_first)
            prefetch_block = ""
            if not sym_clean or eff_dep_mode == "full":
                prefetch_block, _ = DependencyPrefetcher.prefetch_for_file(path)
            slice_header = f"// [TOCP Raw Slice: lines {range_label} of {os.path.basename(path)}]\n"
            delivered = (prefetch_block or "") + slice_header + (content or "")
            tracker.record_event(
                session_id=_mcp_session_id(),
                event_type="RAW_SLICE",
                original_tokens=max(200, st.st_size // 4),
                optimized_tokens=count_tokens(delivered),
                details={"file": os.path.basename(path), "lines": range_label, "symbol": sym_clean[:120]}
            )
            return {"content": [{"type": "text", "text": delivered}]}

        if symbol:
            # Team pre-warm hit: a teammate already skeletonized this symbol
            team_skel = _team_skeleton_lookup(path, symbol, dep_mode=eff_dep_mode)
            if team_skel:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    raw_code = f.read()
                orig_t = count_tokens(raw_code)
                skel_t = count_tokens(team_skel)
                tracker.record_event(
                    session_id=_mcp_session_id(),
                    event_type="TEAM_CACHE_HIT",
                    original_tokens=orig_t,
                    optimized_tokens=skel_t,
                    details={"file": os.path.basename(path), "symbol": symbol, "dep_mode": eff_dep_mode, "source": "team_cache"}
                )
                prefetch_block = ""
                if eff_dep_mode == "full":
                    prefetch_block, _ = DependencyPrefetcher.prefetch_for_file(path)
                header = f"// [TokenSafe Team-Cached Skeleton for '{symbol}' | shared by a teammate]\n"
                return {"content": [{"type": "text", "text": (prefetch_block or "") + header + team_skel}]}

            try:
                graph, rel_path = GraphEngine.for_file(path)
                graph_skel = graph.graph_guided_skeletonize(
                    path,
                    target_symbol=symbol,
                    include_deps=inc_deps,
                    dep_mode=eff_dep_mode,
                    collapse_imports=(eff_dep_mode != "full"),
                )
                if graph_skel:
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        raw_code = f.read()
                    orig_t = count_tokens(raw_code)
                    skel_t = count_tokens(graph_skel)
                    saved = max(0, orig_t - skel_t)
                    tracker.record_event(
                        session_id=_mcp_session_id(),
                        event_type="GRAPH_SKELETON",
                        original_tokens=orig_t,
                        optimized_tokens=skel_t,
                        details={
                            "file": os.path.basename(path),
                            "symbol": symbol,
                            "saved_tokens": saved,
                            "include_deps": inc_deps,
                            "dep_mode": eff_dep_mode,
                        }
                    )
                    if not inc_deps:
                        dep_label = "Surgical single-symbol isolation"
                    elif eff_dep_mode == "full":
                        dep_label = "Full body + 1-hop dependencies"
                    else:
                        dep_label = "Full body + 1-hop signature deps"
                    header = f"// [TokenSafe Graph-Guided Skeleton for '{symbol}' | {dep_label}]\n"
                    _team_skeleton_prewarm(path, symbol, graph_skel, saved, dep_mode=eff_dep_mode)
                    prefetch_block = ""
                    if eff_dep_mode == "full":
                        prefetch_block, _ = DependencyPrefetcher.prefetch_for_file(path)
                    return {"content": [{"type": "text", "text": (prefetch_block or "") + header + graph_skel}]}
            except Exception:
                pass

            overwrite, metrics = ToolSlicer.optimize_read_file({"Path": path}, context_hint=symbol)
            if overwrite:
                s_line = overwrite["StartLine"]
                e_line = overwrite["EndLine"]
                content, _ = _read_line_slice(path, s_line, e_line)
                prefetch_block = ""
                if eff_dep_mode == "full":
                    prefetch_block, _ = DependencyPrefetcher.prefetch_for_file(path)
                header = f"// [TOCP Targeted Window: lines {s_line}-{e_line} for '{symbol}']\n"
                return {"content": [{"type": "text", "text": (prefetch_block or "") + header + (content or "")}]}

        # Check AST skeleton (preserves target_symbol body when symbol was given; avoids duplicate prefetch)
        skel_path, orig_t, skel_t = Skeletonizer.get_skeleton_file(path, target_symbol=(symbol or None))
        if skel_path and os.path.exists(skel_path):
            with open(skel_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            if orig_t > skel_t:
                tracker.record_event(
                    session_id=_mcp_session_id(),
                    event_type="AST_SKELETON",
                    original_tokens=orig_t,
                    optimized_tokens=skel_t,
                    details={"target_file": os.path.basename(path), "saved_tokens": orig_t - skel_t}
                )
            return {"content": [{"type": "text", "text": content}]}

        # Default bounded read (avoid dumping multi-MB files raw)
        prefetch_block, _ = DependencyPrefetcher.prefetch_for_file(path)
        max_raw_bytes = 512 * 1024
        if st.st_size > max_raw_bytes:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(max_raw_bytes)
            content += f"\n// [TokenSafe: file truncated at 512KB ({st.st_size:,} bytes total). Use start_line/end_line or symbol to inspect specific sections.]\n"
        else:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        return {"content": [{"type": "text", "text": (prefetch_block or "") + content}]}

    elif name == "smart_bundle":
        targets = args.get("targets")
        if not isinstance(targets, list) or not targets:
            return _fallback_error("smart_bundle", "missing required 'targets' array of {path, symbol} objects")
        if len(targets) > 8:
            return _fallback_error("smart_bundle", "too many targets (max 8)")
        if is_quota_exceeded:
            LicenseManager.should_emit_quota_banner(tracker, session_id=_mcp_session_id(), every_n_turns=5)
            tier_msg = _tier_quota_limit_msg(quota)
            return {"content": [{"type": "text", "text": f"{quota.get('warning', '')}\n\nsmart_bundle is paused: {tier_msg}"}]}

        inc_deps, _eff_dep_mode = _resolve_dep_mode(args)
        max_tokens = _safe_int(os.environ.get("TOKENSAFE_BUNDLE_MAX_TOKENS"), 6000, lo=1000, hi=50000)

        active_ws = get_active_workspace()
        by_file: Dict[str, List[str]] = {}
        for t in targets:
            if not isinstance(t, dict) or not str(t.get("path") or "").strip():
                return _fallback_error("smart_bundle", "each target must be {path: str, symbol?: str}")
            raw_p = str(t["path"]).strip()
            if not os.path.isabs(raw_p) and active_ws and os.path.exists(os.path.join(active_ws, raw_p)):
                target_p = os.path.abspath(os.path.join(active_ws, raw_p))
            else:
                target_p = os.path.abspath(raw_p)
            if is_plugin_or_system_dir(target_p):
                return _fallback_error("smart_bundle", f"refusing to read from system root or plugin dir '{raw_p}'")
            by_file.setdefault(target_p, []).append(str(t.get("symbol") or "").strip())

        missing = [p for p in by_file if not os.path.isfile(p)]
        if missing:
            return {"isError": True, "content": [{"type": "text", "text": f"File(s) not found: {', '.join(missing)}"}]}

        bundle_max_bytes = 512 * 1024
        file_sections: List[Tuple[str, str, List[str]]] = []  # (abs_path, skeleton, import_lines)
        dep_lines: List[str] = []
        seen_dep_lines: set = set()
        orig_tokens_est = 0

        _imp_line_re = re.compile(r"^\s*(?:import\s|from\s+\S+\s+import\b|use\s|using\s|#include|require(?:_relative)?\s|extern crate\s).*$", re.MULTILINE)

        for p, symbols in by_file.items():
            try:
                st_b = os.stat(p)
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    code = f.read(bundle_max_bytes)
            except OSError as e:
                return _fallback_error("smart_bundle", f"cannot read {p}: {e}")
            orig_tokens_est += max(200, st_b.st_size // 4)
            ext = os.path.splitext(p)[1].lower()
            keep = {s for s in symbols if s} or None
            skel = Skeletonizer.skeletonize_code(code, ext, keep_symbols=keep)
            imp_lines = sorted({m.group(0).strip() for m in _imp_line_re.finditer(skel)})
            file_sections.append((p, skel, imp_lines))

            if inc_deps and keep:
                try:
                    graph, rel = GraphEngine.for_file(p)
                    if graph and rel:
                        for sym in keep:
                            # A class target's call edges live on its methods:
                            # resolve every qual key under the target symbol.
                            keys = [k for k in graph.symbols if k == f"{rel}::{sym}" or k.startswith(f"{rel}::{sym}::")]
                            for key in keys[:12]:
                                qual = key[len(rel) + 2:]
                                nb = graph.get_neighborhood(qual, target_file=rel)
                                for dep_key in sorted(nb.get("one_hop", [])):
                                    dep_sym = graph.symbols.get(dep_key)
                                    if not dep_sym:
                                        continue
                                    line = f"  {qual} -> {dep_key[len(rel) + 2:]}: {dep_sym.signature.strip()[:160]}"
                                    if line not in seen_dep_lines:
                                        seen_dep_lines.add(line)
                                        dep_lines.append(line)
                except Exception:
                    pass

        # Shared imports: lines present in 2+ files are emitted once
        from collections import Counter
        imp_counter = Counter()
        for _p, _skel, imps in file_sections:
            imp_counter.update(set(imps))
        shared = sorted({ln for ln, cnt in imp_counter.items() if cnt >= 2 and len(ln) > 4})

        parts: List[str] = []
        if shared:
            parts.append("// [Shared imports — deduplicated across files]")
            parts.extend(f"  {ln}" for ln in shared[:40])
        for p, skel, imps in file_sections:
            body = skel
            if shared:
                body = "\n".join(ln for ln in body.splitlines() if ln.strip() not in shared)
            parts.append(f"\n// ===== {os.path.basename(p)} ({len(by_file[p])} symbol target(s)) =====")
            parts.append(body)
        if dep_lines:
            parts.append("\n// [Merged 1-hop dependency signatures (deduplicated)]")
            parts.extend(dep_lines)
        bundle = "\n".join(parts)

        bundle_t = count_tokens(bundle)
        if bundle_t > max_tokens and dep_lines:
            # Graceful degradation: drop the merged dependency signatures first
            deps_idx = bundle.find("\n// [Merged 1-hop dependency signatures")
            if deps_idx > 0:
                bundle = bundle[:deps_idx] + (
                    f"\n// [TokenSafe smart_bundle: dependency signatures omitted (output cap {max_tokens} tokens)"
                    " — narrow the 'targets' list for deeper detail.]"
                )
            bundle_t = count_tokens(bundle)
        if bundle_t > max_tokens:
            bundle += f"\n// [TokenSafe smart_bundle: output exceeds the {max_tokens}-token cap — narrow the 'targets' list.]"

        tracker.record_event(
            session_id=_mcp_session_id(),
            event_type="BUNDLE",
            original_tokens=min(orig_tokens_est, 200000),
            optimized_tokens=bundle_t,
            details={"files": len(by_file), "targets": len(targets), "shared_imports": len(shared), "dep_lines": len(dep_lines)}
        )
        return {"content": [{"type": "text", "text": bundle}]}

    elif name == "compress_prompt":
        prompt = args.get("prompt", "")
        if is_quota_exceeded:
            emit_warn = LicenseManager.should_emit_quota_banner(tracker, session_id=_mcp_session_id(), every_n_turns=5)
            payload = {
                "original_text": prompt,
                "compact_text": prompt,
                "saved_tokens": 0,
                "tier": "QUOTA_EXCEEDED",
                "quota_exceeded": True,
                "warning": quota.get("warning") if emit_warn else _tier_quota_limit_msg(quota),
            }
            return {"content": [{"type": "text", "text": json.dumps(payload, indent=2, ensure_ascii=False)}]}
        res = PromptCompactor.compact(prompt, enable_distill=True)
        return {"content": [{"type": "text", "text": json.dumps(res, indent=2, ensure_ascii=False)}]}

    elif name == "context_budget":
        model = str(args.get("model") or "")
        window_tokens = args.get("window_tokens")
        session_id = str(args.get("session_id") or _mcp_session_id())
        transcript_path = str(args.get("transcript_path") or "")
        try:
            if window_tokens is not None:
                window_tokens = int(window_tokens)
        except (TypeError, ValueError):
            window_tokens = None
        budget = ContextBudget.estimate(
            tracker, session_id=session_id, model=model, window_tokens=window_tokens,
            transcript_path=transcript_path
        )
        try:
            from src.quality_score import ContextQualityScore
            from src.environment_auditor import EnvironmentAuditor
            audit = EnvironmentAuditor.audit()
            budget["quality_score"] = ContextQualityScore.calculate(budget=budget, audit=audit)
        except Exception:
            pass
        return {"content": [{"type": "text", "text": json.dumps(budget, indent=2, ensure_ascii=False)}]}

    elif name == "compact_conversation":
        transcript_path = args.get("transcript_path", "")
        try:
            invocation_num = max(3, int(args.get("invocation_num", 3) or 3))
        except (TypeError, ValueError):
            invocation_num = 3
        if not transcript_path or not os.path.exists(transcript_path):
            return {"isError": True, "content": [{"type": "text", "text": f"Transcript not found: {transcript_path}"}]}
        if is_quota_exceeded:
            LicenseManager.should_emit_quota_banner(tracker, session_id=_mcp_session_id(), every_n_turns=5)
            return {"content": [{"type": "text", "text": json.dumps({
                "compactable": False,
                "quota_exceeded": True,
                "monthly_saved": quota.get("monthly_saved"),
                "monthly_limit": quota.get("monthly_limit"),
                "reason": quota.get("warning"),
            }, indent=2, ensure_ascii=False)}]}
        ledger, orig_t, folded_t, stats = HistoryCompactor.fold_transcript(transcript_path, invocation_num)
        if not ledger:
            return {"content": [{"type": "text", "text": json.dumps({
                "compactable": False,
                "reason": "Transcript too short to fold (needs >= 4 steps and >= 1000 history tokens)"
            }, indent=2)}]}
        _EPOCH_GEN += 1
        saved = max(0, orig_t - folded_t)
        # Incremental metrics guard (same policy as the PreInvocation hook):
        # repeated compaction of the SAME transcript must not re-record the
        # full history mass — count only the delta since the last fold.
        prev_event = tracker.get_last_event(_mcp_session_id(), "HISTORY_FOLD")
        prev_same_path = bool(
            prev_event and isinstance(prev_event.get("details"), dict)
            and prev_event["details"].get("transcript_path") == transcript_path
        )
        prev_orig = prev_event["details"].get("total_accum_orig", 0) if prev_same_path else 0
        delta_orig = max(0, orig_t - prev_orig)
        if not prev_same_path or delta_orig >= 1500:
            inc_orig = orig_t if not prev_same_path else delta_orig
            tracker.record_event(
                session_id=_mcp_session_id(),
                event_type="HISTORY_FOLD",
                original_tokens=inc_orig,
                optimized_tokens=folded_t,
                details={"mode": "manual_compact_conversation", "turns": invocation_num,
                         "transcript_path": transcript_path, "total_accum_orig": orig_t, **stats}
            )
        digest_dict = stats.get("orientation_digest") or {}
        return {"content": [{"type": "text", "text": json.dumps({
            "compactable": True,
            "ledger": ledger,
            "orientation_digest": digest_dict,
            "original_tokens": orig_t,
            "folded_tokens": folded_t,
            "saved_tokens": saved,
            "note": "Inject this ledger and 3-line orientation digest as a system message to resume flow instantly.",
            "stats": stats
        }, indent=2, ensure_ascii=False)}]}

    elif name == "sanitize_command":
        cmd = args.get("command") or args.get("cmd") or args.get("c") or ""
        # Step 1: TokenSafe Guard security inspection
        guard_res = TokenSafeGuard.inspect_command(cmd)
        if guard_res.blocked:
            tracker.record_event(
                session_id=_mcp_session_id(),
                event_type=f"GUARD_{guard_res.check_name}",
                original_tokens=count_tokens(cmd),
                optimized_tokens=0,
                details={"cmd": cmd[:100], "reason": guard_res.reason}
            )
            result = {
                "original_command": cmd,
                "sanitized_command": None,
                "blocked": True,
                "check_name": guard_res.check_name,
                "reason": guard_res.reason,
                "recommendation": "Blocked by TokenSafe Guard. Do not execute this command."
            }
            return {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]}

        # Step 2: Command sanitization (paused at Community 500k quota while Security Guard stays active)
        if is_quota_exceeded:
            emit_note = LicenseManager.should_emit_quota_banner(tracker, session_id=_mcp_session_id(), every_n_turns=5)
            result = {
                "original_command": cmd,
                "sanitized_command": cmd,
                "blocked": False,
                "tokens_saved": 0,
                "quota_exceeded": True,
                "reason": quota.get("warning") if emit_note else _tier_quota_limit_msg(quota),
            }
            if guard_res.warning_only:
                result["warning"] = guard_res.reason
            return {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]}

        new_cmd, saved, reason = CommandSanitizer.sanitize(cmd, cwd=args.get("cwd") or None)
        result = {
            "original_command": cmd,
            "sanitized_command": new_cmd or cmd,
            "blocked": False,
            "tokens_saved": saved,
            "reason": reason or "No modification needed"
        }
        if guard_res.warning_only:
            result["warning"] = guard_res.reason
        return {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]}

    elif name == "guard_inspect":
        cmd = args.get("command") or args.get("cmd") or args.get("c")
        code = args.get("code") or args.get("snippet")
        file_path = args.get("file_path") or args.get("path") or args.get("file") or ""

        results = []
        if cmd is not None:
            c_res = TokenSafeGuard.inspect_command(cmd)
            results.append({
                "target": "command",
                "input": cmd,
                "allowed": not c_res.blocked,
                "blocked": c_res.blocked,
                "check_name": c_res.check_name,
                "reason": c_res.reason,
                "warning_only": c_res.warning_only
            })
            if c_res.blocked:
                tracker.record_event(
                    session_id=_mcp_session_id(),
                    event_type=f"GUARD_{c_res.check_name}",
                    original_tokens=count_tokens(cmd),
                    optimized_tokens=0,
                    details={"cmd": cmd[:100], "reason": c_res.reason}
                )
            else:
                # Adapter-level command checks (v2.1.0): TokenSafeGuard's
                # command audit does not scan for literal credentials, so
                # deny-level secrets/destruction come from the adapter core —
                # the same patterns the Claude Code / Cursor shell guards use.
                try:
                    from src.adapter_core import (_SECRET_PATTERNS, _DESTRUCTIVE_PATTERNS,
                                                  _INFINITE_PATTERNS, _TLS_BYPASS_PATTERNS)
                    hit = None
                    for pat in _SECRET_PATTERNS:
                        if re.search(pat, cmd):
                            hit = ("SECRET", "Blocked: command contains a hardcoded secret", True)
                            break
                    if hit is None:
                        for pat in _DESTRUCTIVE_PATTERNS:
                            if re.search(pat, cmd):
                                hit = ("DESTRUCTIVE", "Blocked: irreversible/destructive operation", True)
                                break
                    if hit:
                        results.append({
                            "target": "command",
                            "input": cmd,
                            "allowed": False,
                            "blocked": True,
                            "check_name": hit[0],
                            "reason": hit[1],
                            "warning_only": False
                        })
                        tracker.record_event(
                            session_id=_mcp_session_id(),
                            event_type=f"GUARD_{hit[0]}",
                            original_tokens=count_tokens(cmd),
                            optimized_tokens=0,
                            details={"cmd": cmd[:100], "reason": hit[1]}
                        )
                    else:
                        # Advisory tier: infinite streams and TLS bypass never
                        # block an inspection, and inspections write no events.
                        advisory = None
                        for pat in _INFINITE_PATTERNS:
                            if re.search(pat, cmd):
                                advisory = ("INFINITE_STREAM", "Command never terminates on its own (follow/streaming) — run it in the background or confirm intent")
                                break
                        if advisory is None:
                            for pat in _TLS_BYPASS_PATTERNS:
                                if re.search(pat, cmd):
                                    advisory = ("TLS_VERIFY_BYPASS", "Certificate verification disabled — confirm this is intentional")
                                    break
                        if advisory:
                            results.append({
                                "target": "command",
                                "input": cmd,
                                "allowed": True,
                                "blocked": False,
                                "check_name": advisory[0],
                                "reason": advisory[1],
                                "warning_only": True
                            })
                except Exception:
                    pass

        if code is not None:
            w_res = TokenSafeGuard.inspect_file_write(file_path, code)
            results.append({
                "target": "code",
                "file_path": file_path,
                "allowed": not w_res.blocked,
                "blocked": w_res.blocked,
                "check_name": w_res.check_name,
                "reason": w_res.reason,
                "warning_only": w_res.warning_only
            })
            if w_res.blocked:
                tracker.record_event(
                    session_id=_mcp_session_id(),
                    event_type=f"GUARD_{w_res.check_name}",
                    original_tokens=count_tokens(code),
                    optimized_tokens=0,
                    details={"target_file": os.path.basename(file_path) if file_path else "unknown", "reason": w_res.reason}
                )

        if not results:
            return {
                "isError": True,
                "content": [{"type": "text", "text": "Error: Please provide 'command' or 'code' (with optional 'file_path') to inspect."}]
            }

        overall_blocked = any(r["blocked"] for r in results)
        payload = {
            "passed": not overall_blocked,
            "blocked": overall_blocked,
            "checks": results
        }
        return {"content": [{"type": "text", "text": json.dumps(payload, indent=2)}]}

    elif name == "slice_diff":
        raw_diff = args.get("diff", "")
        if is_quota_exceeded:
            banner = _quota_banner_if_due(quota, tracker, _mcp_session_id())
            return {"content": [{"type": "text", "text": banner + raw_diff}]}
        max_ctx = _safe_int(args.get("max_context"), 2, lo=0, hi=50)
        sliced, orig_t, opt_t = DiffSlicer.slice_diff(raw_diff, max_context_lines=max_ctx)
        saved = max(0, orig_t - opt_t)
        if saved > 0:
            tracker.record_event(
                session_id=_mcp_session_id(),
                event_type="DIFF_SLICER",
                original_tokens=orig_t,
                optimized_tokens=opt_t,
                details={"saved_tokens": saved, "max_context": max_ctx}
            )
        return {"content": [{"type": "text", "text": sliced}]}

    elif name == "get_token_savings_metrics":
        dashboard = Dashboard(tracker)
        return {"content": [{"type": "text", "text": dashboard.render_markdown()}]}

    elif name == "get_architecture_graph":
        target_abs, err = _resolve_project_root(args)
        if err or not target_abs:
            return {
                "isError": True,
                "content": [{"type": "text", "text": err or "Error: Invalid project_path."}]
            }
        if is_quota_exceeded:
            LicenseManager.should_emit_quota_banner(tracker, session_id=_mcp_session_id(), every_n_turns=5)
            return {"content": [{"type": "text", "text": json.dumps({
                "quota_exceeded": True,
                "monthly_saved": quota.get("monthly_saved"),
                "monthly_limit": quota.get("monthly_limit"),
                "fallback": "Use free list_dir / scoped_search or activate a Solo/Team license: tokensafe activate <key>",
                "warning": quota.get("warning"),
            }, indent=2, ensure_ascii=False)}]}
        graph = GraphEngine.get_instance(target_abs)
        # Cap estimated crawl tokens to max 50,000 to prevent runaway spikes on large/root dirs
        capped_files = min(80, len(graph.file_symbols))
        est_crawl_tokens = min(50000, max(5000, capped_files * 600))
        topo_tokens = 250

        # Shared Team Symbol Index: reuse a teammate's graph for the exact same
        # repo state (key = rel paths + sizes, so clones with identical trees hit).
        fp = _repo_fingerprint(graph)
        team_cached = None
        try:
            raw_cached = TeamCache.get("arch_index", fp)
            if raw_cached:
                team_cached = json.loads(raw_cached)
        except Exception:
            team_cached = None
        if isinstance(team_cached, dict) and team_cached.get("god_nodes") is not None:
            payload = dict(team_cached)
            payload["source"] = "team_symbol_index"
            hit_text = json.dumps(payload, indent=2, ensure_ascii=False)
            tracker.record_event(
                session_id=_mcp_session_id(),
                event_type="TEAM_CACHE_HIT",
                original_tokens=est_crawl_tokens,
                optimized_tokens=topo_tokens,
                details={"source": "team_symbol_index", "indexed_files": len(graph.file_symbols)}
            )
            return {"content": [{"type": "text", "text": hit_text}]}

        god_nodes = graph.get_god_nodes(7)
        banner = graph.format_topology_banner()
        payload = {
            "project_root": graph.root_dir,
            "banner": banner,
            "total_files": len(graph.file_symbols),
            "total_symbols": len(graph.symbols),
            "god_nodes": god_nodes
        }
        tracker.record_event(
            session_id=_mcp_session_id(),
            event_type="GRAPH_TOPOLOGY",
            original_tokens=est_crawl_tokens,
            optimized_tokens=topo_tokens,
            details={"hubs_count": len(god_nodes), "indexed_files": len(graph.file_symbols)}
        )
        try:
            TeamCache.put("arch_index", fp, json.dumps(payload, ensure_ascii=False, default=str))
        except Exception:
            pass
        return {"content": [{"type": "text", "text": json.dumps(payload, indent=2, ensure_ascii=False)}]}

    elif name == "get_api_surface":
        target_abs, err = _resolve_project_root(args)
        if err or not target_abs:
            return {
                "isError": True,
                "content": [{"type": "text", "text": err or "Error: Invalid project_path."}]
            }
        if is_quota_exceeded:
            LicenseManager.should_emit_quota_banner(tracker, session_id=_mcp_session_id(), every_n_turns=5)
            return {"content": [{"type": "text", "text": f"{quota.get('warning', '')}\n\nAPI surface map generation is paused over the 500k Community limit. Use scoped_search or list_dir, or activate a subscription at https://tokensafe.dev/#pricing."}]}
        graph = GraphEngine.get_instance(target_abs)
        # Cap estimated crawl tokens to max 60,000
        capped_files = min(100, len(graph.file_symbols))
        est_orig_tokens = min(60000, max(10000, capped_files * 600))

        # Shared Team Symbol Index surface variant (separate key from the graph payload)
        fp = _repo_fingerprint(graph) + ":surface"
        team_cached_md = None
        try:
            team_cached_md = TeamCache.get("arch_index", fp)
        except Exception:
            team_cached_md = None
        if team_cached_md:
            tracker.record_event(
                session_id=_mcp_session_id(),
                event_type="TEAM_CACHE_HIT",
                original_tokens=est_orig_tokens,
                optimized_tokens=count_tokens(team_cached_md),
                details={"source": "team_symbol_index", "type": "API_SURFACE"}
            )
            return {"content": [{"type": "text", "text": team_cached_md}]}

        surface_md = graph.generate_api_surface()
        surface_tokens = count_tokens(surface_md)
        tracker.record_event(
            session_id=_mcp_session_id(),
            event_type="GRAPH_TOPOLOGY",
            original_tokens=est_orig_tokens,
            optimized_tokens=surface_tokens,
            details={"type": "API_SURFACE", "symbols_count": len(graph.symbols)}
        )
        try:
            TeamCache.put("arch_index", fp, surface_md[:200000])
        except Exception:
            pass
        return {"content": [{"type": "text", "text": surface_md}]}

    elif name == "list_dir":
        path = args.get("path") or args.get("project_path") or args.get("directory") or args.get("root_dir") or ""
        if not path:
            return _fallback_error("list_dir", "missing required 'path' argument")

        # Resolve path relative to active workspace, cwd, or absolute
        active_ws = get_active_workspace()
        if not os.path.isabs(path):
            if active_ws and os.path.isdir(os.path.join(active_ws, path)):
                abs_path = os.path.abspath(os.path.join(active_ws, path))
            else:
                abs_path = os.path.abspath(path)
        else:
            abs_path = os.path.abspath(path)

        if not os.path.isdir(abs_path):
            return _fallback_error("list_dir", f"directory not found: {abs_path}")
        if is_plugin_or_system_dir(abs_path):
            return _fallback_error("list_dir", f"refusing to inspect system root or plugin dir '{path}'")
        system_roots = {"/", "/Users", "/home", "/root", os.path.expanduser("~")}
        depth = _safe_int(args.get("depth"), 2, lo=1, hi=4)
        if abs_path in system_roots:
            # Do not walk entire home dir or filesystem root deeply
            depth = 1
        max_entries = _safe_int(args.get("max_entries"), 80, lo=10, hi=150)

        tree_output = build_dir_tree(abs_path, max_depth=depth, max_entries=max_entries)
        tree_tokens = count_tokens(tree_output)
        # Estimate equivalent tokens from multiple verbose ls -la calls (at least ~800 tokens)
        est_orig_tokens = max(800, tree_tokens * 6)
        tracker.record_event(
            session_id=_mcp_session_id(),
            event_type="DIR_LIST",
            original_tokens=est_orig_tokens,
            optimized_tokens=tree_tokens,
            details={"path": os.path.basename(abs_path) or abs_path, "depth": depth, "tokens_saved": est_orig_tokens - tree_tokens}
        )
        return {"content": [{"type": "text", "text": tree_output}]}

    elif name == "scoped_search":
        if args.get("queries"):
            from src.batch_search import execute_batch_search
            cwd = args.get("cwd") or get_active_workspace()
            res = execute_batch_search(queries=args.get("queries"), cwd=cwd)
            return {"content": [{"type": "text", "text": res.get("summary_md", "")}]}

        query = args.get("query") or args.get("q") or args.get("pattern") or args.get("text") or ""
        if not query:
            return _fallback_error("scoped_search", "missing required 'query' argument")

        raw_scope = str(args.get("scope") or "all").strip()
        raw_path = args.get("project_path") or args.get("path") or args.get("root_dir") or args.get("directory")
        active_ws = get_active_workspace()

        # 1. Resolve base directory safely without scanning plugin or system roots
        if raw_path:
            if not os.path.isabs(raw_path):
                if active_ws and os.path.isdir(os.path.join(active_ws, raw_path)):
                    target_path = os.path.abspath(os.path.join(active_ws, raw_path))
                else:
                    target_path = os.path.abspath(raw_path)
            else:
                target_path = os.path.abspath(raw_path)

            if os.path.isfile(target_path):
                target_path = os.path.dirname(target_path)
            if is_plugin_or_system_dir(target_path):
                return _fallback_error("scoped_search", f"refusing to search system root or plugin dir '{raw_path}'")
        else:
            resolved_path, err = _resolve_project_root(args)
            if err or not resolved_path:
                return _fallback_error("scoped_search", err or "unspecified project_path")
            target_path = resolved_path

        # 2. Smart scope routing: handle agents passing subdirectories or extensions in `scope`
        scope = raw_scope
        if raw_scope and raw_scope.lower() not in ("code", "config", "all"):
            candidate_subdir = os.path.normpath(os.path.join(target_path, raw_scope))
            if os.path.isdir(candidate_subdir):
                target_path = candidate_subdir
                scope = "all"
            elif "/" in raw_scope or "\\" in raw_scope:
                if os.path.exists(candidate_subdir):
                    target_path = candidate_subdir
                    scope = "all"

        max_res = _safe_int(args.get("max_results"), 25, lo=1, hi=100)
        c_sens = bool(args.get("case_sensitive", False))
        raw_regex = args.get("use_regex")
        u_regex = None if raw_regex is None else bool(raw_regex)

        # Quota degradation: when monthly quota is reached, softly degrade scoped_search
        # to top 3 matches with an upgrade header rather than returning an empty block,
        # preventing the agent from deadlocking or panicking into blocked raw terminal grep.
        if is_quota_exceeded:
            max_res = min(max_res, 3)

        res = ScopedSearch.search(
            query=query,
            project_path=target_path,
            scope=scope,
            max_results=max_res,
            case_sensitive=c_sens,
            use_regex=u_regex
        )
        md_text = res["markdown"]
        if not res.get("success", True):
            return _fallback_error("scoped_search", md_text)

        if is_quota_exceeded:
            banner = _quota_banner_if_due(quota, tracker, _mcp_session_id())
            current_tier = str(quota.get("tier") or "guest").lower()
            limit_val = int(quota.get("monthly_limit") or (50000 if current_tier == "guest" else 500000))
            if current_tier == "guest":
                cta_note = "Run 'tokensafe login' in your terminal to unlock 500,000 free tokens/month!"
            else:
                cta_note = "Upgrade to Solo for unlimited optimization: https://tokensafe.dev/pricing"
            degraded_header = (
                f"// [TokenSafe Quota Notice: {current_tier.upper()} limit of {limit_val:,} tokens reached. "
                f"Showing top {max_res} matches in degraded mode.]\n"
                f"// [{cta_note}]\n\n"
            )
            return {"content": [{"type": "text", "text": banner + degraded_header + md_text}]}

        res_tokens = count_tokens(md_text)
        est_orig = max(1500, res_tokens * 8)
        tracker.record_event(
            session_id=_mcp_session_id(),
            event_type="SCOPED_SEARCH",
            original_tokens=est_orig,
            optimized_tokens=res_tokens,
            details={"query": query, "scope": scope, "matches": res["total_matches"]}
        )
        return {"content": [{"type": "text", "text": md_text}]}

    elif name == "expand_ref":
        raw_hash = args.get("hash") or args.get("ref") or args.get("id") or ""
        if not raw_hash:
            return _fallback_error("expand_ref", "missing required 'hash' argument")
        try:
            from src.stash import retrieve_block
            content = retrieve_block(str(raw_hash))
            if content is None:
                return {
                    "isError": True,
                    "content": [{"type": "text", "text": f"Error: Stashed reference '{raw_hash}' not found or expired (retention is 7 days)."}]
                }
            return {"content": [{"type": "text", "text": content}]}
        except Exception as e:
            return _fallback_error("expand_ref", f"cannot retrieve stashed ref: {e}")

    elif name == "read_web_page":
        target_url = str(args.get("url") or args.get("link") or "").strip()
        force_refresh = bool(args.get("force_refresh", False))
        if not target_url:
            return _fallback_error("read_web_page", "missing required 'url' argument")
        try:
            from src.web_reader import WebReader
            content, ok, err = WebReader.fetch_url(target_url, force_refresh=force_refresh)
            if not ok:
                return _fallback_error("read_web_page", err or "failed to fetch web page")
            orig_t = max(1000, len(content) // 2)
            opt_t = count_tokens(content)
            tier = getattr(WebReader, "last_tier", "live")
            tracker.record_event(
                session_id=_mcp_session_id(),
                event_type="WEB_READ",
                original_tokens=orig_t,
                optimized_tokens=opt_t,
                details={"url": target_url[:80], "tier": tier, "saved": max(0, orig_t - opt_t)}
            )
            return {"content": [{"type": "text", "text": content}]}
        except Exception as e:
            return _fallback_error("read_web_page", f"error reading web page: {e}")

    elif name == "query_dependency":
        pkg = str(args.get("package_name") or args.get("package") or "").strip()
        lock_p = str(args.get("lockfile_path") or args.get("path") or "").strip() or None
        if not pkg:
            return _fallback_error("query_dependency", "missing required 'package_name' argument")
        try:
            from src.format_parsers.lockfiles import LockfileReducer
            res = LockfileReducer.query_dependency(lock_p, pkg)
            lock_bytes = res.pop("_lockfile_bytes", 0)
            json_text = json.dumps(res, indent=2, ensure_ascii=False)
            opt_tokens = count_tokens(json_text)
            if res.get("found"):
                orig_tokens = max(1000, lock_bytes // 4) if lock_bytes else max(opt_tokens, 1000)
            else:
                orig_tokens = opt_tokens
            tracker.record_event(
                session_id=_mcp_session_id(),
                event_type="LOCKFILE_QUERY",
                original_tokens=orig_tokens,
                optimized_tokens=opt_tokens,
                details={"package": pkg, "source": res.get("source", "parsed"), "found": res.get("found", False)}
            )
            return {"content": [{"type": "text", "text": json_text}]}
        except Exception as e:
            return _fallback_error("query_dependency", f"error resolving dependency: {e}")

    elif name == "slice_external_type":
        pkg = str(args.get("package_name") or args.get("package") or "").strip()
        sym = str(args.get("symbol") or args.get("type") or args.get("type_name") or "").strip()
        proj_root = str(args.get("project_root") or "").strip() or None
        if not pkg or not sym:
            return _fallback_error("slice_external_type", "missing required 'package_name' or 'symbol' argument")
        try:
            from src.type_slicer import TypeSlicer
            res = TypeSlicer.slice_package_type(pkg, sym, project_root=proj_root)
            dts_bytes = res.pop("_dts_bytes", 0)
            json_text = json.dumps(res, indent=2, ensure_ascii=False)
            opt_tokens = count_tokens(json_text)
            if res.get("found"):
                orig_tokens = max(opt_tokens, dts_bytes // 4) if dts_bytes else max(opt_tokens, 500)
            else:
                orig_tokens = opt_tokens
            tracker.record_event(
                session_id=_mcp_session_id(),
                event_type="TYPE_SLICE",
                original_tokens=orig_tokens,
                optimized_tokens=opt_tokens,
                details={"package": pkg, "symbol": sym, "source": res.get("source", "parsed"), "found": res.get("found", False)}
            )
            return {"content": [{"type": "text", "text": json_text}]}
        except Exception as e:
            return _fallback_error("slice_external_type", f"error slicing external type: {e}")

    elif name == "optimize_image":
        img_path = str(args.get("image_path") or args.get("path") or "").strip()
        if not img_path:
            return _fallback_error("optimize_image", "missing required 'image_path' argument")
        try:
            max_edge = _safe_int(args.get("max_edge"), 1024, lo=256, hi=4096)
            from src.image_downscale import ImageDownscaler
            res = ImageDownscaler.optimize(img_path, max_edge=max_edge)
            if not res.get("success"):
                return _fallback_error("optimize_image", res.get("error", "optimization failed"))
            if res.get("was_downscaled"):
                tracker.record_event(
                    session_id=_mcp_session_id(),
                    event_type="IMAGE_OPTIMIZATION",
                    original_tokens=res.get("orig_tokens", 1000),
                    optimized_tokens=res.get("new_tokens", 500),
                    details={"path": os.path.basename(img_path), "saved": res.get("saved_tokens", 0)}
                )
            return {"content": [{"type": "text", "text": json.dumps(res, indent=2, ensure_ascii=False)}]}
        except Exception as e:
            return _fallback_error("optimize_image", f"error optimizing image: {e}")

    elif name == "prune_superseded":
        t_path = str(args.get("transcript_path") or "").strip()
        if not t_path:
            return _fallback_error("prune_superseded", "missing required 'transcript_path' argument")
        try:
            from src.superseded import SupersededPruner
            res = SupersededPruner.prune_transcript(t_path)
            if not res.get("success"):
                return _fallback_error("prune_superseded", res.get("error", "pruning failed"))
            if res.get("saved_tokens", 0) > 0:
                tracker.record_event(
                    session_id=_mcp_session_id(),
                    event_type="SUPERSEDED_PRUNE",
                    original_tokens=res.get("orig_tokens", 0),
                    optimized_tokens=res.get("pruned_tokens", 0),
                    details={"pruned_count": res.get("pruned_count", 0), "saved": res.get("saved_tokens", 0)}
                )
            return {"content": [{"type": "text", "text": json.dumps(res, indent=2, ensure_ascii=False)}]}
        except Exception as e:
            return _fallback_error("prune_superseded", f"error pruning transcript: {e}")

    elif name == "get_mistakes":
        proj_root = str(args.get("project_root") or "").strip() or None
        if args.get("clear"):
            from src.mistakes import MistakesLedger
            cleared = MistakesLedger.clear_mistakes(project_root=proj_root)
            return {"content": [{"type": "text", "text": json.dumps({"cleared": cleared}, indent=2)}]}
        try:
            from src.mistakes import MistakesLedger
            entries = MistakesLedger.list_mistakes(project_root=proj_root)
            team_entries = MistakesLedger.get_team_mistakes()
            guard = MistakesLedger.get_guard_prompt(project_root=proj_root)
            return {"content": [{"type": "text", "text": json.dumps({
                "count": len(entries),
                "entries": entries,
                "team_entries": team_entries,
                "active_guard": guard
            }, indent=2, ensure_ascii=False)}]}
        except Exception as e:
            return _fallback_error("get_mistakes", f"error reading mistakes ledger: {e}")

    elif name == "oumv_status":
        try:
            from src.oumv_gateway import OUMVGateway
            ws = str(args.get("workspace_dir") or "").strip() or None
            res = OUMVGateway.status(workspace_dir=ws)
            return {"content": [{"type": "text", "text": json.dumps(res, indent=2, ensure_ascii=False)}]}
        except Exception as e:
            return _fallback_error("oumv_status", f"failed to check OUMV status: {e}")

    elif name == "oumv_observe":
        try:
            from src.oumv_gateway import OUMVGateway
            lvl = str(args.get("level") or "L0").strip()
            tgt = str(args.get("target") or "").strip() or None
            rt = str(args.get("runtime") or "auto").strip()
            res = OUMVGateway.observe(level=lvl, target=tgt, runtime=rt)
            return {"content": [{"type": "text", "text": json.dumps(res, indent=2, ensure_ascii=False)}]}
        except Exception as e:
            return _fallback_error("oumv_observe", f"failed to observe OUMV runtime: {e}")

    elif name == "oumv_understand":
        try:
            from src.oumv_gateway import OUMVGateway
            qry = str(args.get("query") or "causality").strip()
            tgt = str(args.get("target") or "").strip() or None
            rt = str(args.get("runtime") or "auto").strip()
            res = OUMVGateway.understand(query=qry, target=tgt, runtime=rt)
            return {"content": [{"type": "text", "text": json.dumps(res, indent=2, ensure_ascii=False)}]}
        except Exception as e:
            return _fallback_error("oumv_understand", f"failed to query OUMV understanding: {e}")

    elif name == "oumv_mutate":
        try:
            from src.oumv_gateway import OUMVGateway
            p = str(args.get("path") or "").strip()
            sym = str(args.get("symbol") or "").strip() or None
            rep = args.get("replacement")
            ev = str(args.get("expectedVersion") or "").strip() or None
            rt = str(args.get("runtime") or "auto").strip()
            res = OUMVGateway.mutate(path=p, symbol=sym, replacement=rep, expected_version=ev, runtime=rt)
            return {"content": [{"type": "text", "text": json.dumps(res, indent=2, ensure_ascii=False)}]}
        except Exception as e:
            return _fallback_error("oumv_mutate", f"failed to apply OUMV mutation: {e}")

    elif name == "oumv_verify":
        try:
            from src.oumv_gateway import OUMVGateway
            tgt = str(args.get("target") or "").strip()
            act = str(args.get("action") or "invoke").strip()
            payload = args.get("payload") if isinstance(args.get("payload"), dict) else None
            rt = str(args.get("runtime") or "auto").strip()
            res = OUMVGateway.verify(target=tgt, action=act, payload=payload, runtime=rt)
            return {"content": [{"type": "text", "text": json.dumps(res, indent=2, ensure_ascii=False)}]}
        except Exception as e:
            return _fallback_error("oumv_verify", f"failed to execute OUMV verification: {e}")

    elif name == "apply_edits":
        try:
            from src.batch_editor import execute_batch_edits
            edits = args.get("edits")
            if not edits:
                return _fallback_error("apply_edits", "missing required 'edits' list")
            best_effort = bool(args.get("best_effort", False))
            dry_run = bool(args.get("dry_run", False))
            verify = args.get("verify")
            verify_timeout = int(args.get("verify_timeout_sec") or 30)
            cwd = args.get("cwd") or get_active_workspace()

            res = execute_batch_edits(
                edits=edits,
                best_effort=best_effort,
                dry_run=dry_run,
                verify=verify,
                verify_timeout_sec=verify_timeout,
                cwd=cwd
            )
            lines = []
            if res.get("success"):
                action_desc = "Validated" if dry_run else "Applied"
                lines.append(f"✔ {action_desc} {res.get('applied_count', 0)} file(s) successfully.")
                for f in res.get("applied_files", []):
                    h_cnt = f.get("hunks", 0)
                    is_cr = f.get("is_create", False)
                    label = "created" if is_cr else f"{h_cnt} hunk(s)"
                    lines.append(f"  - `{f.get('path')}` ({label})")
            else:
                lines.append(f"✖ Batch edits failed ({res.get('failed_count', 0)} error(s)).")
                for f in res.get("failed_files", []):
                    lines.append(f"  - `{f.get('path')}`: {f.get('error')}")

            if "verify" in res:
                v = res["verify"]
                status_sym = "✔ PASS" if v.get("passed") else "✖ FAIL"
                lines.append(f"\nInline Verify ({v.get('command')}): {status_sym} (exit {v.get('exit_code')}, {v.get('duration_seconds', 0)}s)")
                if v.get("output"):
                    lines.append("```text\n" + v["output"] + "\n```")
                elif v.get("error"):
                    lines.append(f"Error: {v['error']}")

            return {"content": [{"type": "text", "text": "\n".join(lines)}]}
        except Exception as e:
            return _fallback_error("apply_edits", f"failed to execute batch edits: {e}")

    elif name == "batch_search":
        try:
            from src.batch_search import execute_batch_search
            queries = args.get("queries")
            if isinstance(queries, dict):
                queries = [queries]
            elif not queries and (args.get("query") or args.get("pattern")):
                queries = [{"pattern": args.get("query") or args.get("pattern"), "type": args.get("type", "text")}]
            if not queries:
                return _fallback_error("batch_search", "missing required 'queries' list")

            cwd = args.get("cwd") or get_active_workspace()
            res = execute_batch_search(queries=queries, cwd=cwd)
            return {"content": [{"type": "text", "text": res.get("summary_md", "")}]}
        except Exception as e:
            return _fallback_error("batch_search", f"failed to execute batch search: {e}")

    return _fallback_error(name, f"unknown tool '{name}'")


def _redact_result_secrets(result):
    """Egress guard at the MCP boundary: strips recognized secret patterns from
    tool results BEFORE they enter any agent's context. Covers every MCP-connected
    agent (CC, Cursor, Cline, Windsurf, Copilot, Kimi, Codex, Antigravity, ZCode)
    even when the proxy is not on the path. Same redactor the proxy pipeline uses.
    Fail-open: any internal problem returns the result untouched."""
    try:
        if result is None:
            return result
        from src.secret_redactor import SecretRedactor
    except Exception:
        return result
    total = 0

    def _walk(node):
        nonlocal total
        if isinstance(node, str):
            red, n, _ = SecretRedactor.redact_text(node)
            if n:
                total += n
            return red
        if isinstance(node, dict):
            return {k: _walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_walk(v) for v in node]
        return node

    try:
        redacted = _walk(result)
    except Exception:
        return result
    if total:
        try:
            sys.stderr.write(f"[TokenSafe] MCP egress redaction: {total} secret occurrence(s) stripped from tool result\n")
        except Exception:
            pass
    return redacted


def _cap_mcp_output_size(result: Any, tool_name: str = "tool", max_bytes: int = 8800) -> Any:
    """Zero-Spill Guard: guarantees no MCP tool response exceeds 8800 UTF-8 bytes.
    Prevents Antigravity from spilling tool output to disk (output.txt) which forces
    an extra LLM roundtrip turn to read the spill file. Full content is saved to
    reversible stash and referenced with an 8-char hash anchor."""
    if not isinstance(result, dict):
        return result
    content = result.get("content")
    if not isinstance(content, list):
        return result

    modified = False
    new_content = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text", "")
            raw_bytes = text.encode("utf-8")
            if len(raw_bytes) > max_bytes:
                try:
                    from src.stash import stash_block
                    from src.format_parsers.ansi import strip_ansi
                    clean_text = strip_ansi(text)
                    hash8, marker = stash_block(clean_text, source=f"mcp:{tool_name}")
                    lines = clean_text.splitlines()
                    head = lines[:20]
                    tail = lines[-15:]
                    omitted = len(lines) - (len(head) + len(tail))
                    folded_msg = f"\n... [{omitted} lines / {len(clean_text)} chars folded by TokenSafe Zero-Spill Guard] ...\n"
                    tail_str = "\n".join(tail)
                    footer = f"\n{marker}"

                    budget = max_bytes - len(folded_msg.encode("utf-8")) - len(footer.encode("utf-8")) - len(tail_str.encode("utf-8")) - 50
                    head_str = ""
                    for line in head:
                        if len((head_str + line + "\n").encode("utf-8")) <= budget:
                            head_str += line + "\n"
                        else:
                            break

                    folded_text = f"{head_str.rstrip()}{folded_msg}{tail_str}{footer}"
                    if len(folded_text.encode("utf-8")) > max_bytes:
                        allowed_head = max_bytes - len(footer.encode("utf-8")) - 100
                        folded_text = clean_text.encode("utf-8")[:allowed_head].decode("utf-8", errors="ignore") + f"\n... [folded]\n{footer}"

                    new_block = dict(block)
                    new_block["text"] = folded_text
                    new_content.append(new_block)
                    modified = True
                    continue
                except Exception:
                    pass
        new_content.append(block)

    if modified:
        new_res = dict(result)
        new_res["content"] = new_content
        return new_res
    return result


def _execute_with_timeout(tool_name: str, tool_args: dict, timeout: float = MCP_TOOL_TIMEOUT) -> dict:
    """Runs handle_tool_call inside a daemon thread with hard timeout so stdio MCP never freezes."""
    box = {"result": None, "error": None}

    def _runner():
        try:
            box["result"] = handle_tool_call(tool_name, tool_args)
        except Exception as exc:
            box["error"] = f"{type(exc).__name__}: {str(exc)}"

    worker = threading.Thread(target=_runner, daemon=True)
    worker.start()
    worker.join(timeout=timeout)

    if worker.is_alive():
        return _fallback_error(tool_name, f"execution exceeded {timeout:.1f}s timeout")
    if box["error"] is not None:
        return _fallback_error(tool_name, box["error"])
    if box["result"] is None:
        return _fallback_error(tool_name, "empty result returned")
    return box["result"]


def _get_watched_mtimes() -> Tuple[Optional[float], ...]:
    """Returns mtimes for core server/package files and the update reload marker."""
    try:
        from src.paths import resolve_data_dir
        reload_markers = [os.path.join(resolve_data_dir(), ".mcp_reload")]
    except Exception:
        reload_markers = [os.path.join(BASE_DIR, "data", ".mcp_reload")]
    watched_paths = (
        os.path.abspath(__file__),
        os.path.join(BASE_DIR, "package.json"),
        *reload_markers,
    )
    mtimes: List[Optional[float]] = []
    for p in watched_paths:
        try:
            mtimes.append(os.path.getmtime(p))
        except OSError:
            mtimes.append(None)
    return tuple(mtimes)


def run_mcp_server():
    """Runs stdio JSON-RPC 2.0 loop with per-call timeout, resilient recovery & auto-reload on upgrade."""
    global _MCP_CLIENT_NAME, _TOOLS_LISTED, _EPOCH_GEN
    _MCP_CLIENT_NAME = ""
    _TOOLS_LISTED = False
    _EPOCH_GEN = 0
    _IN_PROCESS_DELIVERED.clear()
    initial_mtimes = _get_watched_mtimes()
    auto_reload = os.environ.get("TOKENSAFE_MCP_AUTORELOAD", "1").strip().lower() not in ("0", "false", "no")

    while True:
        try:
            line = sys.stdin.readline()
        except Exception:
            break
        if not line:
            break
        line = line.strip()
        if not line:
            continue

        try:
            req = json.loads(line)
        except Exception:
            continue
        if not isinstance(req, dict):
            continue

        msg_id = req.get("id")
        method = req.get("method")
        raw_params = req.get("params")
        params = raw_params if isinstance(raw_params, dict) else {}

        # Notifications (no id, or notifications/*) must never receive JSON-RPC responses
        if msg_id is None or (isinstance(method, str) and method.startswith("notifications/")):
            continue

        response = {"jsonrpc": "2.0", "id": msg_id}

        if method == "initialize":
            try:
                client_info = params.get("clientInfo") or {}
                if isinstance(client_info, dict):
                    _set_mcp_client(str(client_info.get("name") or ""))
            except Exception:
                pass
            try:
                from src.telemetry_sync import TelemetrySync
                TelemetrySync.sync_background()
            except Exception:
                pass
            req_version = str(params.get("protocolVersion") or "")
            supported_versions = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
            negotiated_version = req_version if req_version in supported_versions else "2025-06-18"
            response["result"] = {
                "protocolVersion": negotiated_version,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO
            }
        elif method == "tools/list":
            _TOOLS_LISTED = True
            response["result"] = {"tools": TOOLS}
        elif method == "tools/call":
            tool_name = str(params.get("name") or "")
            raw_tool_args = params.get("arguments")
            tool_args = raw_tool_args if isinstance(raw_tool_args, dict) else {}
            if not tool_args and raw_tool_args:
                # Self-healing: hosts on small/local models intermittently send
                # arguments as a malformed JSON string — repair before failing.
                from src.json_repair import parse_tool_arguments
                tool_args = parse_tool_arguments(raw_tool_args) or {}
            try:
                raw_res = _execute_with_timeout(tool_name, tool_args)
                redacted = _redact_result_secrets(raw_res)
                response["result"] = _cap_mcp_output_size(redacted, tool_name=tool_name)
            except Exception as e:
                err_res = _fallback_error(tool_name, f"{type(e).__name__}: {str(e)}")
                redacted = _redact_result_secrets(err_res)
                response["result"] = _cap_mcp_output_size(redacted, tool_name=tool_name)
            finally:
                try:
                    from src.telemetry_sync import TelemetrySync
                    TelemetrySync.sync_background(throttle_seconds=5.0)
                except Exception:
                    pass
        else:
            response["error"] = {"code": -32601, "message": f"Method not found: {method}"}

        try:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
        except Exception:
            break

        # Safeguard: if TokenSafe files were upgraded on disk while running, exit cleanly
        # immediately after sending the response so the host IDE respawns the new version.
        if auto_reload and method == "tools/call":
            if _get_watched_mtimes() != initial_mtimes:
                break

def main():
    run_mcp_server()

if __name__ == "__main__":
    main()
