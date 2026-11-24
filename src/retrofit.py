"""
TokenSafe OUMV Retrofit Engine.
Enables existing frontend and backend projects to adopt the OUMV protocol
incrementally without migrating frameworks or restructuring codebases.
"""

import os
import json
import time
from typing import Dict, Any, List, Optional, Tuple


def detect_project_stack(workspace_dir: str) -> Dict[str, Any]:
    """Inspects workspace manifests to detect frontend and backend frameworks."""
    stack: Dict[str, Any] = {
        "workspace": workspace_dir,
        "frontend": None,
        "backend": None,
        "package_managers": [],
        "detected_files": [],
    }

    # 1. Frontend detection (package.json)
    pkg_json_path = os.path.join(workspace_dir, "package.json")
    if os.path.isfile(pkg_json_path):
        stack["detected_files"].append("package.json")
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
            }

            if "next" in deps:
                fe_info.update({"framework": "nextjs", "provider": "react", "adapter": "tokensafe-ui/oumv/react", "version": deps.get("next")})
            elif "react" in deps:
                fe_info.update({"framework": "react", "provider": "react", "adapter": "tokensafe-ui/oumv/react", "version": deps.get("react")})
            elif "vue" in deps:
                fe_info.update({"framework": "vue", "provider": "vue", "adapter": "tokensafe-ui/oumv/vue", "version": deps.get("vue")})
            elif "solid-js" in deps or "@tokensafe-ui/core" in deps:
                fe_info.update({"framework": "solid", "provider": "solid", "adapter": "tokensafe-ui/core", "version": deps.get("solid-js") or deps.get("@tokensafe-ui/core")})
            elif "svelte" in deps:
                fe_info.update({"framework": "svelte", "provider": "svelte", "adapter": "tokensafe-ui/core", "version": deps.get("svelte")})

            if "framework" in fe_info:
                stack["frontend"] = fe_info
        except Exception:
            pass

    # Lockfiles
    for lock, pm in [("pnpm-lock.yaml", "pnpm"), ("bun.lockb", "bun"), ("bun.lock", "bun"), ("yarn.lock", "yarn"), ("package-lock.json", "npm")]:
        if os.path.isfile(os.path.join(workspace_dir, lock)):
            stack["package_managers"].append(pm)
            stack["detected_files"].append(lock)

    # 2. Go Backend detection (go.mod)
    go_mod_path = os.path.join(workspace_dir, "go.mod")
    if not os.path.isfile(go_mod_path) and os.path.isfile(os.path.join(workspace_dir, "go", "go.mod")):
        go_mod_path = os.path.join(workspace_dir, "go", "go.mod")

    if os.path.isfile(go_mod_path):
        stack["detected_files"].append(os.path.relpath(go_mod_path, workspace_dir))
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
            stack["backend"] = {"framework": framework, "provider": "go", "adapter": "tokensafe-backend/go", "language": "go"}
        except Exception:
            stack["backend"] = {"framework": "go", "provider": "go", "adapter": "tokensafe-backend/go", "language": "go"}

    # 3. Python Backend detection (pyproject.toml, requirements.txt, setup.py)
    py_files = [
        os.path.join(workspace_dir, "pyproject.toml"),
        os.path.join(workspace_dir, "python", "pyproject.toml"),
        os.path.join(workspace_dir, "requirements.txt"),
    ]
    for pf in py_files:
        if os.path.isfile(pf):
            stack["detected_files"].append(os.path.relpath(pf, workspace_dir))
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
                if not stack["backend"]:
                    stack["backend"] = {
                        "framework": framework,
                        "provider": "fastapi" if framework == "fastapi" else "python",
                        "adapter": "tokensafe-backend/python",
                        "language": "python",
                    }
            except Exception:
                pass
            break

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

    return config, target_file, True


def get_adapters_catalog() -> List[Dict[str, Any]]:
    """Lists available OUMV adapters in the consolidated architecture."""
    return [
        {
            "id": "frontend/react",
            "name": "React Adapter (Zustand, Redux, Context, useReducer)",
            "profile": "frontend",
            "repo": "tokensafe-ui",
            "mode": "hybrid",
            "status": "ready",
            "description": "Progressive AST component discovery + runtime action/state inspection",
        },
        {
            "id": "frontend/solid",
            "name": "SolidJS / TokenSafe UI Native Adapter",
            "profile": "frontend",
            "repo": "tokensafe-ui",
            "mode": "hybrid",
            "status": "ready",
            "description": "Fine-grained reactive signals causality + virtual DOM observer",
        },
        {
            "id": "frontend/vue",
            "name": "Vue 3 Adapter (Composition API, Pinia)",
            "profile": "frontend",
            "repo": "tokensafe-ui",
            "mode": "hybrid",
            "status": "ready",
            "description": "Vue component hierarchy and store snapshot observer",
        },
        {
            "id": "backend/go",
            "name": "Go Backend Adapter (Gin, Chi, net/http)",
            "profile": "backend",
            "repo": "tokensafe-backend/go",
            "mode": "hybrid",
            "status": "ready",
            "description": "Go AST pre-flight compiler guard + transactional DB sandbox with rollback",
        },
        {
            "id": "backend/fastapi",
            "name": "Python FastAPI Adapter (Starlette, Pydantic)",
            "profile": "backend",
            "repo": "tokensafe-backend/python",
            "mode": "hybrid",
            "status": "ready",
            "description": "FastAPI automatic route & schema introspection + SQLAlchemy dry-run sandbox",
        },
        {
            "id": "backend/node",
            "name": "Node.js Backend Adapter (Express, Fastify)",
            "profile": "backend",
            "repo": "tokensafe-backend/node",
            "mode": "hybrid",
            "status": "planned",
            "description": "Express/Fastify route handler & middleware reflection",
        },
    ]


def run_doctor(workspace_dir: str) -> Dict[str, Any]:
    """Diagnostics check for OUMV configuration, ports, lockfiles, and permissions."""
    stack = detect_project_stack(workspace_dir)
    conf_path = os.path.join(workspace_dir, ".tokensafe", "oumv.json")
    runtime_path = os.path.join(workspace_dir, ".tokensafe", "runtime.json")

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

    return {
        "workspace": workspace_dir,
        "detected_stack": stack,
        "config_present": has_config,
        "config": config_data,
        "runtime_beacon_present": has_runtime,
        "runtime_beacon": runtime_data,
        "permissions": config_data.get("permissions", {"observe": True, "mutate": False}),
        "ready_for_agent": True,
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
