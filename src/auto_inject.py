"""Automatic, reversible OUMV adapter wiring ("works out of the box").

Two very different injection strategies live here, because the runtimes differ:

* **Frontend (React/Vue + Vite)** — nothing is written into the repository. The
  bridge is served by ``tokensafe dev`` (a zero-footprint Vite server with an
  inline config) and discovered through ``~/.tokensafe/runtimes/<hash>.json``.
* **Python (FastAPI)** — a global ``sitecustomize.py`` in ``~/.tokensafe/pyhooks``
  patches ``uvicorn.importer.import_from_string`` so ``mount_tokensafe(app)`` runs
  as soon as the app object is imported. Zero repository changes; activated only
  by ``TOKENSAFE_OUMV_AUTOMOUNT=1``, which our command wrapper sets.
* **Go (Gin/Chi/net/http)** — there is no in-process hook, so one line plus one
  import is inserted between ``// tokensafe:oumv:begin/end`` markers. The original
  file is backed up under ``~/.tokensafe/inject/<hash>/`` and ``revert`` restores
  it byte-for-byte. The edited file is validated with ``go build`` before we keep
  it; a failed build reverts automatically.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Tuple

from src.retrofit import detect_project_stack, generate_config, get_adapters_catalog

_PYHOOKS_DIRNAME = "pyhooks"
_INJECT_DIRNAME = "inject"
_MARK_BEGIN = "// tokensafe:oumv:begin"
_MARK_END = "// tokensafe:oumv:end"

_GO_ADAPTERS = {
    "gin": (
        "oumvgin",
        "github.com/tokensafe-dev/tokensafe-backend/go/pkg/adapters/gin",
        "gin.New()",
        "gin.Default()",
        "\toumvgin.AutoMount(r)\n",
    ),
    "chi": (
        "oumvchi",
        "github.com/tokensafe-dev/tokensafe-backend/go/pkg/adapters/chi",
        "chi.NewRouter()",
        None,
        "\t_, _ = oumvchi.AutoMount(r)\n",
    ),
}


def _tokensafe_home() -> str:
    return os.environ.get("TOKENSAFE_HOME") or os.path.expanduser("~/.tokensafe")


def _workspace_id(workspace: str) -> str:
    return hashlib.sha256(os.path.realpath(workspace).encode("utf-8")).hexdigest()[:16]


def _inject_dir(workspace: str) -> str:
    return os.path.join(_tokensafe_home(), _INJECT_DIRNAME, _workspace_id(workspace))


def _manifest_path(workspace: str, kind: str) -> str:
    """One manifest per adapter kind: a monorepo can carry a Go patch and a .NET
    patch at once, and a single shared file would lose the first one's backup."""
    return os.path.join(_inject_dir(workspace), "manifests", f"{kind}.json")


def _write_manifest(workspace: str, kind: str, payload: Dict[str, Any]) -> str:
    path = _manifest_path(workspace, kind)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    return path


def _read_manifest(workspace: str, kind: str, legacy_modes: tuple) -> Optional[Dict[str, Any]]:
    """Own manifest first; a legacy single manifest.json only when its mode belongs
    to this adapter (the old file was shared by every kind)."""
    own = _manifest_path(workspace, kind)
    if os.path.isfile(own):
        try:
            with open(own, "r", encoding="utf-8") as f:
                data = json.load(f)
            data["_manifest"] = own
            return data
        except Exception:
            return None
    legacy = os.path.join(_inject_dir(workspace), "manifest.json")
    if os.path.isfile(legacy):
        try:
            with open(legacy, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return None
        if data.get("mode") in legacy_modes:
            data["_manifest"] = legacy
            return data
    return None


def _delete_manifest(manifest: Dict[str, Any]) -> None:
    path = manifest.get("_manifest")
    if path and os.path.isfile(path):
        os.unlink(path)


def pyhooks_dir() -> str:
    return os.path.join(_tokensafe_home(), _PYHOOKS_DIRNAME)


# --------------------------------------------------------------------------- #
# Python autoload (no repository changes)
# --------------------------------------------------------------------------- #

_SITECUSTOMIZE = '''"""TokenSafe OUMV autoload (generated) — see `tokensafe inject --status`.

Mounts `mount_tokensafe(app)` on a FastAPI app the moment uvicorn resolves it.
Inert unless TOKENSAFE_OUMV_AUTOMOUNT=1, which `tokensafe run-backend` sets.
"""
import os

if os.environ.get("TOKENSAFE_OUMV_AUTOMOUNT") == "1":
    def _install():
        try:
            from tokensafe_python.adapters.fastapi import mount_tokensafe as _mount
        except Exception:  # tokensafe-python not installed — nothing to do
            return

        import uvicorn.importer as _importer

        if getattr(_importer.import_from_string, "_tokensafe_wrapped", False):
            return

        _original = _importer.import_from_string

        def _patched(import_str, *args, **kwargs):
            app = _original(import_str, *args, **kwargs)
            try:
                _mount(app)
            except Exception as exc:  # never break the user's server
                import sys
                print(f"[tokensafe] OUMV autoload skipped: {exc}", file=sys.stderr)
            return app

        _patched._tokensafe_wrapped = True
        _importer.import_from_string = _patched

        # uvicorn/config.py binds the function by name at import time, so patching
        # the module attribute alone is not enough.
        try:
            import uvicorn.config as _config
            _config.import_from_string = _patched
        except Exception:
            pass

    _install()
'''


def ensure_python_autoload() -> Dict[str, Any]:
    """Writes the global sitecustomize hook. Returns what was done."""
    target_dir = pyhooks_dir()
    target = os.path.join(target_dir, "sitecustomize.py")
    os.makedirs(target_dir, exist_ok=True)
    current = None
    if os.path.isfile(target):
        with open(target, "r", encoding="utf-8") as f:
            current = f.read()
    if current == _SITECUSTOMIZE:
        return {"status": "unchanged", "path": target}
    with open(target, "w", encoding="utf-8") as f:
        f.write(_SITECUSTOMIZE)
    return {"status": "written", "path": target}


def python_autoload_present() -> bool:
    target = os.path.join(pyhooks_dir(), "sitecustomize.py")
    try:
        with open(target, "r", encoding="utf-8") as f:
            return f.read() == _SITECUSTOMIZE
    except OSError:
        return False


def backend_env(env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Environment that activates the Python autoload hook without touching the repo."""
    base = dict(env if env is not None else os.environ)
    existing = base.get("PYTHONPATH", "")
    hook = pyhooks_dir()
    base["PYTHONPATH"] = f"{hook}{os.pathsep}{existing}" if existing else hook
    base["TOKENSAFE_OUMV_AUTOMOUNT"] = "1"
    return base


def python_run_hint(workspace: str) -> str:
    """The command an agent should use to start a FastAPI dev server with OUMV."""
    return (
        f'TOKENSAFE_OUMV_AUTOMOUNT=1 PYTHONPATH="{pyhooks_dir()}:$PYTHONPATH" '
        "uvicorn <module:app> --reload"
    )


# --------------------------------------------------------------------------- #
# Go one-line patch (reversible)
# --------------------------------------------------------------------------- #

def _go_candidates(workspace: str) -> List[str]:
    candidates: List[str] = [os.path.join(workspace, "main.go")]
    for root, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs if d not in {"node_modules", ".git", "vendor", "testdata"}]
        if "main.go" in files and os.sep + "cmd" + os.sep in root + os.sep:
            candidates.append(os.path.join(root, "main.go"))
    return [c for c in candidates if os.path.isfile(c)]


def find_go_entrypoint(workspace: str, framework: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
    """Returns (path, framework) for the first main.go that looks mountable."""
    for path in _go_candidates(workspace):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                source = f.read()
        except OSError:
            continue
        detected = framework
        if not detected:
            if "gin-gonic/gin" in source:
                detected = "gin"
            elif "go-chi/chi" in source:
                detected = "chi"
            elif "tokensafe-dev/tokensafe-backend/go/pkg/adapters/stdhttp" in source:
                detected = "stdhttp"
        # stdhttp's mux is constructed by the adapter itself, so there is no anchor
        # line to patch; those projects get the generated-project recipe instead.
        if detected and detected in _GO_ADAPTERS and "func main(" in source:
            return path, detected
    return None, None


def _go_anchor(source: str, framework: str) -> Optional[int]:
    """Insertion point: immediately before the server starts listening.

    Mounting must happen after every route is registered, otherwise L1/L2 observe
    discovers nothing. Fall back to the router-construction line if the entrypoint
    starts the server indirectly.
    """
    for marker in ("r.Run(", ".ListenAndServe(", "ListenAndServeTLS("):
        idx = source.find(marker)
        if idx != -1:
            return source.rfind("\n", 0, idx) + 1

    _, _, anchor_a, anchor_b, _ = _GO_ADAPTERS[framework]
    for anchor in (anchor_a, anchor_b):
        if not anchor:
            continue
        idx = source.find(anchor)
        if idx == -1:
            continue
        line_end = source.find("\n", idx)
        return line_end if line_end != -1 else len(source)
    return None


def go_patch_state(workspace: str) -> Dict[str, Any]:
    path, framework = find_go_entrypoint(workspace)
    if not path:
        return {"patched": False, "entrypoint": None, "framework": None}
    with open(path, "r", encoding="utf-8") as f:
        source = f.read()
    return {
        "patched": _MARK_BEGIN in source,
        "entrypoint": os.path.relpath(path, workspace),
        "framework": framework,
    }


_GO_MODULE = "github.com/tokensafe-dev/tokensafe-backend/go"
# Keep in step with the backend release tag (go/vX.Y.Z): injection must never
# downgrade an adapter that is already newer than this pin.
_GO_MODULE_VERSION = "v0.11.0"


def _version_tuple(tag: str):
    """`v1.2.3` -> (1, 2, 3); raises ValueError when the tag is not semver."""
    parts = tag.lstrip("v").split(".")
    if len(parts) < 2:
        raise ValueError(tag)
    return tuple(int(part) for part in parts[:3] + ["0"] * (3 - len(parts[:3])))


def _ensure_go_dependency(workspace: str) -> Optional[str]:
    """Adds the adapter module to go.mod when missing. Returns an error reason or None."""
    go_mod = os.path.join(workspace, "go.mod")
    if not os.path.isfile(go_mod):
        return None
    try:
        with open(go_mod, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError as exc:
        return f"cannot read go.mod: {exc}"

    # Never downgrade: a project already on a newer go/v* tag keeps it, and an
    # older requirement is raised to the pin.
    match = re.search(re.escape(_GO_MODULE) + r"\s+v([0-9][\w.+-]*)", content)
    if match and _version_tuple(f"v{match.group(1)}") >= _version_tuple(_GO_MODULE_VERSION):
        return None

    go_bin = shutil.which("go")
    if not go_bin:
        return "go is not on PATH: cannot add the adapter dependency"

    try:
        proc = subprocess.run(
            [go_bin, "get", f"{_GO_MODULE}@{_GO_MODULE_VERSION}"],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except Exception as exc:  # pragma: no cover - defensive
        return f"go get failed to run: {exc}"
    if proc.returncode != 0:
        output = (proc.stderr or proc.stdout or "").strip().splitlines()
        return "go get failed: " + (output[0] if output else "unknown error")
    return None


def _go_tidy(workspace: str) -> Tuple[bool, str]:
    """Runs `go mod tidy` after the import exists (tidy before would drop it)."""
    go_bin = shutil.which("go")
    if not go_bin:
        return True, "go not found on PATH — tidy skipped"
    try:
        proc = subprocess.run(
            [go_bin, "mod", "tidy"],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except Exception as exc:  # pragma: no cover - defensive
        return True, f"tidy failed to run: {exc}"
    return proc.returncode == 0, (proc.stderr or proc.stdout or "").strip()


_GO_SERVE_PATTERNS = {
    # `r.Run(":8080")` — gin
    "gin": re.compile(r'(?m)^(?P<indent>[ \t]*)(?P<router>[A-Za-z_]\w*)\.Run\((?P<addr>[^()\n]*)\)[ \t]*$'),
    # `http.ListenAndServe(":8080", r)` — chi
    "chi": re.compile(
        r'(?m)^(?P<indent>[ \t]*)http\.ListenAndServe\((?P<addr>[^,()\n]+),\s*(?P<router>[A-Za-z_]\w*)\)[ \t]*$'
    ),
}


def _go_router_var(source: str, framework: str) -> Optional[str]:
    _, _, anchor_a, anchor_b, _ = _GO_ADAPTERS[framework]
    for anchor in (anchor_a, anchor_b):
        if not anchor:
            continue
        idx = source.find(anchor)
        if idx == -1:
            continue
        line_start = source.rfind("\n", 0, idx) + 1
        line_end = source.find("\n", idx)
        line = source[line_start : line_end if line_end != -1 else len(source)]
        match = re.search(r"([A-Za-z_]\w*)\s*:?=\s*" + re.escape(anchor), line)
        if match:
            return match.group(1)
    return None


def _wrap_go_serve(source: str, framework: str) -> Optional[Tuple[str, str]]:
    """Rewrites a simple serve call to the discovery-announcing wrapper.

    `r.Run(":8080")` → `oumvgin.AutoMountAndServe(r, ":8080")`, so the §9.2
    descriptor carries the port the kernel actually assigned. Only a one-line call
    with no nested parentheses is rewritten; anything else falls back to a bare
    AutoMount (the user can swap the serve call manually).
    """
    pattern = _GO_SERVE_PATTERNS.get(framework)
    if pattern is None:
        return None
    router_var = _go_router_var(source, framework)
    for match in pattern.finditer(source):
        if router_var and match.group("router") != router_var:
            continue
        alias = _GO_ADAPTERS[framework][0]
        router = match.group("router")
        addr = match.group("addr").strip()
        indent = match.group("indent")
        block = (
            f"{indent}{_MARK_BEGIN}\n"
            f"{indent}if err := {alias}.AutoMountAndServe({router}, {addr}); err != nil {{\n"
            f"{indent}\tlog.Fatal(err)\n"
            f"{indent}}}\n"
            f"{indent}{_MARK_END}\n"
        )
        return source[: match.start()] + block + source[match.end() :], block.strip()
    return None


def _ensure_std_import(source: str, package: str) -> str:
    if re.search(r'^\s*(?:\w+\s+)?"%s"\s*$' % re.escape(package), source, re.M):
        return source
    idx = source.find("import (")
    if idx == -1:
        return source
    line_end = source.find("\n", idx)
    if line_end == -1:
        return source
    return source[: line_end + 1] + f'\t"{package}"\n' + source[line_end + 1 :]


def apply_go_patch(workspace: str, dry_run: bool = False) -> Dict[str, Any]:
    """Ensures the module dependency, inserts the AutoMount call, validates with `go build`.

    The original file is backed up first and restored byte-for-byte if any step fails.
    """
    path, framework = find_go_entrypoint(workspace)
    if not path or not framework:
        return {"status": "skipped", "reason": "no mountable Go entrypoint (main.go with gin/chi/net/http) found"}

    if not dry_run:
        dep_error = _ensure_go_dependency(workspace)
        if dep_error:
            return {"status": "skipped", "reason": dep_error}

    with open(path, "r", encoding="utf-8") as f:
        original = f.read()

    if _MARK_BEGIN in original:
        return {"status": "already-patched", "entrypoint": path, "framework": framework}

    anchor = _go_anchor(original, framework)
    alias, import_path, _, _, call = _GO_ADAPTERS[framework]
    wrapped = _wrap_go_serve(original, framework)
    if wrapped is None and anchor is None:
        return {
            "status": "skipped",
            "reason": f"could not find router construction ({framework}) in {os.path.relpath(path, workspace)}",
        }

    if wrapped is not None:
        patched, preview = wrapped
        patched = _ensure_std_import(patched, "log")
    else:
        insert = f"\t{_MARK_BEGIN}\n{call}\t{_MARK_END}\n"
        patched = original[:anchor] + insert + original[anchor:]
        preview = insert.strip()

    import_line = f'\t{alias} "{import_path}"\n'
    if import_path not in patched:
        import_idx = patched.find("import (")
        if import_idx == -1:
            return {"status": "skipped", "reason": "no parenthesised import block to extend"}
        line_end = patched.find("\n", import_idx)
        patched = patched[: line_end + 1] + import_line + patched[line_end + 1 :]

    if dry_run:
        return {
            "status": "would-patch",
            "entrypoint": os.path.relpath(path, workspace),
            "framework": framework,
            "mode": "serve" if wrapped is not None else "mount",
            "preview": preview,
        }

    backup_dir = _inject_dir(workspace)
    os.makedirs(backup_dir, exist_ok=True)
    suffix = os.path.relpath(path, workspace).replace(os.sep, "__") + ".bak"
    backup_path = os.path.join(backup_dir, suffix)
    shutil.copy2(path, backup_path)
    module_backups: List[Tuple[str, str]] = []
    for mod_file in ("go.mod", "go.sum"):
        src = os.path.join(workspace, mod_file)
        if os.path.isfile(src):
            dst = os.path.join(backup_dir, mod_file + ".bak")
            shutil.copy2(src, dst)
            module_backups.append((src, dst))

    with open(path, "w", encoding="utf-8") as f:
        f.write(patched)

    tidy_ok, tidy_output = _go_tidy(workspace)
    build_ok, build_output = _go_build(workspace) if tidy_ok else (False, tidy_output)
    if not build_ok:
        shutil.copy2(backup_path, path)
        for src, dst in module_backups:
            shutil.copy2(dst, src)
        first_error = next((line for line in build_output.splitlines() if line.strip()), "")
        return {
            "status": "reverted",
            "reason": "go build failed after the patch — original file restored"
                      + (f" ({first_error.strip()})" if first_error else ""),
            "build_output": build_output[-1200:],
        }

    _write_manifest(workspace, "go", {
        "entrypoint": path,
        "backup": backup_path,
        "framework": framework,
        "mode": "serve" if wrapped is not None else "mount",
    })

    return {
        "status": "patched",
        "entrypoint": os.path.relpath(path, workspace),
        "framework": framework,
        "mode": "serve" if wrapped is not None else "mount",
        "backup": backup_path,
        "line": (preview.splitlines()[1].strip() if wrapped is not None else call.strip()),
    }


def _go_build(workspace: str) -> Tuple[bool, str]:
    go_bin = shutil.which("go")
    if not go_bin:
        return True, "go not found on PATH — build validation skipped"
    try:
        proc = subprocess.run(
            [go_bin, "build", "./..."],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return proc.returncode == 0, (proc.stderr or proc.stdout or "").strip()
    except Exception as exc:  # pragma: no cover - defensive
        return True, f"build validation failed to run: {exc}"


def revert_go_patch(workspace: str) -> Dict[str, Any]:
    manifest = _read_manifest(workspace, "go", ("serve", "mount", "go_patch"))
    if manifest is None:
        return {"status": "nothing-to-revert"}
    target = manifest.get("entrypoint")
    backup = manifest.get("backup")
    if not target or not backup or not os.path.isfile(backup):
        return {"status": "error", "reason": "backup missing"}
    shutil.copy2(backup, target)
    _delete_manifest(manifest)
    return {"status": "reverted", "entrypoint": os.path.relpath(target, workspace)}


# --------------------------------------------------------------------------- #
# Plan / apply / revert for the CLI and the first-prompt hook
# --------------------------------------------------------------------------- #

def _find_node_entrypoint(workspace: str) -> Optional[str]:
    """Finds the Node entry file that creates the framework app."""
    candidates: List[str] = []
    pkg_path = os.path.join(workspace, "package.json")
    try:
        with open(pkg_path, "r", encoding="utf-8") as f:
            pkg = json.load(f)
        main = pkg.get("main")
        if isinstance(main, str) and main:
            candidates.append(main)
    except Exception:
        pass
    candidates += ["src/index.js", "src/server.js", "src/app.js", "index.js", "server.js", "app.js"]
    for rel in candidates:
        path = os.path.join(workspace, rel)
        if os.path.isfile(path):
            return path
    return None


def _node_framework_of(workspace: str) -> Optional[str]:
    try:
        with open(os.path.join(workspace, "package.json"), "r", encoding="utf-8") as f:
            pkg = json.load(f)
        deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
    except Exception:
        return None
    if "fastify" in deps:
        return "fastify"
    if "express" in deps:
        return "express"
    return None


def node_patch_state(workspace: str) -> Dict[str, Any]:
    """Reports whether the one-line OUMV mount is already in the Node entrypoint."""
    path = _find_node_entrypoint(workspace)
    if not path:
        return {"entrypoint": None, "patched": False, "framework": _node_framework_of(workspace)}
    try:
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
    except OSError:
        return {"entrypoint": path, "patched": False, "framework": _node_framework_of(workspace)}
    return {
        "entrypoint": os.path.relpath(path, workspace),
        "patched": _MARK_BEGIN in source and _MARK_END in source,
        "framework": _node_framework_of(workspace),
    }


def apply_node_patch(workspace: str, dry_run: bool = False) -> Dict[str, Any]:
    """Inserts one mount line between tokensafe markers, with a byte-exact backup."""
    framework = _node_framework_of(workspace)
    path = _find_node_entrypoint(workspace)
    if framework not in ("express", "fastify"):
        return {"status": "skipped", "reason": "no supported Node framework (express/fastify) found"}
    if not path:
        return {"status": "skipped", "reason": "no Node entrypoint found"}

    with open(path, "r", encoding="utf-8") as f:
        source = f.read()
    if _MARK_BEGIN in source:
        return {"status": "already_patched", "entrypoint": os.path.relpath(path, workspace)}

    if framework == "express":
        pattern = re.compile(r"(?m)^(?P<indent>[ \t]*)(?:const|let|var)\s+\w+\s*=\s*express\(\)[ \t]*;?[ \t]*$")
        mount = (
            f"{_MARK_BEGIN}\n"
            "require('tokensafe-backend-node').mountTokensafe(app);\n"
            f"{_MARK_END}\n"
        )
    else:
        pattern = re.compile(r"(?m)^(?P<indent>[ \t]*)(?:const|let|var)\s+(?P<var>\w+)\s*=\s*(?:Fastify|fastify)\([^)]*\)[ \t]*;?[ \t]*$")
        mount = (
            f"{_MARK_BEGIN}\n"
            "void require('tokensafe-backend-node').registerTokensafe(app);\n"
            f"{_MARK_END}\n"
        )

    found = pattern.search(source)
    if not found:
        return {
            "status": "skipped",
            "reason": f"could not find `{framework}(...)` assignment in {os.path.relpath(path, workspace)}",
        }

    line_end = source.find("\n", found.end())
    insert_at = len(source) if line_end < 0 else line_end + 1
    patched = source[:insert_at] + mount + source[insert_at:]

    if dry_run:
        return {"status": "dry_run", "entrypoint": os.path.relpath(path, workspace), "preview": mount.strip()}

    backup_dir = _inject_dir(workspace)
    os.makedirs(backup_dir, exist_ok=True)
    backup_path = os.path.join(backup_dir, os.path.relpath(path, workspace).replace(os.sep, "__") + ".bak")
    shutil.copy2(path, backup_path)
    with open(path, "w", encoding="utf-8") as f:
        f.write(patched)
    _write_manifest(workspace, "node", {"entrypoint": path, "backup": backup_path, "framework": framework})
    return {
        "status": "patched",
        "entrypoint": os.path.relpath(path, workspace),
        "framework": framework,
        "mode": "node_mount",
        "backup": backup_path,
        "line": mount.splitlines()[1],
    }


def revert_node_patch(workspace: str) -> Dict[str, Any]:
    manifest = _read_manifest(workspace, "node", ("node_mount",))
    if manifest is None:
        return {"status": "clean", "reason": "no Node patch manifest"}
    entrypoint = manifest.get("entrypoint")
    backup = manifest.get("backup")
    if not (entrypoint and backup and os.path.isfile(backup)):
        return {"status": "error", "reason": "backup missing; restore manually"}
    shutil.copy2(backup, entrypoint)
    _delete_manifest(manifest)
    return {"status": "reverted", "entrypoint": os.path.relpath(entrypoint, workspace)}



# Next.js App Router: the adapter is a Route Handler created by the npm package.
# Nothing is patched inside app components — one generated file mounts the
# runtime, and `revert` deletes it, so the worktree returns to its old shape.
_NEXT_ROUTE_REL = os.path.join("app", "api", "tokensafe", "route.ts")
_NEXT_ROUTE_SOURCE = """// TokenSafe OUMV Route Handler (App Router, wire 1.6.0).
// Generated by `tokensafe inject`; delete this file (or run `tokensafe inject --revert`) to unmount.
import { createNextRouteHandlers } from 'tokensafe-backend-node';

const handlers = createNextRouteHandlers({
  workspaceDir: process.cwd(),
  publicUrl: `${process.env.TOKENSAFE_OUMV_PUBLIC_URL ?? 'http://127.0.0.1:3000'}/__oumv/rpc`,
});

export const POST = handlers.POST;
export const GET = handlers.GET;
"""


def next_route_path(workspace: str) -> str:
    return os.path.join(workspace, _NEXT_ROUTE_REL)


def next_patch_state(workspace: str) -> Dict[str, Any]:
    path = next_route_path(workspace)
    return {"entrypoint": path if os.path.isfile(path) else None, "patched": os.path.isfile(path)}


def apply_next_patch(workspace: str, dry_run: bool = False) -> Dict[str, Any]:
    path = next_route_path(workspace)
    if os.path.isfile(path):
        return {"status": "already_patched", "entrypoint": path}
    if dry_run:
        return {"status": "dry_run", "entrypoint": path, "would_create": path}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(_NEXT_ROUTE_SOURCE)
    _write_manifest(workspace, "next", {"entrypoint": path, "framework": "nextjs", "mode": "next_route"})
    return {"status": "patched", "entrypoint": path}


def revert_next_patch(workspace: str) -> Dict[str, Any]:
    manifest = _read_manifest(workspace, "next", ("next_route",))
    if not manifest:
        return {"status": "nothing-to-revert"}
    entrypoint = manifest.get("entrypoint")
    if entrypoint and os.path.isfile(entrypoint):
        os.remove(entrypoint)
        parent = os.path.dirname(entrypoint)
        try:
            os.removedirs(parent)  # only removes the directories this adapter created
        except OSError:
            pass
    _delete_manifest(manifest)
    return {"status": "reverted", "entrypoint": entrypoint}


def detect_orm(workspace: str) -> Dict[str, Any]:
    """Static ORM hint for the inject plan (the .NET AddDbContext scan is the model).

    It reads manifests and imports, never runs the app: `sandbox` says whether the
    adapter can adopt the engine on its own (`auto`), needs an explicit argument
    (`manual`), or has no engine to sandbox (`none`).
    """
    deps: Dict[str, str] = {}
    sources: List[str] = []
    package_json = os.path.join(workspace, "package.json")
    if os.path.isfile(package_json):
        try:
            with open(package_json, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            for key in ("dependencies", "devDependencies"):
                deps.update({str(k): str(v) for k, v in (data.get(key) or {}).items()})
        except Exception:
            pass
    for name in ("pyproject.toml", "requirements.txt", "go.mod"):
        candidate = os.path.join(workspace, name)
        if os.path.isfile(candidate):
            try:
                with open(candidate, "r", encoding="utf-8") as handle:
                    sources.append(handle.read().lower())
            except Exception:
                pass
    python_text = " ".join(sources)

    if "@prisma/client" in deps or "prisma" in deps:
        return {"orm": "prisma", "sandbox": "auto", "how": "Prisma $transaction detected from the app's prisma client (app.locals.prisma / fastify decorator)"}
    if "knex" in deps:
        return {"orm": "knex", "sandbox": "manual", "how": "pass sandbox.db.run: knex.transaction + rollback"}
    if "drizzle-orm" in deps:
        return {"orm": "drizzle", "sandbox": "manual", "how": "pass sandbox.db.run: db.transaction + rollback"}
    if "pg" in deps or "mysql2" in deps or "better-sqlite3" in deps:
        return {"orm": "database/sql", "sandbox": "manual", "how": "pass sandbox.db.run around a transaction"}
    if "sqlalchemy" in python_text:
        return {"orm": "sqlalchemy", "sandbox": "auto", "how": "Engine on app.state (or dependency override) is adopted by mount_tokensafe"}
    if "django" in python_text:
        return {"orm": "django-orm", "sandbox": "manual", "how": "atomic block + forced rollback; not auto-detected"}
    if "gorm.io/gorm" in python_text:
        return {"orm": "gorm", "sandbox": "manual", "how": "rt.SetDBSandbox(db.NewSandbox(sqlDB))"}
    if "database/sql" in python_text:
        return {"orm": "database/sql", "sandbox": "manual", "how": "rt.SetDBSandbox(db.NewSandbox(sqlDB)) or gin/chi Options{DB}"}
    if "microsoft.entityframeworkcore" in python_text or "@prisma" in deps:
        return {"orm": "ef-core", "sandbox": "auto", "how": "AddDbContext is discovered by AddTokensafe()"}
    if "entityframework" in python_text:
        return {"orm": "ef-core", "sandbox": "auto", "how": "AddDbContext is discovered by AddTokensafe()"}
    return {"orm": None, "sandbox": "none", "how": "no ORM detected"}

def _sidecar_cache_dir() -> str:
    return os.path.join(_tokensafe_home(), "sidecars")


# --- ASP.NET Core -----------------------------------------------------------------
#
# The .NET adapter is a NuGet package with a middleware mount, so injection is the
# same shape as Go/Node: one visible, reversible block in Program.cs. The builder
# line is required only because ASP.NET Core does not expose the service collection
# after Build(), and EF Core DbContext discovery needs it.

_DOTNET_MARK_BEGIN = "// tokensafe:oumv:begin"
_DOTNET_MARK_END = "// tokensafe:oumv:end"
_DOTNET_PACKAGE = "TokenSafe.Oumv.AspNetCore"
_DOTNET_PACKAGE_VERSION = "0.2.0"



def _dotnet_cli() -> str:
    """Resolves the dotnet executable: explicit override, then DOTNET_ROOT, then PATH.

    A machine can carry an old SDK on PATH (this one had 7.0) while a current one
    lives under DOTNET_ROOT; the build pre-flight must use the latter or a net10
    project fails to compile for the wrong reason.
    """
    override = os.environ.get("TOKENSAFE_DOTNET")
    if override and os.path.isfile(override):
        return override
    root = os.environ.get("DOTNET_ROOT")
    if root:
        candidate = os.path.join(root, "dotnet.exe" if os.name == "nt" else "dotnet")
        if os.path.isfile(candidate):
            return candidate
    return "dotnet"

def _dotnet_web_project(workspace: str) -> Optional[str]:
    """Finds a web csproj (Microsoft.NET.Sdk.Web or the ASP.NET Core framework reference)."""
    for root, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs if d not in ("bin", "obj", "node_modules", ".git", ".tokensafe")]
        for name in files:
            if not name.endswith(".csproj"):
                continue
            path = os.path.join(root, name)
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    content = handle.read()
            except OSError:
                continue
            if "Microsoft.NET.Sdk.Web" in content or "Microsoft.AspNetCore.App" in content:
                return path
    return None


def _find_dotnet_entrypoint(workspace: str, project: Optional[str] = None) -> Optional[str]:
    """Program.cs next to the web project (top-level statements), if present."""
    if project:
        candidate = os.path.join(os.path.dirname(project), "Program.cs")
        if os.path.isfile(candidate):
            return candidate
    candidate = os.path.join(workspace, "Program.cs")
    return candidate if os.path.isfile(candidate) else None


def dotnet_patch_state(workspace: str) -> Dict[str, Any]:
    """Reports whether the OUMV mount is already present in Program.cs."""
    project = _dotnet_web_project(workspace)
    entrypoint = _find_dotnet_entrypoint(workspace, project)
    if not entrypoint:
        return {"entrypoint": None, "patched": False, "project": project}
    try:
        with open(entrypoint, "r", encoding="utf-8") as handle:
            source = handle.read()
    except OSError:
        return {"entrypoint": os.path.relpath(entrypoint, workspace), "patched": False, "project": project}
    return {
        "entrypoint": os.path.relpath(entrypoint, workspace),
        "patched": _DOTNET_MARK_BEGIN in source and _DOTNET_MARK_END in source,
        "project": os.path.relpath(project, workspace) if project else None,
    }


def _dotnet_build(project: str) -> Tuple[bool, str]:
    try:
        result = subprocess.run(
            [_dotnet_cli(), "build", project, "-c", "Release", "--nologo", "-v", "quiet"],
            capture_output=True,
            text=True,
            timeout=600,
        )
        return result.returncode == 0, (result.stdout + result.stderr)[-1500:]
    except FileNotFoundError:
        return False, "dotnet SDK not found on PATH"
    except subprocess.TimeoutExpired:
        return False, "dotnet build timed out"


def _ensure_dotnet_dependency(project: str) -> Optional[str]:
    """Adds the NuGet package once; returns a note when the CLI could not do it."""
    try:
        with open(project, "r", encoding="utf-8") as handle:
            content = handle.read()
    except OSError:
        return f"could not read {project}"
    if _DOTNET_PACKAGE in content:
        return None
    try:
        result = subprocess.run(
            [_dotnet_cli(), "add", project, "package", _DOTNET_PACKAGE, "--version", _DOTNET_PACKAGE_VERSION],
            capture_output=True,
            text=True,
            timeout=300,
        )
    except FileNotFoundError:
        return f"dotnet SDK not found; add <PackageReference Include=\"{_DOTNET_PACKAGE}\" Version=\"{_DOTNET_PACKAGE_VERSION}\" /> manually"
    except subprocess.TimeoutExpired:
        return "dotnet add package timed out"
    if result.returncode != 0:
        return f"dotnet add package failed; add the PackageReference manually: {(result.stdout + result.stderr)[-300:]}"
    return None


def apply_dotnet_patch(workspace: str, dry_run: bool = False) -> Dict[str, Any]:
    """Inserts `using`, `AddTokensafe()` and `UseTokensafe()` between tokensafe markers."""
    project = _dotnet_web_project(workspace)
    if not project:
        return {"status": "skipped", "reason": "no ASP.NET Core (Microsoft.NET.Sdk.Web) project found"}
    entrypoint = _find_dotnet_entrypoint(workspace, project)
    if not entrypoint:
        return {"status": "skipped", "reason": "no Program.cs next to the web project"}

    with open(entrypoint, "r", encoding="utf-8") as handle:
        source = handle.read()
    if _DOTNET_MARK_BEGIN in source:
        return {"status": "already_patched", "entrypoint": os.path.relpath(entrypoint, workspace)}

    builder = re.search(
        r"(?m)^(?P<indent>[ \t]*)(?:var|WebApplicationBuilder)\s+(?P<builder>\w+)\s*=\s*WebApplication\.CreateBuilder\(",
        source,
    )
    app = re.search(
        r"(?m)^(?P<indent>[ \t]*)(?:var|WebApplication)\s+(?P<app>\w+)\s*=\s*(?P<builder>\w+)\.Build\(\)\s*;",
        source,
    )
    if not builder or not app:
        return {
            "status": "skipped",
            "reason": "could not find `WebApplication.CreateBuilder(...)` and `builder.Build()` in Program.cs",
        }
    if builder.group("builder") != app.group("builder"):
        return {"status": "skipped", "reason": "Program.cs builds the app from a different builder variable"}

    def insertion(line_match: "re.Match[str]", body: str) -> Tuple[int, str]:
        line_end = source.find("\n", line_match.end())
        insert_at = len(source) if line_end < 0 else line_end + 1
        indent = line_match.group("indent")
        block = f"{_DOTNET_MARK_BEGIN}\n{indent}{body}\n{_DOTNET_MARK_END}\n"
        return insert_at, block

    services_at, services_block = insertion(
        builder, f"{builder.group('builder')}.Services.AddTokensafe();"
    )
    mount_at, mount_block = insertion(app, f"{app.group('app')}.UseTokensafe();")

    using_block = ""
    if not re.search(r"(?m)^\s*using\s+TokenSafe\.Oumv\s*;", source):
        using_lines = list(re.finditer(r"(?m)^using\s+[^;]+;\s*$", source))
        using_block = f"{_DOTNET_MARK_BEGIN}\nusing TokenSafe.Oumv;\n{_DOTNET_MARK_END}\n"
        if using_lines:
            last = using_lines[-1]
            line_end = source.find("\n", last.end())
            using_at = len(source) if line_end < 0 else line_end + 1
        else:
            using_at = 0

    patched = source
    # Insert from the bottom up so earlier offsets stay valid.
    for position, block in sorted(((services_at, services_block), (mount_at, mount_block)), reverse=True):
        patched = patched[:position] + block + patched[position:]
    if using_block:
        patched = patched[:using_at] + using_block + patched[using_at:]

    if dry_run:
        return {
            "status": "dry_run",
            "entrypoint": os.path.relpath(entrypoint, workspace),
            "preview": f"{builder.group('builder')}.Services.AddTokensafe();\n{app.group('app')}.UseTokensafe();",
        }

    backup_dir = _inject_dir(workspace)
    os.makedirs(backup_dir, exist_ok=True)
    backup_path = os.path.join(backup_dir, os.path.relpath(entrypoint, workspace).replace(os.sep, "__") + ".bak")
    shutil.copy2(entrypoint, backup_path)
    with open(entrypoint, "w", encoding="utf-8") as handle:
        handle.write(patched)

    dependency_note = _ensure_dotnet_dependency(project)
    build_ok, build_output = _dotnet_build(project) if dependency_note is None else (False, "skipped")
    if not build_ok:
        shutil.copy2(backup_path, entrypoint)
        os.unlink(backup_path)
        return {
            "status": "reverted",
            "entrypoint": os.path.relpath(entrypoint, workspace),
            "reason": dependency_note or "dotnet build failed",
            "build": build_output[-800:],
        }

    _write_manifest(workspace, "dotnet",
                    {"entrypoint": entrypoint, "backup": backup_path, "framework": "aspnetcore"})
    return {
        "status": "patched",
        "entrypoint": os.path.relpath(entrypoint, workspace),
        "framework": "aspnetcore",
        "mode": "dotnet_mount",
        "backup": backup_path,
        "line": f"{app.group('app')}.UseTokensafe();",
        "dependency": f"{_DOTNET_PACKAGE}@{_DOTNET_PACKAGE_VERSION}",
    }


def revert_dotnet_patch(workspace: str) -> Dict[str, Any]:
    manifest = _read_manifest(workspace, "dotnet", ("dotnet_mount",))
    if manifest is None:
        return {"status": "clean", "reason": "no .NET patch manifest"}
    entrypoint = manifest.get("entrypoint")
    backup = manifest.get("backup")
    if not (entrypoint and backup and os.path.isfile(backup)):
        return {"status": "error", "reason": "backup missing; restore manually"}
    shutil.copy2(backup, entrypoint)
    _delete_manifest(manifest)
    return {"status": "reverted", "entrypoint": os.path.relpath(entrypoint, workspace)}


# Pinned to the backend release: the sidecars are single files, so the cache is
# content-addressed by SHA-256 and a clean machine downloads the exact bytes the
# release was certified from. Bump both the tag and the hashes together.
_SIDECAR_RELEASE_TAG = "v0.9.2"
_SIDECARS = {
    "spring": (
        "java/src/dev/tokensafe/oumv/OumvSpring.java",
        "OumvSpring.java",
        "45aa63f2c71cf25065f591ebd589b21c7c7c58d218f3dce45880af0e1f1e7d81",
    ),
    "php": (
        "php/bin/oumv-php",
        "oumv-php",
        "660e7f53cc3799bd700cea2173f672594a5b8bc33e26cfa91ee559aec66f325a",
    ),
}


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sidecar_run_command(kind: str, sidecar_path: str, workspace: str) -> Optional[str]:
    """The exact command that starts the sidecar against `workspace`."""
    if not sidecar_path:
        return None
    if kind == "spring":
        return f"tokensafe run-backend java {sidecar_path} --root {workspace} --port 7331"
    if kind == "php":
        return f"tokensafe run-backend php -S 127.0.0.1:7332 {sidecar_path} --root {workspace}"
    return None


def ensure_sidecar(kind: str) -> Tuple[Optional[str], str]:
    """Caches OumvSpring.java / oumv-php at the pinned release and verifies SHA-256.

    Order: a valid cache → a local checkout whose bytes match the pin (developer
    flow, no network) → download from the release tag. A hash mismatch is never
    accepted silently; the note says what failed instead of handing back a
    different file under the certified name.
    """
    if kind not in _SIDECARS:
        return None, f"unknown sidecar {kind!r}"
    rel_source, filename, expected_sha = _SIDECARS[kind]
    cache_dir = _sidecar_cache_dir()
    target = os.path.join(cache_dir, filename)

    def adopt(source: str, note: str) -> Tuple[str, str]:
        os.makedirs(cache_dir, exist_ok=True)
        shutil.copy2(source, target)
        if kind == "php":
            os.chmod(target, 0o755)
        return target, note

    if os.path.isfile(target) and _sha256_file(target) == expected_sha:
        return target, f"cached at {_SIDECAR_RELEASE_TAG}"

    repo_candidates = []
    env_repo = os.environ.get("OUMV_BACKEND_REPO")
    if env_repo:
        repo_candidates.append(env_repo)
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    repo_candidates += [
        os.path.join(os.path.dirname(here), "tokensafe-backend"),
        os.path.join(here, "..", "tokensafe-backend"),
    ]
    local_mismatch = None
    for repo in repo_candidates:
        candidate = os.path.join(os.path.abspath(repo), rel_source)
        if not os.path.isfile(candidate):
            continue
        if _sha256_file(candidate) == expected_sha:
            return adopt(candidate, f"cached from local checkout matching {_SIDECAR_RELEASE_TAG}")
        local_mismatch = candidate

    if os.environ.get("TOKENSAFE_SIDECAR_OFFLINE") == "1":
        if local_mismatch:
            return adopt(local_mismatch, f"local checkout (unpinned, offline): {local_mismatch}")
        return None, "offline (TOKENSAFE_SIDECAR_OFFLINE=1) and no cached sidecar"

    url = (
        f"https://raw.githubusercontent.com/tokensafe-dev/tokensafe-backend/"
        f"{_SIDECAR_RELEASE_TAG}/{rel_source}"
    )
    try:
        import urllib.request

        with urllib.request.urlopen(url, timeout=30) as response:
            payload = response.read()
    except Exception as exc:
        if local_mismatch:
            return adopt(local_mismatch, f"download failed ({exc}); local checkout (unpinned): {local_mismatch}")
        return None, f"cannot download {url}: {exc}"

    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected_sha:
        return None, f"sha256 mismatch for {url}: expected {expected_sha}, got {actual}"
    os.makedirs(cache_dir, exist_ok=True)
    with open(target, "wb") as handle:
        handle.write(payload)
    if kind == "php":
        os.chmod(target, 0o755)
    return target, f"downloaded from {_SIDECAR_RELEASE_TAG}"


def plan(workspace: str) -> Dict[str, Any]:
    """Describes what apply would do, without touching anything."""
    stack = detect_project_stack(workspace)
    catalog = {entry["id"]: entry for entry in get_adapters_catalog()}
    actions: List[Dict[str, Any]] = []

    frontend = stack.get("frontend")
    is_next = bool(frontend and frontend.get("framework") == "nextjs")
    if frontend and not is_next:
        actions.append({
            "kind": "frontend_zero_footprint",
            "profile": "frontend",
            "framework": f"{frontend.get('provider')}/{frontend.get('framework')}",
            "writes_repo": False,
            "description": "served by `tokensafe dev` (inline Vite config, descriptor in ~/.tokensafe/runtimes)",
            "run": "tokensafe dev",
            "install": catalog.get("frontend/react", {}).get("technology", {}).get("package", {}).get("install"),
        })
    if is_next:
        next_state = next_patch_state(workspace)
        actions.append({
            "kind": "next_route_handler",
            "profile": "backend",
            "framework": "nextjs",
            "writes_repo": True,
            "already_patched": next_state["patched"],
            "entrypoint": next_state["entrypoint"] or next_route_path(workspace),
            "description": "one App Router Route Handler (app/api/tokensafe/route.ts) calling createNextRouteHandlers; server profile only — delete the file or --revert to unmount",
            "install": "npm install --save-dev tokensafe-backend-node",
            "run": "next dev",
        })

    backend = stack.get("backend")
    if backend:
        provider = backend.get("provider")
        entry_id = {
            "fastapi": "backend/fastapi",
            "go": "backend/go",
            "node": "backend/node",
            "spring": "backend/spring",
            "php": "backend/php",
            "dotnet": "backend/aspnetcore",
        }.get(provider, "backend/go")
        entry = catalog.get(entry_id, {})
        install = entry.get("technology", {}).get("package", {}).get("install")
        if provider == "fastapi":
            actions.append({
                "kind": "python_autoload",
                "profile": "backend",
                "framework": "fastapi",
                "writes_repo": False,
                "description": "sitecustomize patches uvicorn; mount_tokensafe(app) runs on import",
                "install": install or 'pip install "tokensafe-backend-python @ git+https://github.com/tokensafe-dev/tokensafe-backend.git#subdirectory=python"',
                "run": python_run_hint(workspace),
            })
        elif provider == "go":
            state = go_patch_state(workspace)
            actions.append({
                "kind": "go_one_line_patch",
                "profile": "backend",
                "framework": backend.get("framework", "go"),
                "writes_repo": True,
                "already_patched": state["patched"],
                "entrypoint": state["entrypoint"],
                "description": "one AutoMount() line between tokensafe markers; byte-exact backup + --revert",
                "install": install,
            })
        elif provider == "node":
            state = node_patch_state(workspace)
            actions.append({
                "kind": "node_one_line_patch",
                "profile": "backend",
                "framework": state.get("framework") or backend.get("framework"),
                "writes_repo": True,
                "already_patched": state["patched"],
                "entrypoint": state["entrypoint"],
                "description": "one mountTokensafe(app)/registerTokensafe(app) line between tokensafe markers; byte-exact backup + --revert",
                "install": install or "npm install tokensafe-backend-node",
            })
        elif provider == "dotnet":
            state = dotnet_patch_state(workspace)
            actions.append({
                "kind": "dotnet_one_line_patch",
                "profile": "backend",
                "framework": "aspnetcore",
                "writes_repo": True,
                "already_patched": state["patched"],
                "entrypoint": state["entrypoint"],
                "project": state.get("project"),
                "description": "one AddTokensafe()/UseTokensafe() pair between tokensafe markers; byte-exact backup + --revert",
                "install": install or f"dotnet add package {_DOTNET_PACKAGE}",
                "run": "dotnet run",
            })
        elif provider in ("spring", "php"):
            sidecar_path, note = ensure_sidecar(provider)
            command = sidecar_run_command(provider, sidecar_path or "", workspace)
            actions.append({
                "kind": "sidecar",
                "profile": "backend",
                "framework": backend.get("framework", provider),
                "writes_repo": False,
                "sidecar": sidecar_path,
                "install": note,
                "run": command,
                "description": "static route scan + OCC edits + verify through the single-file sidecar; nothing is written into the project",
            })
        else:
            actions.append({
                "kind": "unsupported",
                "profile": "backend",
                "framework": backend.get("framework"),
                "writes_repo": False,
                "description": "no OUMV adapter for this stack yet — use tokensafe tools instead",
            })

    db = detect_orm(workspace)
    for action in actions:
        action.setdefault("db", db)

    return {
        "workspace": workspace,
        "profiles": [p for p in ("frontend", "backend") if stack.get(p)],
        "actions": actions,
        "python_autoload_present": python_autoload_present(),
        "revertable": bool(glob.glob(os.path.join(_inject_dir(workspace), "manifests", "*.json")))
        or os.path.isfile(os.path.join(_inject_dir(workspace), "manifest.json")),
    }


def apply(workspace: str, dry_run: bool = False, include_go: bool = True) -> Dict[str, Any]:
    """Wires everything that can be wired without leaving a git trace."""
    results: Dict[str, Any] = {"workspace": workspace, "actions": []}

    config, conf_path, created = generate_config(workspace, dry_run=dry_run)
    results["config"] = {
        "path": conf_path,
        "created": created,
        "mutate": config.get("permissions", {}).get("mutate", True),
    }

    stack = detect_project_stack(workspace)
    frontend = stack.get("frontend") or {}
    if frontend and frontend.get("framework") != "nextjs":
        results["actions"].append({
            "kind": "frontend_zero_footprint",
            "status": "ready",
            "run": "tokensafe dev",
            "note": "no repository files are written; the bridge lives in the dev-server wrapper",
        })
    if frontend.get("framework") == "nextjs":
        results["actions"].append({"kind": "next_route_handler", **apply_next_patch(workspace, dry_run=dry_run)})

    backend = stack.get("backend") or {}
    if backend.get("provider") == "fastapi":
        if dry_run:
            results["actions"].append({"kind": "python_autoload", "status": "would-write", "path": os.path.join(pyhooks_dir(), "sitecustomize.py")})
        else:
            written = ensure_python_autoload()
            results["actions"].append({"kind": "python_autoload", **written, "run": python_run_hint(workspace)})

    if backend.get("provider") == "go" and include_go:
        results["actions"].append({"kind": "go_one_line_patch", **apply_go_patch(workspace, dry_run=dry_run)})
    elif backend.get("provider") == "node":
        results["actions"].append({"kind": "node_one_line_patch", **apply_node_patch(workspace, dry_run=dry_run)})
    elif backend.get("provider") == "dotnet":
        results["actions"].append({"kind": "dotnet_one_line_patch", **apply_dotnet_patch(workspace, dry_run=dry_run)})
    elif backend.get("provider") in ("spring", "php"):
        sidecar_path, note = ensure_sidecar(backend["provider"])
        results["actions"].append({
            "kind": "sidecar",
            "status": "ready" if sidecar_path else "needs-source",
            "sidecar": sidecar_path,
            "note": note,
            "run": sidecar_run_command(backend["provider"], sidecar_path or "", workspace),
        })

    return results


def revert(workspace: str) -> Dict[str, Any]:
    return {
        "workspace": workspace,
        "go": revert_go_patch(workspace),
        "node": revert_node_patch(workspace),
        "dotnet": revert_dotnet_patch(workspace),
        "next": revert_next_patch(workspace),
    }
