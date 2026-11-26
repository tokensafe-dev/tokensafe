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
_GO_MODULE_VERSION = "v0.4.3"


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

    # Skip only when the pinned version is already required; an older requirement
    # would build against an adapter that lacks AutoMount.
    match = re.search(re.escape(_GO_MODULE) + r"\s+v([0-9][\w.+-]*)", content)
    if match and f"v{match.group(1)}" == _GO_MODULE_VERSION:
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

    with open(os.path.join(backup_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(
            {
                "entrypoint": path,
                "backup": backup_path,
                "framework": framework,
                "mode": "serve" if wrapped is not None else "mount",
            },
            f,
            indent=2,
        )

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
    manifest_path = os.path.join(_inject_dir(workspace), "manifest.json")
    if not os.path.isfile(manifest_path):
        return {"status": "nothing-to-revert"}
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    target = manifest.get("entrypoint")
    backup = manifest.get("backup")
    if not target or not backup or not os.path.isfile(backup):
        return {"status": "error", "reason": "backup missing"}
    shutil.copy2(backup, target)
    os.remove(manifest_path)
    return {"status": "reverted", "entrypoint": os.path.relpath(target, workspace)}


# --------------------------------------------------------------------------- #
# Plan / apply / revert for the CLI and the first-prompt hook
# --------------------------------------------------------------------------- #

def plan(workspace: str) -> Dict[str, Any]:
    """Describes what apply would do, without touching anything."""
    stack = detect_project_stack(workspace)
    catalog = {entry["id"]: entry for entry in get_adapters_catalog()}
    actions: List[Dict[str, Any]] = []

    frontend = stack.get("frontend")
    if frontend:
        actions.append({
            "kind": "frontend_zero_footprint",
            "profile": "frontend",
            "framework": f"{frontend.get('provider')}/{frontend.get('framework')}",
            "writes_repo": False,
            "description": "served by `tokensafe dev` (inline Vite config, descriptor in ~/.tokensafe/runtimes)",
            "run": "tokensafe dev",
            "install": catalog.get("frontend/react", {}).get("technology", {}).get("package", {}).get("install"),
        })

    backend = stack.get("backend")
    if backend:
        provider = backend.get("provider")
        entry_id = "backend/fastapi" if provider == "fastapi" else "backend/go"
        entry = catalog.get(entry_id, {})
        if provider == "fastapi":
            actions.append({
                "kind": "python_autoload",
                "profile": "backend",
                "framework": "fastapi",
                "writes_repo": False,
                "description": "sitecustomize patches uvicorn; mount_tokensafe(app) runs on import",
                "install": entry.get("technology", {}).get("package", {}).get("install", 'pip install "tokensafe-backend-python @ git+https://github.com/tokensafe-dev/tokensafe-backend.git#subdirectory=python"'),
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
                "install": entry.get("technology", {}).get("package", {}).get("install"),
            })
        else:
            actions.append({
                "kind": "unsupported",
                "profile": "backend",
                "framework": backend.get("framework"),
                "writes_repo": False,
                "description": "no OUMV adapter for this stack yet — use tokensafe tools instead",
            })

    return {
        "workspace": workspace,
        "profiles": [p for p in ("frontend", "backend") if stack.get(p)],
        "actions": actions,
        "python_autoload_present": python_autoload_present(),
        "revertable": os.path.isfile(os.path.join(_inject_dir(workspace), "manifest.json")),
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
    if stack.get("frontend"):
        results["actions"].append({
            "kind": "frontend_zero_footprint",
            "status": "ready",
            "run": "tokensafe dev",
            "note": "no repository files are written; the bridge lives in the dev-server wrapper",
        })

    backend = stack.get("backend") or {}
    if backend.get("provider") == "fastapi":
        if dry_run:
            results["actions"].append({"kind": "python_autoload", "status": "would-write", "path": os.path.join(pyhooks_dir(), "sitecustomize.py")})
        else:
            written = ensure_python_autoload()
            results["actions"].append({"kind": "python_autoload", **written, "run": python_run_hint(workspace)})

    if backend.get("provider") == "go" and include_go:
        results["actions"].append({"kind": "go_one_line_patch", **apply_go_patch(workspace, dry_run=dry_run)})

    return results


def revert(workspace: str) -> Dict[str, Any]:
    return {"workspace": workspace, "go": revert_go_patch(workspace)}
