"""Central data-directory resolution for TokenSafe.

Priority order:
1. ``TOKENSAFE_DATA_DIR`` — explicit override (tests, Docker ``/app/data``).
   Honored verbatim, no migration: the caller controls the environment.
2. ``~/.tokensafe/data`` — the canonical, user-level directory shared by ALL
   agents (Antigravity, Claude Code, Cursor, Codex, ...). Metrics, caches and
   telemetry state live in one SQLite/JSON set regardless of how TokenSafe was
   installed (git clone, npm, pip) or which agent runs the hook.

Migration (only when resolving the canonical dir): well-known state files are
carried over copy-if-absent from the legacy ``<repo>/data`` directory first
(the historical authoritative location), then from ``PLUGIN_DATA`` (the
per-plugin dir Antigravity exposes, which earlier builds migrated into).
``PLUGIN_DATA`` is a migration source only — it never wins resolution, so the
active directory is identical for every agent on the machine.
"""

import os
import shutil
import sqlite3
from typing import Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Well-known state files carried over into the canonical directory.
_MIGRATION_FILES = (
    "metrics.db",
    "version_cache.json",
    "sync_state.json",
    "license.json",
    "config.json",
)


def user_data_dir() -> str:
    """The canonical per-user data directory shared across all agents."""
    return os.path.join(os.path.expanduser("~"), ".tokensafe", "data")


def legacy_data_dir() -> str:
    """The historical data directory inside the plugin repo (migration source)."""
    return os.path.join(PROJECT_ROOT, "data")


def plugin_data_dir() -> str:
    """Antigravity's per-plugin persistent directory (migration source)."""
    return os.environ.get("PLUGIN_DATA") or ""


def resolve_data_dir() -> str:
    """Returns the active data directory according to the priority order above."""
    explicit = os.environ.get("TOKENSAFE_DATA_DIR")
    if explicit:
        return explicit
    canonical = user_data_dir()
    _migrate_into(canonical)
    return canonical


def _migrate_into(target: str) -> None:
    """One-time copy-if-absent carry-over from legacy and plugin directories.

    Uses a sentinel marker (.migrated) once checked, and SQLite's online
    backup API for metrics.db so active WAL transactions are never truncated."""
    sentinel = os.path.join(target, ".migrated")
    if os.path.isfile(sentinel):
        return
    sources = [legacy_data_dir()]
    if plugin_data_dir():
        sources.append(plugin_data_dir())
    sources = [s for s in sources if os.path.isdir(s)]
    if not sources:
        try:
            os.makedirs(target, exist_ok=True)
            with open(sentinel, "w", encoding="utf-8") as f:
                f.write("1")
        except Exception:
            pass
        return
    try:
        os.makedirs(target, exist_ok=True)
        for source in sources:
            if os.path.abspath(source) == os.path.abspath(target):
                continue
            for name in _MIGRATION_FILES:
                src_file = os.path.join(source, name)
                dst_file = os.path.join(target, name)
                if os.path.isfile(src_file) and not os.path.exists(dst_file):
                    tmp_file = f"{dst_file}.migrating.{os.getpid()}"
                    if name == "metrics.db":
                        try:
                            src_conn = sqlite3.connect(src_file, timeout=5.0)
                            dst_conn = sqlite3.connect(tmp_file, timeout=5.0)
                            try:
                                src_conn.backup(dst_conn)
                            finally:
                                dst_conn.close()
                                src_conn.close()
                            os.replace(tmp_file, dst_file)
                            continue
                        except Exception:
                            if os.path.exists(tmp_file):
                                try:
                                    os.unlink(tmp_file)
                                except OSError:
                                    pass
                    shutil.copy2(src_file, tmp_file)
                    os.replace(tmp_file, dst_file)
        with open(sentinel, "w", encoding="utf-8") as f:
            f.write("1")
    except Exception:
        pass


def is_plugin_or_system_dir(path: str) -> bool:
    """Returns True if path is a system root or inside an agent plugin directory."""
    if not path:
        return True
    abs_p = os.path.abspath(path)
    system_roots = {"/", "/Users", "/home", "/root", os.path.expanduser("~")}
    if abs_p in system_roots:
        return True
    lower = abs_p.lower()
    if any(k in lower for k in (".gemini/config/plugins", "/.tokensafe", "plugins/tokensafe", ".cursor/extensions")):
        return True
    return False


def save_active_workspace(path: str) -> None:
    """Records the active user workspace directory observed by lifecycle hooks."""
    if not path or not os.path.isabs(path) or not os.path.isdir(path):
        return
    if is_plugin_or_system_dir(path):
        return
    try:
        import threading
        data_dir = resolve_data_dir()
        os.makedirs(data_dir, exist_ok=True)
        marker = os.path.join(data_dir, ".active_workspace")
        tmp_marker = f"{marker}.tmp.{os.getpid()}.{threading.get_ident()}"
        with open(tmp_marker, "w", encoding="utf-8") as f:
            f.write(os.path.abspath(path))
        os.replace(tmp_marker, marker)
    except Exception:
        pass


def get_active_workspace() -> Optional[str]:
    """Retrieves the last known active user workspace directory."""
    try:
        data_dir = resolve_data_dir()
        marker = os.path.join(data_dir, ".active_workspace")
        if os.path.isfile(marker):
            with open(marker, "r", encoding="utf-8") as f:
                path = f.read().strip()
                if path and os.path.isabs(path) and os.path.isdir(path):
                    if not is_plugin_or_system_dir(path):
                        return path
    except Exception:
        pass
    return None


def ensure_tokensafe_gitignore(project_root: Optional[str]) -> bool:
    """
    Ensures .tokensafe/ and local caches never leak into git.
    1. If <project_root>/.tokensafe exists, drops an internal .gitignore (*\n!.gitignore\n).
    2. If <project_root>/.gitignore exists, ensures .tokensafe/ is ignored.
    """
    if not project_root or not os.path.isdir(project_root):
        return False

    updated = False
    proj_tokensafe = os.path.join(project_root, ".tokensafe")
    if os.path.isdir(proj_tokensafe):
        inner_ignore = os.path.join(proj_tokensafe, ".gitignore")
        if not os.path.isfile(inner_ignore):
            try:
                with open(inner_ignore, "w", encoding="utf-8") as f:
                    f.write("# TokenSafe local runtime artifacts & caches\n*\n!.gitignore\n")
                updated = True
            except Exception:
                pass

    root_gitignore = os.path.join(project_root, ".gitignore")
    if os.path.isfile(root_gitignore):
        try:
            with open(root_gitignore, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            lines = [ln.strip() for ln in content.splitlines()]
            has_entry = any(
                ln in (".tokensafe", ".tokensafe/", "/.tokensafe", "/.tokensafe/")
                for ln in lines
            )
            if not has_entry:
                with open(root_gitignore, "a", encoding="utf-8") as f:
                    if content and not content.endswith("\n"):
                        f.write("\n")
                    f.write("\n# TokenSafe local cache & runtime metrics\n.tokensafe/\n")
                updated = True
        except Exception:
            pass

    return updated


_PROJECT_MARKER_FILES = (
    "package.json", "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt",
    "go.mod", "Cargo.toml", "pom.xml", "build.gradle", "build.gradle.kts",
    "composer.json", "Gemfile", "mix.exs", "Makefile", "CMakeLists.txt",
)


def is_project_root(path: str) -> bool:
    """True when ``path`` looks like a real working project the agent can code in.

    Used to gate project-scoped SessionStart injections (cold-start brief /
    God Nodes banner): plain chats and plugin/system directories must stay
    banner-free. A directory qualifies when it carries a VCS root or any
    well-known project manifest, and is neither the home directory, a hidden
    home subdirectory, nor a plugin/system dir per ``is_plugin_or_system_dir``.
    Fail-open is deliberately NOT used here: an unrecognized directory simply
    gets no brief (additive-only behavior, never breaks existing sessions).
    """
    try:
        if not path or not os.path.isdir(path):
            return False
        if is_plugin_or_system_dir(path):
            return False
        real = os.path.realpath(path)
        home = os.path.expanduser("~")
        if real == home or real.startswith(home + os.sep + "."):
            return False
        if os.path.isdir(os.path.join(real, ".git")) or os.path.isfile(os.path.join(real, ".git")):
            return True
        return any(os.path.exists(os.path.join(real, marker)) for marker in _PROJECT_MARKER_FILES)
    except Exception:
        return False

