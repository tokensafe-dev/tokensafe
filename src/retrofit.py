"""
TokenSafe OUMV Retrofit Engine.
Enables existing frontend and backend projects to adopt the OUMV protocol
incrementally without migrating frameworks or restructuring codebases.
"""

import glob
import os
import json
import time
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

# Top-level directories that hold frontend source in a repo-root app (as opposed to
# scripts/, tooling/ and configs, which are .ts/.js but not OUMV-observable).
_FRONTEND_SOURCE_DIRS = frozenset({
    "src", "app", "pages", "components", "hooks", "lib", "features",
    "views", "stores", "store", "utils", "layouts", "router", "routes", "api",
})


def _detect_package_json_entry(pkg_json_path: str, rel_dir: str = ".") -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], List[str]]:
    """Inspects a single package.json for frontend or Node.js backend stack."""
    fe_out: Optional[Dict[str, Any]] = None
    be_out: Optional[Dict[str, Any]] = None
    managers: List[str] = []
    try:
        with open(pkg_json_path, "r", encoding="utf-8") as f:
            pkg_data = json.load(f)
        deps = {
            **pkg_data.get("dependencies", {}),
            **pkg_data.get("devDependencies", {}),
        }

        state_managers = []
        if "zustand" in deps:
            state_managers.append("zustand")
        if "redux" in deps or "@reduxjs/toolkit" in deps:
            state_managers.append("redux")
        if "pinia" in deps:
            state_managers.append("pinia")
        if "jotai" in deps:
            state_managers.append("jotai")

        fe_info: Dict[str, Any] = {
            "state_managers": state_managers,
            "orm": "prisma" if "@prisma/client" in deps or "prisma" in deps else ("drizzle" if "drizzle-orm" in deps else None),
            "vite": "vite" in deps,
            "root": rel_dir,
        }

        has_vite = "vite" in deps
        if "next" in deps:
            # The App Router is served by the Node runtime as a Route Handler: there is
            # no Vite dev server to patch, and the DOM bridge for client components is
            # separate work. The server profile is what this adapter certifies.
            fe_info.update({
                "framework": "nextjs",
                "provider": "node",
                "profile": "backend",
                "adapter": "tokensafe-backend-node/next",
                "version": deps.get("next"),
                "oumv_supported": True,
                "status": "ready",
                "note": "App Router Route Handler (server profile); client-component DOM bridge is not part of this adapter",
            })
        elif "react" in deps:
            fe_info.update({"framework": "react", "provider": "react", "adapter": "tokensafe-ui/oumv/react", "version": deps.get("react"), "oumv_supported": has_vite, "status": "ready" if has_vite else "needs-vite"})
        elif "vue" in deps:
            fe_info.update({"framework": "vue", "provider": "vue", "adapter": "tokensafe-ui/oumv/vue", "version": deps.get("vue"), "oumv_supported": has_vite, "status": "ready" if has_vite else "needs-vite"})
        elif "solid-js" in deps or "@tokensafe-ui/core" in deps:
            fe_info.update({"framework": "solid", "provider": "solid", "adapter": "tokensafe-ui/core", "version": deps.get("solid-js") or deps.get("@tokensafe-ui/core"), "oumv_supported": False, "status": "planned"})
        elif "svelte" in deps:
            fe_info.update({"framework": "svelte", "provider": "svelte", "adapter": "tokensafe-ui/core", "version": deps.get("svelte"), "oumv_supported": False, "status": "planned"})

        if "framework" in fe_info:
            fe_out = fe_info

        # Node.js backend frameworks (adapter status: planned / unsupported yet)
        node_fw = None
        if "@nestjs/core" in deps:
            node_fw = "nestjs"
        elif "fastify" in deps:
            node_fw = "fastify"
        elif "express" in deps:
            node_fw = "express"
        elif "hono" in deps:
            node_fw = "hono"
        elif "koa" in deps:
            node_fw = "koa"
        elif "elysia" in deps:
            node_fw = "elysia"

        # Bun-only services (queue workers, cron jobs) declare no HTTP framework at
        # all. They used to vanish from `packages` even though their manifest was
        # listed in `detected_files`, so a monorepo looked fully covered when 4 of
        # its 6 packages had no OUMV adapter.
        pkg_dir = os.path.dirname(pkg_json_path)
        bun_marker = (
            any(key in deps for key in ("@types/bun", "bun-types", "elysia"))
            or os.path.isfile(os.path.join(pkg_dir, "bun.lockb"))
            or os.path.isfile(os.path.join(pkg_dir, "bun.lock"))
        )
        if bun_marker:
            managers.append("bun")

        if node_fw:
            supported_fw = node_fw in ("express", "fastify")
            be_out = {
                "framework": node_fw,
                "provider": "bun" if node_fw == "elysia" else "node",
                "adapter": None if node_fw == "elysia" else "tokensafe-backend/node",
                "language": "bun" if node_fw == "elysia" else "node",
                "root": rel_dir,
                # Express/Fastify ship a certified adapter (npm tokensafe-backend-node);
                # Elysia/Nest/Hono/Koa do not yet.
                "oumv_supported": supported_fw,
                "status": "ready" if supported_fw else "planned",
            }
        elif bun_marker and not fe_out:
            # Bun runtime without a web framework: a worker/queue service, not a
            # backend. A frontend package that merely uses Bun as its package
            # manager is not a worker, so `fe_out` wins.
            be_out = {
                "framework": "bun",
                "provider": "bun",
                "adapter": None,
                "language": "bun",
                "root": rel_dir,
                "role": "worker",
                "oumv_supported": False,
                "status": "planned",
            }

        if isinstance(pkg_data.get("workspaces"), (list, dict)):
            managers.append("workspaces")
    except Exception:
        pass
    return fe_out, be_out, managers


def _iter_monorepo_package_dirs(workspace_dir: str) -> List[str]:
    """Returns shallow package subdirectories (depth <= 2) for monorepo stack discovery."""
    skip = {"node_modules", ".git", "dist", "build", ".next", ".venv", "venv", "__pycache__", ".tokensafe"}
    candidates: List[str] = []
    container_names = {"apps", "packages", "services", "frontend", "web", "client", "ui", "backend", "api", "server", "go", "python"}
    try:
        for entry in sorted(os.listdir(workspace_dir)):
            if entry in skip or entry.startswith("."):
                continue
            full = os.path.join(workspace_dir, entry)
            if not os.path.isdir(full):
                continue
            if entry in container_names or os.path.isfile(os.path.join(full, "package.json")) or os.path.isfile(os.path.join(full, "go.mod")) or os.path.isfile(os.path.join(full, "pyproject.toml")):
                candidates.append(full)
                if entry in ("apps", "packages", "services"):
                    try:
                        for sub in sorted(os.listdir(full)):
                            if sub in skip or sub.startswith("."):
                                continue
                            sub_full = os.path.join(full, sub)
                            if os.path.isdir(sub_full):
                                candidates.append(sub_full)
                    except OSError:
                        pass
    except OSError:
        pass
    return candidates


def detect_project_stack(workspace_dir: str) -> Dict[str, Any]:
    """Inspects workspace and monorepo manifests to detect frontend and backend frameworks."""
    stack: Dict[str, Any] = {
        "workspace": workspace_dir,
        "frontend": None,
        "backend": None,
        "package_managers": [],
        "detected_files": [],
        "packages": [],
    }

    def _add_detected(rel_path: str) -> None:
        if rel_path not in stack["detected_files"]:
            stack["detected_files"].append(rel_path)

    def _add_package(entry: Dict[str, Any]) -> None:
        """Adds a package once: the same manifest is reachable from the root scan
        (e.g. python/pyproject.toml) and from monorepo discovery."""
        key = (entry.get("role"), entry.get("path"), entry.get("framework"))
        for existing in stack["packages"]:
            if (existing.get("role"), existing.get("path"), existing.get("framework")) == key:
                return
        stack["packages"].append(entry)

    def _scan_dir(target_dir: str):
        rel = os.path.relpath(target_dir, workspace_dir) or "."
        pkg_json_path = os.path.join(target_dir, "package.json")
        if os.path.isfile(pkg_json_path):
            _add_detected(os.path.relpath(pkg_json_path, workspace_dir))
            fe_info, be_info, pkg_managers = _detect_package_json_entry(pkg_json_path, rel_dir=rel)
            for manager in pkg_managers:
                if manager not in stack["package_managers"]:
                    stack["package_managers"].append(manager)
            if fe_info:
                if not stack["frontend"]:
                    stack["frontend"] = fe_info
                _add_package({
                    "role": "frontend",
                    "path": rel,
                    "framework": fe_info["framework"],
                    "provider": fe_info["provider"],
                    "adapter": fe_info["adapter"],
                    "vite": fe_info.get("vite", False),
                    "oumvSupported": bool(fe_info.get("oumv_supported", True)),
                    "status": fe_info.get("status", "ready"),
                })
            if be_info:
                role = be_info.get("role") or "backend"
                if role == "backend" and not stack["backend"]:
                    stack["backend"] = be_info
                entry = {
                    "role": role,
                    "path": rel,
                    "framework": be_info["framework"],
                    "provider": be_info["provider"],
                    "adapter": be_info["adapter"],
                    "oumvSupported": bool(be_info.get("oumv_supported", False)),
                    "status": be_info.get("status", "planned"),
                }
                if not entry["oumvSupported"]:
                    entry["reason"] = f"no OUMV adapter for {be_info['framework']} yet"
                _add_package(entry)
            if not fe_info and not be_info:
                # Every manifest that was detected must also be accounted for: a
                # package missing from `packages` reads as "not there", not "unsupported".
                _add_package({
                    "role": "package",
                    "path": rel,
                    "framework": "node",
                    "provider": "node",
                    "adapter": None,
                    "oumvSupported": False,
                    "status": "unsupported",
                    "reason": "manifest present, but no supported frontend/backend framework was detected",
                })

        # Go Backend detection
        go_mod_path = os.path.join(target_dir, "go.mod")
        if rel == "." and not os.path.isfile(go_mod_path) and os.path.isfile(os.path.join(target_dir, "go", "go.mod")):
            go_mod_path = os.path.join(target_dir, "go", "go.mod")
        if os.path.isfile(go_mod_path):
            rel_mod = os.path.relpath(go_mod_path, workspace_dir)
            _add_detected(rel_mod)
            if "go" not in stack["package_managers"]:
                stack["package_managers"].append("go")
            framework = "net/http"
            try:
                with open(go_mod_path, "r", encoding="utf-8") as f:
                    content = f.read()
                if "github.com/gin-gonic/gin" in content:
                    framework = "gin"
                elif "github.com/go-chi/chi" in content:
                    framework = "chi"
                elif "github.com/labstack/echo" in content:
                    framework = "echo"
            except Exception:
                pass
            go_entry = {
                "framework": framework,
                "provider": "go",
                "adapter": "tokensafe-backend/go",
                "language": "go",
                "root": os.path.dirname(rel_mod) or ".",
                "oumv_supported": True,
                "status": "ready",
            }
            if not stack["backend"] or not stack["backend"].get("oumv_supported", False):
                stack["backend"] = go_entry
            _add_package({
                "role": "backend",
                "path": go_entry["root"],
                "framework": framework,
                "provider": "go",
                "adapter": "tokensafe-backend/go",
                "oumvSupported": True,
                "status": "ready",
            })

        # Python Backend detection
        py_files = [
            os.path.join(target_dir, "pyproject.toml"),
            os.path.join(target_dir, "requirements.txt"),
        ]
        if rel == ".":
            py_files.insert(1, os.path.join(target_dir, "python", "pyproject.toml"))
        for pf in py_files:
            if os.path.isfile(pf):
                rel_pf = os.path.relpath(pf, workspace_dir)
                _add_detected(rel_pf)
                if "pip/python" not in stack["package_managers"]:
                    stack["package_managers"].append("pip/python")
                try:
                    with open(pf, "r", encoding="utf-8") as f:
                        c = f.read().lower()
                    framework = "python"
                    if "fastapi" in c:
                        framework = "fastapi"
                    elif "django" in c:
                        framework = "django"
                    elif "flask" in c:
                        framework = "flask"
                    elif "starlette" in c:
                        framework = "starlette"
                    supported = framework in ("fastapi",)
                    py_entry = {
                        "framework": framework,
                        "provider": "fastapi" if framework == "fastapi" else "python",
                        "adapter": "tokensafe-backend/python",
                        "language": "python",
                        "root": os.path.dirname(rel_pf) or ".",
                        "oumv_supported": supported,
                        "status": "ready" if supported else "planned",
                    }
                    # A supported Python backend outranks an unsupported Node one,
                    # mirroring the Go precedence rule above.
                    if not stack["backend"] or not stack["backend"].get("oumv_supported", False):
                        stack["backend"] = py_entry
                    _add_package({
                        "role": "backend",
                        "path": py_entry["root"],
                        "framework": framework,
                        "provider": py_entry["provider"],
                        "adapter": "tokensafe-backend/python",
                        "oumvSupported": supported,
                        "status": py_entry["status"],
                    })
                except Exception:
                    pass
                break

        # Spring (JDK-only sidecar) and PHP (Laravel/Symfony sidecar) detection.
        # Both are source-level adapters: nothing is written into the project.
        for java_manifest in ("pom.xml", "build.gradle", "build.gradle.kts"):
            java_path = os.path.join(target_dir, java_manifest)
            if not os.path.isfile(java_path):
                continue
            try:
                with open(java_path, "r", encoding="utf-8", errors="replace") as f:
                    java_content = f.read().lower()
            except Exception:
                continue
            if "spring-boot" in java_content or "org.springframework" in java_content:
                _add_detected(os.path.relpath(java_path, workspace_dir))
                sidecar = {
                    "framework": "spring",
                    "provider": "spring",
                    "adapter": "tokensafe-backend/java",
                    "language": "java",
                    "root": os.path.dirname(os.path.relpath(java_path, workspace_dir)) or ".",
                    "oumv_supported": True,
                    "status": "ready",
                }
                if not stack["backend"] or not stack["backend"].get("oumv_supported", False):
                    stack["backend"] = sidecar
                _add_package({
                    "role": "backend",
                    "path": sidecar["root"],
                    "framework": "spring",
                    "provider": "spring",
                    "adapter": "tokensafe-backend/java",
                    "oumvSupported": True,
                    "status": "ready",
                })
                break

        # ASP.NET Core detection: Microsoft.NET.Sdk.Web or the ASP.NET Core
        # framework reference in a csproj.
        for csproj in sorted(glob.glob(os.path.join(target_dir, "**", "*.csproj"), recursive=True)):
            if any(part in ("bin", "obj", "node_modules") for part in Path(csproj).parts):
                continue
            try:
                with open(csproj, "r", encoding="utf-8", errors="replace") as f:
                    csproj_content = f.read()
            except Exception:
                continue
            if "Microsoft.NET.Sdk.Web" not in csproj_content and "Microsoft.AspNetCore.App" not in csproj_content:
                continue
            rel_csproj = os.path.relpath(csproj, workspace_dir)
            _add_detected(rel_csproj)
            if "nuget/dotnet" not in stack["package_managers"]:
                stack["package_managers"].append("nuget/dotnet")
            dotnet_entry = {
                "framework": "aspnetcore",
                "provider": "dotnet",
                "adapter": "tokensafe-backend/dotnet",
                "language": "csharp",
                "root": os.path.dirname(rel_csproj) or ".",
                "oumv_supported": True,
                "status": "ready",
            }
            if not stack["backend"] or not stack["backend"].get("oumv_supported", False):
                stack["backend"] = dotnet_entry
            _add_package({
                "role": "backend",
                "path": dotnet_entry["root"],
                "framework": "aspnetcore",
                "provider": "dotnet",
                "adapter": "tokensafe-backend/dotnet",
                "oumvSupported": True,
                "status": "ready",
            })
            break

        composer_path = os.path.join(target_dir, "composer.json")
        if os.path.isfile(composer_path):
            try:
                with open(composer_path, "r", encoding="utf-8") as f:
                    composer = json.load(f)
                requires = {**(composer.get("require") or {}), **(composer.get("require-dev") or {})}
            except Exception:
                requires = {}
            php_framework = (
                "laravel"
                if any(str(name).startswith("laravel/") for name in requires)
                else ("symfony" if any(str(name).startswith("symfony/") for name in requires) else None)
            )
            if php_framework:
                _add_detected("composer.json")
                sidecar = {
                    "framework": php_framework,
                    "provider": "php",
                    "adapter": "tokensafe-backend/php",
                    "language": "php",
                    "root": ".",
                    "oumv_supported": True,
                    "status": "ready",
                }
                if not stack["backend"] or not stack["backend"].get("oumv_supported", False):
                    stack["backend"] = sidecar
                _add_package({
                    "role": "backend",
                    "path": ".",
                    "framework": php_framework,
                    "provider": "php",
                    "adapter": "tokensafe-backend/php",
                    "oumvSupported": True,
                    "status": "ready",
                })

    _scan_dir(workspace_dir)
    if os.path.isfile(os.path.join(workspace_dir, "pnpm-workspace.yaml")):
        _add_detected("pnpm-workspace.yaml")

    for lock, pm in [("pnpm-lock.yaml", "pnpm"), ("bun.lockb", "bun"), ("bun.lock", "bun"), ("yarn.lock", "yarn"), ("package-lock.json", "npm")]:
        if os.path.isfile(os.path.join(workspace_dir, lock)):
            if pm not in stack["package_managers"]:
                stack["package_managers"].append(pm)
            if lock not in stack["detected_files"]:
                stack["detected_files"].append(lock)

    for sub_dir in _iter_monorepo_package_dirs(workspace_dir):
        _scan_dir(sub_dir)

    return stack


def generate_config(
    workspace_dir: str,
    stack: Optional[Dict[str, Any]] = None,
    dry_run: bool = False,
    force: bool = False,
) -> Tuple[Dict[str, Any], str, bool]:
    """
    Generates non-destructive, idempotent .tokensafe/oumv.json manifest.
    Default mutation policy is enabled out-of-the-box (mutate: true).
    """
    detected = stack or detect_project_stack(workspace_dir)
    target_dir = os.path.join(workspace_dir, ".tokensafe")
    target_file = os.path.join(target_dir, "oumv.json")

    profiles = []
    adapters: Dict[str, Any] = {}

    if detected.get("frontend"):
        fe = detected["frontend"]
        profiles.append("frontend")
        adapters["frontend"] = {
            "provider": fe.get("provider", "react"),
            "framework": fe.get("framework", "react"),
            "mode": "hybrid",
        }

    if detected.get("backend"):
        be = detected["backend"]
        profiles.append("backend")
        adapters["backend"] = {
            "provider": be.get("provider", "go"),
            "framework": be.get("framework", "go"),
            "mode": "hybrid",
        }

    project_name = os.path.basename(os.path.abspath(workspace_dir))
    config: Dict[str, Any] = {
        "version": 1,
        "project": {
            "name": project_name,
        },
        "oumv": {
            "enabled": True,
            "profiles": profiles or ["frontend", "backend"],
        },
        "adapters": adapters,
        "permissions": {
            "observe": True,
            "mutate": True,
            "productionRuntime": False,
        },
    }

    if os.path.isfile(target_file) and not force:
        try:
            with open(target_file, "r", encoding="utf-8") as f:
                existing = json.load(f)
            return existing, target_file, False
        except Exception:
            pass

    if not dry_run:
        os.makedirs(target_dir, exist_ok=True)
        with open(target_file, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
        # The manifest must never show up as an untracked change: ignore it via
        # .git/info/exclude (local-only) instead of editing the tracked .gitignore.
        try:
            from src.paths import ensure_local_git_exclude
            ensure_local_git_exclude(workspace_dir)
        except Exception:
            pass

    return config, target_file, True


def get_adapters_catalog() -> List[Dict[str, Any]]:
    """Lists available OUMV adapters in the consolidated architecture.

    Each entry follows the language-neutral OUMV Adapter Contract
    (agent-oumv/schemas/adapter.json): identity, technology/package,
    profiles, and baseline capabilities with constraints.
    """
    return [
        {
            "id": "frontend/react",
            "name": "React Adapter (Zustand, Redux, Context, useReducer)",
            "profile": "frontend",
            "profiles": ["frontend"],
            "repo": "tokensafe-ui",
            "mode": "hybrid",
            "status": "ready",
            "description": "Progressive AST component discovery + runtime action/state inspection",
            "technology": {
                "language": "typescript",
                "framework": "react",
                "package": {"manager": "npm", "name": "tokensafe-ui", "install": "npm install tokensafe-ui"},
            },
            "capabilities": [
                {"name": "observe.resources", "constraints": {"readOnly": True, "developmentOnly": False}},
                {"name": "observe.runtime_state", "constraints": {"readOnly": True, "developmentOnly": True}},
                {"name": "understand.impact", "constraints": {"readOnly": True, "developmentOnly": False}},
                {"name": "mutate.source", "constraints": {"readOnly": False, "developmentOnly": False, "requiresPermission": "permissions.mutate != false"}},
                {"name": "verify.runtime_behavior", "constraints": {"readOnly": True, "developmentOnly": True}},
            ],
        },
        {
            "id": "frontend/solid",
            "name": "SolidJS / TokenSafe UI Native Adapter",
            "profile": "frontend",
            "profiles": ["frontend"],
            "repo": "tokensafe-ui",
            "mode": "hybrid",
            "status": "planned",
            "description": "Planned: fine-grained reactive signals causality + virtual DOM observer. No Solid runtime adapter is implemented yet (the tokensafe-ui plugin ships React and Vue); a Solid repo must use static analysis tools instead",
            "technology": {
                "language": "typescript",
                "framework": "solidjs",
                "package": {"manager": "npm", "name": "tokensafe-ui", "install": "npm install tokensafe-ui"},
            },
            "capabilities": [],
        },
        {
            "id": "frontend/vue",
            "name": "Vue 3 Adapter (Composition API, Pinia)",
            "profile": "frontend",
            "profiles": ["frontend"],
            "repo": "tokensafe-ui",
            "mode": "hybrid",
            "status": "ready",
            "description": "Vue 3 runtime adapter: tokensafe-ui() mounts /__oumv/rpc in the Vite dev server without touching the repo, reads the app instance through the bridge and is certified Level 3 in a real browser (agent-dom/certification/vue-vite-browser.json)",
            "technology": {
                "language": "typescript",
                "framework": "vue",
                "package": {"manager": "npm", "name": "tokensafe-ui", "install": "npm install -D tokensafe-ui"},
            },
            "capabilities": [
                {"name": "observe.resources", "constraints": {"readOnly": True, "developmentOnly": False}},
                {"name": "observe.runtime_state", "constraints": {"readOnly": True, "developmentOnly": True}},
                {"name": "mutate.source", "constraints": {"readOnly": False, "developmentOnly": False, "requiresPermission": "permissions.mutate != false"}},
            ],
        },
        {
            "id": "backend/go",
            "name": "Go Backend Adapter (Gin, Chi, net/http)",
            "profile": "backend",
            "profiles": ["backend"],
            "repo": "tokensafe-backend/go",
            "mode": "hybrid",
            "status": "ready",
            "description": "Go AST pre-flight compiler guard + optional database/sql sandbox: verify.sandboxRollback runs the request in one transaction and rolls it back (opt-in via SetDBSandbox)",
            "technology": {
                "language": "go",
                "framework": "gin/chi/net-http",
                "package": {
                    "manager": "go",
                    "name": "github.com/tokensafe-dev/tokensafe-backend/go",
                    "install": "go get github.com/tokensafe-dev/tokensafe-backend/go",
                },
            },
            "capabilities": [
                {"name": "observe.resources", "constraints": {"readOnly": True, "developmentOnly": False}},
                {"name": "observe.runtime_state", "constraints": {"readOnly": True, "developmentOnly": False}},
                {"name": "understand.causality", "constraints": {"readOnly": True, "developmentOnly": False, "limitations": ["requires observed request traces (TraceHandler or oumv/verify)"]}},
                {"name": "mutate.source", "constraints": {"readOnly": False, "developmentOnly": False, "requiresPermission": "permissions.mutate != false"}},
                {"name": "verify.runtime_behavior", "constraints": {"readOnly": True, "developmentOnly": False}},
            ],
        },
        {
            "id": "backend/fastapi",
            "name": "Python FastAPI Adapter (Starlette, Pydantic)",
            "profile": "backend",
            "profiles": ["backend"],
            "repo": "tokensafe-backend/python",
            "mode": "hybrid",
            "status": "ready",
            "description": "FastAPI automatic route & schema introspection + SQLAlchemy sandbox: verify.sandboxRollback needs a SQLAlchemy engine and an app that honours X-TokenSafe-Dry-Run; otherwise the result says not_applicable/unverified",
            "technology": {
                "language": "python",
                "framework": "fastapi",
                "package": {"manager": "pip", "name": "tokensafe-backend-python", "install": 'pip install "tokensafe-backend-python @ git+https://github.com/tokensafe-dev/tokensafe-backend.git#subdirectory=python"'},
            },
            "capabilities": [
                {"name": "observe.resources", "constraints": {"readOnly": True, "developmentOnly": False}},
                {"name": "understand.causality", "constraints": {"readOnly": True, "developmentOnly": False}},
                {"name": "mutate.source", "constraints": {"readOnly": False, "developmentOnly": False, "requiresPermission": "permissions.mutate != false"}},
                {"name": "verify.runtime_behavior", "constraints": {"readOnly": True, "developmentOnly": False}},
            ],
        },
        {
            "id": "backend/node",
            "name": "Node.js Backend Adapter (Express, Fastify)",
            "profile": "backend",
            "profiles": ["backend"],
            "repo": "tokensafe-backend/node",
            "mode": "hybrid",
            "status": "ready",
            "description": "Express/Fastify runtime: route reflection, OCC source edits, loopback verify, SSE/WS (certified Level 3)",
            "technology": {
                "language": "typescript",
                "framework": "express | fastify",
                "package": {"manager": "npm", "name": "tokensafe-backend-node", "install": "npm install tokensafe-backend-node"},
            },
            "capabilities": [
                {"name": "observe.pulse", "constraints": {"readOnly": True, "developmentOnly": True}},
                {"name": "observe.surface", "constraints": {"readOnly": True, "developmentOnly": True}},
                {"name": "observe.contract", "constraints": {"readOnly": True, "developmentOnly": True}},
                {"name": "observe.source", "constraints": {"readOnly": True, "developmentOnly": True}},
                {"name": "observe.traces", "constraints": {"readOnly": True, "developmentOnly": True}},
                {"name": "understand.impact", "constraints": {"readOnly": True, "developmentOnly": True}},
                {"name": "understand.causality", "constraints": {"readOnly": True, "developmentOnly": True}},
                {"name": "mutate.source", "constraints": {"readOnly": False, "developmentOnly": True, "requiresPermission": "permissions.mutate != false"}},
                {"name": "verify.behavior", "constraints": {"readOnly": True, "developmentOnly": True}},
            ],
        },
        {
            "id": "backend/next",
            "name": "Next.js App Router Adapter (Route Handler, server profile)",
            "profile": "backend",
            "profiles": ["backend"],
            "repo": "tokensafe-backend/node",
            "mode": "hybrid",
            "status": "ready",
            "description": "App Router Route Handler (app/api/tokensafe/route.ts) serving JSON-RPC + SSE over the Node runtime; route surface read from app/**/route.{ts,tsx,js,jsx}, verify dispatched through an adopter-supplied Web handler; the client-component DOM bridge is separate work (certified Level 3)",
            "technology": {
                "language": "typescript",
                "framework": "next (app router)",
                "package": {"manager": "npm", "name": "tokensafe-backend-node", "install": "npm install --save-dev tokensafe-backend-node"},
            },
            "capabilities": [
                {"name": "observe.pulse", "constraints": {"readOnly": True, "developmentOnly": True}},
                {"name": "observe.surface", "constraints": {"readOnly": True, "developmentOnly": True, "note": "app/**/route.* scan"}},
                {"name": "observe.source", "constraints": {"readOnly": True, "developmentOnly": True}},
                {"name": "observe.traces", "constraints": {"readOnly": True, "developmentOnly": True, "note": "requires verifyFetch wiring"}},
                {"name": "understand.impact", "constraints": {"readOnly": True, "developmentOnly": True}},
                {"name": "mutate.source", "constraints": {"readOnly": False, "developmentOnly": True, "requiresPermission": "permissions.mutate != false"}},
                {"name": "verify.behavior", "constraints": {"readOnly": True, "developmentOnly": True, "note": "requires verifyFetch wiring"}},
            ],
        },
        {
            "id": "backend/aspnetcore",
            "name": "ASP.NET Core Adapter (minimal APIs, MVC controllers, EF Core)",
            "profile": "backend",
            "profiles": ["backend"],
            "repo": "tokensafe-backend/dotnet",
            "mode": "hybrid",
            "status": "ready",
            "description": "In-process endpoint dispatch for minimal APIs and controllers, Roslyn syntax pre-flight, the full wire 1.6.0 surface (previewEdit, index, query, build, cancel), and verify.sandboxRollback that rolls every auto-discovered EF Core DbContext back",
            "technology": {
                "language": "csharp",
                "framework": "aspnetcore",
                "package": {
                    "manager": "nuget",
                    "name": "TokenSafe.Oumv.AspNetCore",
                    "install": "dotnet add package TokenSafe.Oumv.AspNetCore",
                },
            },
            "capabilities": [
                {"name": "observe.resources", "constraints": {"readOnly": True, "developmentOnly": False}},
                {"name": "observe.runtime_state", "constraints": {"readOnly": True, "developmentOnly": False}},
                {"name": "understand.causality", "constraints": {"readOnly": True, "developmentOnly": False, "limitations": ["requires observed request traces (UseTokensafe middleware or oumv/verify)"]}},
                {"name": "mutate.source", "constraints": {"readOnly": False, "developmentOnly": False, "requiresPermission": "permissions.mutate != false"}},
                {"name": "mutate.preview", "constraints": {"readOnly": True, "developmentOnly": True, "requiresPermission": "permissions.mutate != false", "limitations": ["dry run of applyEdit's guard chain; nothing is written"]}},
                {"name": "index.symbols", "constraints": {"readOnly": True, "developmentOnly": True}},
                {"name": "observe.query", "constraints": {"readOnly": True, "developmentOnly": True, "limitations": ["read-only SQL over the discovered DbContext; writes answer -32003"]}},
                {"name": "verify.build", "constraints": {"readOnly": True, "developmentOnly": True, "limitations": ["OumvOptions.BuildCommand or params.command; honest skipped without one"]}},
                {"name": "sessions.cancel", "constraints": {"readOnly": True, "developmentOnly": False}},
                {"name": "verify.runtime_behavior", "constraints": {"readOnly": True, "developmentOnly": False}},
            ],
        },
        {
            "id": "backend/spring",
            "name": "Spring Boot Adapter (JDK-only sidecar)",
            "profile": "backend",
            "profiles": ["backend"],
            "repo": "tokensafe-backend/java",
            "mode": "sidecar",
            "status": "ready",
            "description": "Single-file JDK sidecar: scans @RestController mappings statically, serves HTTP and stdio, applies OCC edits and runs a real verify command. Certified Level 2 32/33 (no live request traces, L4 not advertised). Frozen at wire 1.5.0 on purpose: the handshake negotiates 1.5.0 until the 1.6.0 surface is worth implementing.",
            "technology": {
                "language": "java",
                "framework": "spring",
                "package": {
                    "manager": "binary",
                    "name": "OumvSpring.java",
                    "install": "tokensafe inject apply (caches the sidecar in ~/.tokensafe/sidecars)",
                },
            },
            "capabilities": [
                {"name": "observe.resources", "constraints": {"readOnly": True, "developmentOnly": False}},
                {"name": "observe.runtime_state", "constraints": {"readOnly": True, "developmentOnly": False}},
                {"name": "understand.impact", "constraints": {"readOnly": True, "developmentOnly": False}},
                {"name": "mutate.source", "constraints": {"readOnly": False, "developmentOnly": False, "requiresPermission": "permissions.mutate != false"}},
                {"name": "verify.behavior", "constraints": {"readOnly": True, "developmentOnly": False}},
            ],
        },
        {
            "id": "backend/php",
            "name": "Laravel / Symfony Adapter (PHP sidecar)",
            "profile": "backend",
            "profiles": ["backend"],
            "repo": "tokensafe-backend/php",
            "mode": "sidecar",
            "status": "ready",
            "description": "Dependency-free PHP sidecar: Laravel Route:: and Symfony #[Route] discovery, token_get_all handler ranges, php -l pre-flight, OCC edits and stdio. Certified Level 2 32/33 (no live traces). Frozen at wire 1.5.0 on purpose: the handshake negotiates 1.5.0 until the 1.6.0 surface is worth implementing.",
            "technology": {
                "language": "php",
                "framework": "laravel | symfony",
                "package": {
                    "manager": "binary",
                    "name": "oumv-php",
                    "install": "tokensafe inject apply (caches the sidecar in ~/.tokensafe/sidecars)",
                },
            },
            "capabilities": [
                {"name": "observe.resources", "constraints": {"readOnly": True, "developmentOnly": False}},
                {"name": "observe.runtime_state", "constraints": {"readOnly": True, "developmentOnly": False}},
                {"name": "mutate.source", "constraints": {"readOnly": False, "developmentOnly": False, "requiresPermission": "permissions.mutate != false"}},
                {"name": "verify.behavior", "constraints": {"readOnly": True, "developmentOnly": False}},
            ],
        },
    ]


def _adapter_presence(workspace_dir: str, profile: str, provider: str, framework: str) -> Dict[str, Any]:
    """Best-effort, side-effect-free check whether an adapter is available locally.

    Returns {"installed": bool, "evidence": str} where evidence names what was
    actually found — never a bare True.
    """
    ws = os.path.abspath(workspace_dir)
    if profile == "frontend":
        pkg_json = os.path.join(ws, "node_modules", "tokensafe-ui", "package.json")
        if os.path.isfile(pkg_json):
            return {"installed": True, "evidence": "node_modules/tokensafe-ui"}
        return {"installed": False, "evidence": "node_modules/tokensafe-ui missing (run: npm install -D tokensafe-ui)"}

    if provider == "fastapi":
        try:
            import importlib.util
            if importlib.util.find_spec("tokensafe_python") is not None:
                return {"installed": True, "evidence": "tokensafe_python importable"}
        except Exception:
            pass
        for req in ("requirements.txt", "pyproject.toml"):
            path = os.path.join(ws, req)
            try:
                if os.path.isfile(path):
                    with open(path, encoding="utf-8", errors="replace") as fh:
                        if "tokensafe" in fh.read():
                            return {"installed": True, "evidence": f"{req} declares tokensafe"}
            except Exception:
                pass
        return {"installed": False, "evidence": 'tokensafe_python not importable (run: pip install "tokensafe-backend-python @ git+https://github.com/tokensafe-dev/tokensafe-backend.git#subdirectory=python")'}

    if provider == "go":
        for rel in ("go.mod", os.path.join("go", "go.mod")):
            path = os.path.join(ws, rel)
            try:
                if os.path.isfile(path):
                    with open(path, encoding="utf-8", errors="replace") as fh:
                        if "tokensafe-backend/go" in fh.read():
                            return {"installed": True, "evidence": f"{rel} requires tokensafe-backend/go"}
            except Exception:
                pass
        return {"installed": False, "evidence": "tokensafe-backend/go not in go.mod (run: go get github.com/tokensafe-dev/tokensafe-backend/go@vX.Y.Z)"}

    return {"installed": False, "evidence": f"no adapter published for {provider}/{framework}"}


def run_doctor(workspace_dir: str) -> Dict[str, Any]:
    """Diagnostics for OUMV configuration, detected adapters and agent readiness."""
    stack = detect_project_stack(workspace_dir)
    conf_path = os.path.join(workspace_dir, ".tokensafe", "oumv.json")
    runtime_paths = [
        os.path.join(workspace_dir, ".tokensafe", "runtime.json"),
    ]
    try:
        from src.oumv_gateway import OUMVGateway
        runtime_paths.insert(0, OUMVGateway.global_runtime_file(workspace_dir))
    except Exception:
        pass
    runtime_path = next((p for p in runtime_paths if os.path.isfile(p)), runtime_paths[-1])

    has_config = os.path.isfile(conf_path)
    config_data: Dict[str, Any] = {}
    if has_config:
        try:
            with open(conf_path, "r", encoding="utf-8") as f:
                config_data = json.load(f)
        except Exception:
            pass

    has_runtime = os.path.isfile(runtime_path)
    runtime_data: Dict[str, Any] = {}
    if has_runtime:
        try:
            with open(runtime_path, "r", encoding="utf-8") as f:
                runtime_data = json.load(f)
        except Exception:
            pass

    # Per-profile readiness: a config file alone is not "ready for agent".
    readiness: Dict[str, Any] = {}
    for profile, key in (("frontend", "frontend"), ("backend", "backend")):
        detected = stack.get(key)
        if not detected:
            continue
        provider = detected.get("provider", "")
        framework = detected.get("framework", "")
        presence = _adapter_presence(workspace_dir, profile, provider, framework)
        live = has_runtime and runtime_data.get("type", profile) in (profile, None)
        readiness[profile] = {
            "provider": provider,
            "framework": framework,
            "installed": presence["installed"],
            "evidence": presence["evidence"],
            "live": bool(live),
            "ready": bool(presence["installed"] or live),
        }

    permissions = config_data.get("permissions", {"observe": True, "mutate": True})
    config_enabled = bool(config_data.get("oumv", {}).get("enabled", has_config))
    ready_for_agent = bool(
        has_config
        and config_enabled
        and permissions.get("observe", True)
        and all(item["ready"] for item in readiness.values())
    )

    return {
        "workspace": workspace_dir,
        "detected_stack": stack,
        "config_present": has_config,
        "config": config_data,
        "runtime_beacon_present": has_runtime,
        "runtime_beacon": runtime_data,
        "permissions": permissions,
        "adapter_readiness": readiness,
        "ready_for_agent": ready_for_agent,
        "blocking": [
            f"{profile}: {item['evidence']}"
            for profile, item in readiness.items()
            if not item["ready"]
        ],
    }


class OUMVProjectContext:
    """
    Automatic out-of-the-box OUMV Project Context & Adapter Resolver.
    Inspects project manifests, resolves active adapters (frontend & backend),
    and provisions runtime or static AST inspection seamlessly.
    """
    _cache: Dict[str, "OUMVProjectContext"] = {}
    _last_checked: float = 0.0

    def __init__(self, workspace_dir: str):
        self.workspace_dir = os.path.abspath(workspace_dir)
        self.stack = detect_project_stack(self.workspace_dir)
        self.active_adapters = self._resolve_adapters()

    @classmethod
    def get_for_workspace(cls, workspace_dir: Optional[str] = None) -> "OUMVProjectContext":
        from src.paths import get_active_workspace
        ws = os.path.abspath(workspace_dir or get_active_workspace())
        now = time.time()
        if ws not in cls._cache or (now - cls._last_checked > 10.0):
            cls._cache[ws] = cls(ws)
            cls._last_checked = now
        return cls._cache[ws]

    def _resolve_adapters(self) -> Dict[str, Any]:
        adapters: Dict[str, Any] = {}
        fe = self.stack.get("frontend")
        if fe:
            adapters["frontend"] = {
                "name": f"{fe.get('framework', 'React').title()} Adapter",
                "profile": "frontend",
                "repo": "tokensafe-ui",
                "adapter": fe.get("adapter", "tokensafe-ui/oumv/react"),
                "state_managers": fe.get("state_managers", []),
                "orm": fe.get("orm"),
                "mode": "hybrid",
            }

        be = self.stack.get("backend")
        if be:
            adapters["backend"] = {
                "name": f"{be.get('framework', 'Backend').title()} Adapter",
                "profile": "backend",
                "repo": "tokensafe-backend",
                "adapter": be.get("adapter", "tokensafe-backend"),
                "language": be.get("language"),
                "mode": "hybrid",
            }
        return adapters

    def resolve_target_runtime(self, target: Optional[str] = None) -> str:
        """Determines whether a target belongs to the frontend or backend profile."""
        if not target:
            return "auto"
        # Backend hints
        if any(target.startswith(m) for m in ["GET ", "POST ", "PUT ", "DELETE ", "PATCH "]) or "/api/" in target or target.startswith("/"):
            return "backend"
        # Frontend hints
        if any(target.startswith(c) for c in ["#", ".", "[", "button", "input"]) or target.startswith("<") or target.endswith(".tsx") or target.endswith(".jsx"):
            return "frontend"
        return "auto"


_UI_CLI_CACHE: Dict[str, Tuple[float, Optional[Tuple[List[str], str]]]] = {}
_UI_CLI_CACHE_TTL: float = 60.0


def _resolve_tokensafe_ui_cli_uncached(workspace_dir: str) -> Optional[Tuple[List[str], str]]:
    """Finds an installed tokensafe-ui CLI without touching the network.

    Returns (argv prefix, human source) or None when the companion CLI is absent.
    """
    import shutil
    import subprocess

    ws = os.path.abspath(workspace_dir)

    # 1. Explicit override wins: lets CI pin a checkout without a global install.
    env_cli = os.environ.get("TOKENSAFE_UI_CLI")
    if env_cli and os.path.isfile(env_cli):
        return (["node", env_cli], "TOKENSAFE_UI_CLI")

    # 2. Local project node_modules/.bin/tokensafe-ui (if present)
    local_bin = os.path.join(ws, "node_modules", ".bin", "tokensafe-ui")
    if os.path.isfile(local_bin):
        return ([local_bin], "local node_modules")

    # 3. Global tokensafe-ui binary on PATH
    global_bin = shutil.which("tokensafe-ui")
    if global_bin:
        return ([global_bin], "global PATH")

    # 4. npm global root package (nvm-style installs whose bin dir is not on PATH)
    try:
        res = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True, timeout=3)
        if res.returncode == 0:
            cli_js = os.path.join(res.stdout.strip(), "tokensafe-ui", "bin", "cli.js")
            if os.path.isfile(cli_js):
                return (["node", cli_js], "npm global root")
    except Exception:
        pass
    return None


def resolve_tokensafe_ui_cli(workspace_dir: str) -> Optional[Tuple[List[str], str]]:
    """Cached resolver for the tokensafe-ui CLI (60s TTL per workspace).

    Called from the command sanitizer, which runs per shell command; the npm-root
    probe alone costs ~200 ms, so it must not repeat on every invocation.
    """
    ws = os.path.abspath(workspace_dir)
    now = time.time()
    hit = _UI_CLI_CACHE.get(ws)
    if hit and (now - hit[0]) < _UI_CLI_CACHE_TTL:
        return hit[1]
    resolved = _resolve_tokensafe_ui_cli_uncached(ws)
    _UI_CLI_CACHE[ws] = (now, resolved)
    return resolved


def resolve_zero_footprint_vite_command(
    workspace_dir: str,
    port: Optional[int] = None,
    host: Optional[str] = None,
    open_browser: bool = False,
) -> Tuple[List[str], str]:
    """Resolves the zero-footprint tokensafe-ui Vite runner command without editing repo files."""
    import shutil

    ws = os.path.abspath(workspace_dir)
    cli_flags: List[str] = []
    if port is not None:
        cli_flags.append(f"--port={port}")
    if host:
        cli_flags.append(f"--host={host}")
    if open_browser:
        cli_flags.append("--open")
    # `dev` defaults to React; a Vue project must say so explicitly or it gets the
    # React controller and bridge.
    stack = detect_project_stack(ws)
    frontend = stack.get("frontend") or {}
    framework = frontend.get("framework") if isinstance(frontend, dict) else None
    if framework in ("react", "vue"):
        cli_flags.append(f"--framework={framework}")

    resolved = resolve_tokensafe_ui_cli(ws)
    if resolved:
        prefix, source = resolved
        return ([*prefix, "dev", ws, *cli_flags], source)

    # Explicit `tokensafe dev` may bootstrap the companion package via npx; the
    # command sanitizer never rewrites to this path (see CommandSanitizer).
    npx_bin = shutil.which("npx") or "npx"
    return ([npx_bin, "--yes", "tokensafe-ui", "dev", ws, *cli_flags], "npx tokensafe-ui")


def format_oumv_workspace_brief(workspace_dir: str, include_live: bool = False) -> str:
    """Builds a compact OUMV capability matrix banner for SessionStart and tokensafe-proxy.

    Explicitly separates supported profiles (React+Vite, Vue, Solid, Go, FastAPI)
    from unsupported stacks (e.g. Node.js express/fastify/nest in a monorepo), so agents
    use OUMV where available without wasting turns probing unsupported subprojects.
    """
    if not workspace_dir or not os.path.isdir(workspace_dir):
        return ""
    stack = detect_project_stack(workspace_dir)
    packages = stack.get("packages") or []
    parts: List[str] = []
    seen_labels = set()

    for pkg in packages:
        loc = f" ({pkg['path']})" if pkg.get("path") and pkg["path"] != "." else ""
        label = f"{pkg.get('provider')}/{pkg.get('framework')}{loc}"
        if label in seen_labels:
            continue
        seen_labels.add(label)
        if pkg.get("oumvSupported"):
            run_hint = "; start live bridge via 'tokensafe dev'" if pkg.get("vite") else ""
            parts.append(
                f"{label}: OUMV SUPPORTED (use tokensafe:oumv observe/understand/mutate{run_hint})"
            )
        else:
            parts.append(
                f"{label}: OUMV UNSUPPORTED yet (planned — use repo_map/smart_read_file/scoped_search instead of oumv)"
            )

    if not parts:
        return ""

    if include_live:
        try:
            from src.oumv_gateway import OUMVGateway
            fe, be = OUMVGateway.get_endpoints(workspace_dir=workspace_dir)
            live_bits = []
            if fe:
                live_bits.append(f"FE LIVE {fe.get('url')}")
            if be:
                live_bits.append(f"BE LIVE {be.get('url')}")
            if live_bits:
                parts.append("LIVE RUNTIMES: " + ", ".join(live_bits) + " -> MUST inspect/verify via tokensafe:oumv")
        except Exception:
            pass

    return (
        "[TokenSafe OUMV Workspace Matrix @ "
        + (os.path.basename(os.path.abspath(workspace_dir)) or workspace_dir)
        + " | "
        + " | ".join(parts)
        + "]"
    )


_STACK_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_STACK_CACHE_TTL: float = 60.0


def detect_project_stack_cached(workspace_dir: str, ttl: float = _STACK_CACHE_TTL) -> Dict[str, Any]:
    """Short-TTL cache for hot paths (read/edit hooks run per tool call).

    Direct callers keep using detect_project_stack() so tests that mutate
    manifests between calls always see fresh results.
    """
    ws = os.path.realpath(os.path.abspath(workspace_dir))
    now = time.time()
    hit = _STACK_CACHE.get(ws)
    if hit and (now - hit[0]) < ttl:
        return hit[1]
    stack = detect_project_stack(workspace_dir)
    _STACK_CACHE[ws] = (now, stack)
    return stack


def is_path_in_supported_oumv_scope(workspace_dir: str, file_path: str, use_cache: bool = False) -> Optional[Dict[str, Any]]:
    """Returns the supported stack entry owning `file_path`, or None if unsupported (e.g. backend/node)."""
    if not workspace_dir or not file_path:
        return None
    ws_abs = os.path.realpath(os.path.abspath(workspace_dir))
    f_abs = os.path.realpath(os.path.abspath(file_path if os.path.isabs(file_path) else os.path.join(ws_abs, file_path)))
    if not (f_abs == ws_abs or f_abs.startswith(ws_abs + os.sep)):
        return None

    rel = os.path.relpath(f_abs, ws_abs).replace(os.sep, "/")
    stack = detect_project_stack_cached(ws_abs) if use_cache else detect_project_stack(ws_abs)
    packages = stack.get("packages") or []
    # Match most specific package path first
    sorted_pkgs = sorted(
        packages,
        key=lambda p: (0 if p.get("path", ".") == "." else len(str(p.get("path", ".")))),
        reverse=True,
    )
    for pkg in sorted_pkgs:
        p_rel = str(pkg.get("path") or ".").replace(os.sep, "/").strip("/")
        if p_rel == "." or rel == p_rel or rel.startswith(p_rel + "/"):
            if not pkg.get("oumvSupported"):
                return None
            ext = os.path.splitext(f_abs)[1].lower()
            role = pkg.get("role")
            if role == "frontend":
                if ext in (".tsx", ".jsx", ".vue"):
                    return pkg
                if ext in (".ts", ".js"):
                    # A repository-root frontend also owns scripts/, configs and
                    # tooling in .ts/.js; only nudge files under source-like dirs.
                    if p_rel != ".":
                        return pkg
                    top = rel.split("/", 1)[0].lower()
                    if "/" in rel and top in _FRONTEND_SOURCE_DIRS:
                        return pkg
                return None
            if role == "backend":
                if ext in (".go", ".py"):
                    return pkg
                # Express/Fastify adapters ship from npm; their sources are in scope.
                if pkg.get("provider") == "node" and ext in (".js", ".ts", ".mjs", ".cjs", ".jsx", ".tsx"):
                    return pkg
                return None
            return None
    return None
