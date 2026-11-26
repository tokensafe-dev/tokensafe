"""
Universal OUMV (Observe, Understand, Mutate, Verify) Runtime Gateway for TokenSafe.
Connects AI coding agents to live Frontend (tokensafe-ui, Vite, React) and Backend
(FastAPI, Go, Node.js) DevServers via UARP JSON-RPC 2.0.
"""

import os
import json
import time
import hashlib
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Any, Optional, Tuple, List
from src.paths import get_active_workspace


class OUMVRpcError(RuntimeError):
    """Raised when a live OUMV runtime explicitly returns a JSON-RPC error."""


class OUMVGateway:
    """Dispatches OUMV protocol calls to live DevServers with zero-config discovery."""

    _cached_fe: Optional[Dict[str, Any]] = None
    _cached_be: Optional[Dict[str, Any]] = None
    _last_scan_time: float = 0.0
    _SCAN_INTERVAL_SEC: float = 5.0

    @classmethod
    def get_endpoints(cls, workspace_dir: Optional[str] = None) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        """Returns (frontend_endpoint, backend_endpoint) with periodic cache invalidation."""
        now = time.time()
        if (now - cls._last_scan_time > cls._SCAN_INTERVAL_SEC) or (not cls._cached_fe and not cls._cached_be):
            cls.scan(workspace_dir=workspace_dir)
        return cls._cached_fe, cls._cached_be

    @classmethod
    def scan(cls, workspace_dir: Optional[str] = None):
        """Scans workspace runtime lockfiles and local ports in parallel."""
        cls._last_scan_time = time.time()
        ws = workspace_dir or get_active_workspace()

        # 1. Check workspace runtime files (.tokensafe/runtime.json or .oumv/runtime.json)
        cls._check_workspace_runtime_files(ws)

        # 2. Parallel candidate probe for remaining missing endpoints
        env_fe = os.environ.get("TOKENSAFE_FRONTEND_URL")
        fe_candidates = [
            *( [env_fe] if env_fe else [] ),
            "http://localhost:5173/__oumv/rpc",
            "http://localhost:5173/__tokensafe_ui/rpc",
            "http://localhost:5173/__agent_dom/rpc",
            "http://localhost:3000/__oumv/rpc",
            "http://localhost:3000/__tokensafe_ui/rpc",
            "http://localhost:4173/__oumv/rpc",
            "http://localhost:5174/__oumv/rpc",
            "http://localhost:8081/__oumv/rpc",
        ]

        env_be = os.environ.get("TOKENSAFE_BACKEND_URL")
        be_candidates = [
            *( [env_be] if env_be else [] ),
            "http://localhost:8080/__oumv/rpc",
            "http://localhost:8000/__oumv/rpc",
            "http://localhost:3001/__oumv/rpc",
            "http://localhost:5000/__oumv/rpc",
            "http://localhost:9000/__oumv/rpc",
            "http://localhost:4000/__oumv/rpc",
        ]

        with ThreadPoolExecutor(max_workers=6) as executor:
            future_fe = executor.submit(cls._probe_candidates, fe_candidates, "frontend") if not cls._cached_fe else None
            future_be = executor.submit(cls._probe_candidates, be_candidates, "backend") if not cls._cached_be else None

            if future_fe:
                found_fe = future_fe.result()
                if found_fe:
                    cls._cached_fe = found_fe
            if future_be:
                found_be = future_be.result()
                if found_be:
                    cls._cached_be = found_be

    @staticmethod
    def global_runtime_file(workspace_dir: str) -> str:
        """Zero-footprint global runtime descriptor path outside the target repo."""
        canonical = os.path.realpath(os.path.abspath(workspace_dir))
        ws_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        tokensafe_home = os.environ.get("TOKENSAFE_HOME") or os.path.expanduser("~/.tokensafe")
        return os.path.join(tokensafe_home, "runtimes", f"{ws_id}.json")

    @classmethod
    def _check_workspace_runtime_files(cls, workspace_dir: str):
        lock_paths = [
            cls.global_runtime_file(workspace_dir),
            os.path.join(workspace_dir, ".tokensafe", "runtime.json"),
            os.path.join(workspace_dir, ".oumv", "runtime.json"),
        ]
        for lp in lock_paths:
            if os.path.isfile(lp):
                try:
                    with open(lp, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    fe_info = data.get("frontend")
                    if fe_info and fe_info.get("url"):
                        cls._cached_fe = {
                            "kind": "frontend",
                            "name": fe_info.get("name", "tokensafe-ui"),
                            "version": fe_info.get("version", "1.0.0"),
                            "url": fe_info["url"],
                            "sessionToken": fe_info.get("sessionToken"),
                            "healthy": True,
                            "capabilities": fe_info.get("capabilities", {}),
                        }
                    be_info = data.get("backend")
                    if be_info and be_info.get("url"):
                        cls._cached_be = {
                            "kind": "backend",
                            "name": be_info.get("name", "tokensafe-backend"),
                            "version": be_info.get("version", "1.0.0"),
                            "url": be_info["url"],
                            "sessionToken": be_info.get("session_token") or be_info.get("sessionToken"),
                            "healthy": True,
                            "capabilities": be_info.get("capabilities", {}),
                        }
                except Exception:
                    pass

    @classmethod
    def _probe_candidates(cls, urls: List[str], kind: str) -> Optional[Dict[str, Any]]:
        for url in urls:
            try:
                res = cls._rpc_call(url, "oumv/handshake", {"protocolVersion": "1.0.0"}, timeout=0.35)
                if res and isinstance(res, dict) and "runtimeInfo" in res:
                    return {
                        "kind": kind,
                        "name": res.get("runtimeInfo", {}).get("name", f"{kind}-runtime"),
                        "version": res.get("runtimeInfo", {}).get("version", "1.0.0"),
                        "url": url,
                        "healthy": True,
                        "capabilities": {
                            "observeLevels": res.get("capabilities", {}).get("observe", {}).get("levels"),
                            "causality": res.get("capabilities", {}).get("understand", {}).get("causality"),
                            "occ": res.get("capabilities", {}).get("mutate", {}).get("occ"),
                        },
                    }
            except Exception:
                continue
        return None

    @classmethod
    def _rpc_call(cls, endpoint_url: str, method: str, params: Optional[dict] = None, timeout: float = 3.0) -> Any:
        payload = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params or {},
        }).encode("utf-8")

        headers = {"Content-Type": "application/json", "User-Agent": "TokenSafe-Gateway/1.0"}
        token = None
        if cls._cached_fe and cls._cached_fe.get("url") == endpoint_url:
            token = cls._cached_fe.get("sessionToken")
        elif cls._cached_be and cls._cached_be.get("url") == endpoint_url:
            token = cls._cached_be.get("sessionToken")

        if token:
            headers["X-OUMV-Session-Token"] = token

        req = urllib.request.Request(
            endpoint_url,
            data=payload,
            headers=headers,
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if "error" in data and data["error"]:
                err = data["error"]
                if isinstance(err, dict):
                    code = err.get("code")
                    msg = err.get("message") or str(err)
                    raise OUMVRpcError(f"JSON-RPC error ({code}) from {endpoint_url}: {msg}")
                raise OUMVRpcError(f"JSON-RPC error from {endpoint_url}: {err}")
            return data.get("result")

    @classmethod
    def _resolve_client_url(cls, runtime: str = "auto", target: Optional[str] = None, workspace_dir: Optional[str] = None) -> str:
        fe, be = cls.get_endpoints(workspace_dir=workspace_dir)

        if runtime == "frontend":
            if not fe:
                raise RuntimeError("No active frontend runtime detected on ports 5173/3000")
            return fe["url"]
        elif runtime == "backend":
            if not be:
                raise RuntimeError("No active backend runtime detected on ports 8080/8000")
            return be["url"]

        # Auto detection based on target
        if target:
            try:
                from src.retrofit import OUMVProjectContext
                ctx = OUMVProjectContext.get_for_workspace()
                detected_rt = ctx.resolve_target_runtime(target)
                if detected_rt == "backend" and be:
                    return be["url"]
                elif detected_rt == "frontend" and fe:
                    return fe["url"]
            except Exception:
                pass

            # Backend hints: HTTP method prefixes, API paths, SQL, handlers
            if any(target.startswith(m) for m in ["GET ", "POST ", "PUT ", "DELETE ", "PATCH "]) or "/api/" in target or target.startswith("/"):
                if be:
                    return be["url"]
            # Frontend hints: DOM selectors (#, .), components (<, JSX, Tab)
            if any(target.startswith(c) for c in ["#", ".", "[", "button", "input"]) or target.startswith("<") or target.endswith((".tsx", ".jsx", ".vue")):
                if fe:
                    return fe["url"]

        # Default fallback: backend if exists else frontend
        if be:
            return be["url"]
        if fe:
            return fe["url"]
        raise RuntimeError("No live OUMV runtime (Frontend or Backend) detected in workspace.")

    # 1. Tool: oumv_status
    @classmethod
    def status(cls, workspace_dir: Optional[str] = None) -> Dict[str, Any]:
        cls.scan(workspace_dir=workspace_dir)
        fe, be = cls._cached_fe, cls._cached_be
        ws = workspace_dir or get_active_workspace()

        # Out-of-the-box stack detection
        stack_info = {}
        try:
            from src.retrofit import detect_project_stack
            stack_info = detect_project_stack(ws)
        except Exception:
            pass

        return {
            "mode": "OUT_OF_THE_BOX_HYBRID",
            "frontend": fe or {
                "status": "DISCONNECTED",
                "framework": stack_info.get("frontend", {}).get("framework", "None") if stack_info.get("frontend") else "None",
                "tip": "Static AST analysis active. When frontend DevServer starts (port 5173/3000), live OUMV auto-connects."
            },
            "backend": be or {
                "status": "DISCONNECTED",
                "framework": stack_info.get("backend", {}).get("framework", "None") if stack_info.get("backend") else "None",
                "tip": "Static AST analysis active. When backend DevServer starts (port 8080/8000), live OUMV auto-connects."
            },
            "adapters": cls._adapter_lifecycle(stack_info, fe, be),
            "staticIntelligence": "ACTIVE (L0/L1/L2 AST observation & cross-stack linking ready)",
            "detectedStack": stack_info,
            "workspace": ws,
        }

    @staticmethod
    def _adapter_lifecycle(stack_info: Dict[str, Any], fe: Optional[Dict[str, Any]], be: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Per-adapter lifecycle + health per the OUMV Adapter Contract (schemas/adapter.json)."""
        adapters: List[Dict[str, Any]] = []
        fe_detected = bool(stack_info.get("frontend"))
        be_detected = bool(stack_info.get("backend"))
        if not fe_detected and not be_detected:
            return adapters

        try:
            from src.retrofit import get_adapters_catalog
            catalog = get_adapters_catalog()
        except Exception:
            catalog = []

        def _find_adapter(profile: str, provider: Optional[str]) -> Dict[str, Any]:
            if provider:
                for a in catalog:
                    if a.get("id") == f"{profile}/{provider}" and a.get("status") == "ready":
                        return a
            for a in catalog:
                if a.get("profile") == profile and a.get("status") == "ready":
                    return a
            return {}

        def _entry(profile: str, connected: Optional[Dict[str, Any]], detected: bool, provider: Optional[str] = None) -> Dict[str, Any]:
            adapter = _find_adapter(profile, provider)
            if connected:
                state, health, detail = "ACTIVE", "healthy", f"Live runtime: {connected.get('name', 'unknown')}"
            elif detected:
                state, health, detail = "HANDSHAKE", "degraded", "Detected statically; runtime offline — hybrid static fallback active"
            else:
                state, health, detail = "DISCOVER", "unavailable", "Not detected in this workspace"
            return {
                "profile": profile,
                "adapter": adapter.get("id", f"{profile}/unknown"),
                "technology": adapter.get("technology"),
                "capabilities": adapter.get("capabilities"),
                "lifecycle": {"state": state, "health": {"status": health, "detail": detail}},
                "security": {
                    "productionFailClosed": True,
                    "workspaceBoundary": True,
                },
            }

        adapters.append(_entry("frontend", fe, fe_detected, (stack_info.get("frontend") or {}).get("provider")))
        adapters.append(_entry("backend", be, be_detected, (stack_info.get("backend") or {}).get("provider")))
        return adapters

    # 2. Tool: oumv_observe
    @classmethod
    def observe(cls, level: str = "L0", target: Optional[str] = None, runtime: str = "auto", workspace_dir: Optional[str] = None) -> Any:
        fe, be = cls.get_endpoints(workspace_dir=workspace_dir)

        # Consolidated fullstack L0 pulse (~30 tokens)
        if level.upper() == "L0" and (not target or target == "fullstack") and (runtime in ("auto", "")):
            results = {"mode": "FULLSTACK_PULSE"}
            if fe:
                try:
                    results["frontend"] = cls._rpc_call(fe["url"], "oumv/observe", {"level": "L0"})
                except Exception as e:
                    results["frontend"] = {"error": str(e)}
            if be:
                try:
                    results["backend"] = cls._rpc_call(be["url"], "oumv/observe", {"level": "L0"})
                except Exception as e:
                    results["backend"] = {"error": str(e)}

            # Out-of-the-box fallback when both are offline
            if not fe and not be:
                from src.graph_engine import GraphEngine
                ws = workspace_dir or get_active_workspace()
                graph = GraphEngine.get_instance(ws)
                results["staticAnalysis"] = graph.get_static_oumv_slice(level="L0")
                results["tip"] = "DevServers offline — static AST intelligence active out-of-the-box."
            else:
                if not fe:
                    results["frontend"] = {"status": "OFFLINE_STATIC", "tip": "Frontend DevServer offline (using static AST)"}
                if not be:
                    results["backend"] = {"status": "OFFLINE_STATIC", "tip": "Backend DevServer offline (using static AST)"}

            return results

        try:
            url = cls._resolve_client_url(runtime=runtime, target=target, workspace_dir=workspace_dir)
            res = cls._rpc_call(url, "oumv/observe", {"level": level, "target": target, "componentId": target})
            if isinstance(res, dict) and "evidence" not in res:
                res["evidence"] = {"provenance": "runtime", "confidence": "confirmed", "sourceAdapter": "live-devserver"}
            return res
        except RuntimeError:
            # Level 1 Static Fallback via GraphEngine
            from src.graph_engine import GraphEngine
            ws = workspace_dir or get_active_workspace()
            graph = GraphEngine.get_instance(ws)
            return graph.get_static_oumv_slice(level=level, target=target)

    # 3. Tool: oumv_understand
    @classmethod
    def understand(cls, query: str = "causality", target: Optional[str] = None, runtime: str = "auto", workspace_dir: Optional[str] = None) -> Any:
        fe, be = cls.get_endpoints(workspace_dir=workspace_dir)

        from src.graph_engine import GraphEngine
        ws = workspace_dir or get_active_workspace()
        graph = GraphEngine.get_instance(ws)

        # Impact analysis: static cross-stack graph as the backbone, runtime answers attached when online
        if query == "impact":
            report: Dict[str, Any] = {
                "query": "impact",
                "target": target,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            report.update(graph.get_static_impact(target or ""))
            if target:
                if fe:
                    try:
                        report["frontendImpact"] = cls._rpc_call(fe["url"], "oumv/understand", {"query": "impact", "target": target, "componentId": target})
                    except Exception as e:
                        report["frontendError"] = str(e)
                if be:
                    try:
                        report["backendImpact"] = cls._rpc_call(be["url"], "oumv/understand", {"query": "impact", "target": target})
                    except Exception as e:
                        report["backendError"] = str(e)
            return report

        # Cross-layer causality query
        if query == "causality" and runtime in ("auto", ""):
            report = {
                "query": "causality",
                "target": target,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            if be:
                try:
                    report["backendCausality"] = cls._rpc_call(be["url"], "oumv/understand", {"query": query, "target": target})
                except Exception as e:
                    report["backendError"] = str(e)
            if fe:
                try:
                    report["frontendCausality"] = cls._rpc_call(fe["url"], "oumv/understand", {"query": query, "target": target, "componentId": target})
                except Exception as e:
                    report["frontendError"] = str(e)

            # Unified cross-stack relationships (frontend call -> backend endpoint), available online and offline
            cross = cls._static_cross_stack(graph, target)
            if cross["edges"]:
                report["crossStack"] = cross

            # Out-of-the-box static fallback if neither is online
            if not be and not fe:
                report["staticAnalysis"] = graph.get_static_oumv_slice(level="L1", target=target)
                report["evidence"] = {"provenance": "static", "confidence": "confirmed", "sourceAdapter": "tokensafe-graph-engine"}

            return report

        try:
            url = cls._resolve_client_url(runtime=runtime, target=target, workspace_dir=workspace_dir)
            return cls._rpc_call(url, "oumv/understand", {"query": query, "target": target, "componentId": target})
        except RuntimeError:
            if query == "impact":
                return graph.get_static_impact(target or "")
            return graph.get_static_oumv_slice(level="L1", target=target)

    @staticmethod
    def _static_cross_stack(graph: Any, target: Optional[str]) -> Dict[str, Any]:
        """Cross-stack edges (frontend API call -> backend endpoint) from the static graph."""
        evidence = {"provenance": "static", "sourceAdapter": "tokensafe-graph-engine", "confidence": "probable"}
        edges: List[Dict[str, Any]] = []
        for link in getattr(graph, "api_links", []):
            if target:
                t = target.lower()
                hay = " ".join([
                    link["source"].get("route") or "", link["source"].get("file") or "",
                    link["target"].get("route") or "", link["target"].get("file") or "",
                ]).lower()
                if t not in hay:
                    continue
            edges.append({
                "source": link["source"],
                "target": link["target"],
                "evidence": evidence,
            })
        return {"edges": edges[:30], "count": len(edges), "evidence": evidence}

    # 4. Tool: oumv_mutate
    @classmethod
    def mutate(cls, path: str, symbol: Optional[str] = None, replacement: Optional[str] = None, expected_version: Optional[str] = None, runtime: str = "auto", workspace_dir: Optional[str] = None) -> Any:
        # Check workspace permissions (enabled by default out-of-the-box unless explicitly opted out)
        ws = workspace_dir or get_active_workspace()
        cfg_file = os.path.join(ws, ".tokensafe", "oumv.json")
        if os.path.isfile(cfg_file):
            try:
                with open(cfg_file, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                perms = cfg.get("permissions", {})
                if perms.get("mutate") is False:
                    raise RuntimeError("[OUMV PERMISSION_DENIED: -32007] Workspace policy explicitly prohibits mutations (.tokensafe/oumv.json: permissions.mutate=false).")
            except RuntimeError:
                raise
            except Exception:
                pass

        try:
            url = cls._resolve_client_url(runtime=runtime, target=path, workspace_dir=workspace_dir)
            return cls._rpc_call(url, "oumv/applyEdit", {
                "path": path,
                "filePath": path,
                "symbol": symbol,
                "replacement": replacement,
                "source": replacement,
                "expectedVersion": expected_version,
            })
        except OUMVRpcError:
            raise
        except RuntimeError as e:
            # Out-of-the-box direct fallback when live dev-servers are offline
            if replacement is not None:
                abs_path = os.path.normpath(os.path.join(ws, path))
                # Validate workspace boundary
                if not abs_path.startswith(os.path.abspath(ws)):
                    raise RuntimeError(f"[OUMV SECURITY_VIOLATION: -32003] Path traversal blocked: {path}")

                import hashlib
                current_version = None
                if os.path.isfile(abs_path):
                    with open(abs_path, "rb") as f:
                        content = f.read()
                    current_version = "sha256:" + hashlib.sha256(content).hexdigest()

                if expected_version and current_version and expected_version != current_version:
                    raise RuntimeError(f"[OUMV CONFLICT: -32001] OCC version mismatch: expected {expected_version}, current is {current_version}")

                # Syntax pre-flight: never persist syntactically broken code (OUMV spec §8 Mutation Policy)
                from src.syntax_guard import check_syntax
                syntax_check = check_syntax(abs_path, replacement)
                if syntax_check.get("status") == "failed":
                    raise RuntimeError(
                        f"[OUMV SYNTAX_ERROR: -32002] Edit rejected by syntax guard "
                        f"({syntax_check.get('tool')}): {syntax_check.get('error')}"
                    )

                # Automatic backup of previous content before overwriting
                backup_path = None
                if current_version is not None:
                    try:
                        backup_dir = os.path.join(ws, ".tokensafe", "backups")
                        os.makedirs(backup_dir, exist_ok=True)
                        digest = hashlib.sha256(content).hexdigest()[:8]
                        backup_path = os.path.join(backup_dir, f"{digest}_{os.path.basename(abs_path)}.bak")
                        with open(backup_path, "wb") as f:
                            f.write(content)
                    except Exception:
                        backup_path = None

                os.makedirs(os.path.dirname(abs_path), exist_ok=True)
                with open(abs_path, "w", encoding="utf-8") as f:
                    f.write(replacement)

                new_version = "sha256:" + hashlib.sha256(replacement.encode("utf-8")).hexdigest()
                result = {
                    "success": True,
                    "path": path,
                    "symbol": symbol,
                    "newVersion": new_version,
                    "mode": "OUT_OF_THE_BOX_DIRECT_EDIT",
                    "syntaxCheck": syntax_check,
                    "message": "Surgical edit applied directly to workspace file (dev-server offline)",
                }
                if backup_path:
                    result["backupPath"] = backup_path
                    result["rollbackHint"] = f"Previous content backed up at {backup_path}"
                return result
            raise

    # 5. Tool: oumv_verify
    @classmethod
    def verify(cls, target: str, action: str = "invoke", payload: Optional[dict] = None, runtime: str = "auto", workspace_dir: Optional[str] = None) -> Any:
        url = cls._resolve_client_url(runtime=runtime, target=target, workspace_dir=workspace_dir)
        res = cls._rpc_call(url, "oumv/verify", {
            "target": target,
            "componentId": target,
            "action": action,
            "payload": payload or {},
        })
        # Honest verification scope (spec §13): state exactly what was verified.
        if isinstance(res, dict):
            scope = res.setdefault("scope", {})
            checked = scope.setdefault("checked", [])
            checked_entry = "in-memory dispatch against live runtime"
            if checked_entry not in checked:
                checked.append(checked_entry)
            scope.setdefault("notExecuted", [
                "end-to-end browser flow",
                "production behavior",
                "full test suite",
            ])
        return res

    # 6. Tool: oumv_graph — unified cross-stack intelligence graph (spec §11)
    @classmethod
    def graph(cls, query: str = "overview", target: Optional[str] = None, workspace_dir: Optional[str] = None) -> Any:
        """Unified graph over static + runtime evidence, with §11.4-style queries."""
        fe, be = cls.get_endpoints(workspace_dir=workspace_dir)
        from src.cross_stack import build_unified_graph, who_depends_on, impact_from_graph

        ws = workspace_dir or get_active_workspace()

        q = (query or "overview").strip()
        if q == "who_depends_on":
            full = build_unified_graph(workspace_dir=ws, fe=fe, be=be, rpc_call=cls._rpc_call)
            return who_depends_on(target or "", full)
        if q == "impact":
            full = build_unified_graph(workspace_dir=ws, fe=fe, be=be, rpc_call=cls._rpc_call)
            return impact_from_graph(target or "", full)

        g = build_unified_graph(workspace_dir=ws, fe=fe, be=be, rpc_call=cls._rpc_call)
        if target:
            t = target.lower()
            g["nodes"] = [n for n in g["nodes"] if t in str(n).lower()][:50]
            g["edges"] = [e for e in g["edges"] if t in (e["source"] + e["target"]).lower()][:50]
            g["filteredBy"] = target
        return g
