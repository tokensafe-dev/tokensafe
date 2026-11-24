"""
Universal OUMV (Observe, Understand, Mutate, Verify) Runtime Gateway for TokenSafe.
Connects AI coding agents to live Frontend (tokensafe-ui, Vite, React) and Backend
(FastAPI, Go, Node.js) DevServers via UARP JSON-RPC 2.0.
"""

import os
import json
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Any, Optional, Tuple, List
from src.paths import get_active_workspace


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

    @classmethod
    def _check_workspace_runtime_files(cls, workspace_dir: str):
        lock_paths = [
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
                msg = err.get("message") if isinstance(err, dict) else str(err)
                raise RuntimeError(f"JSON-RPC error from {endpoint_url}: {msg}")
            return data.get("result")

    @classmethod
    def _resolve_client_url(cls, runtime: str = "auto", target: Optional[str] = None) -> str:
        fe, be = cls.get_endpoints()

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
            "staticIntelligence": "ACTIVE (L0/L1/L2 AST observation & cross-stack linking ready)",
            "detectedStack": stack_info,
            "workspace": ws,
        }

    # 2. Tool: oumv_observe
    @classmethod
    def observe(cls, level: str = "L0", target: Optional[str] = None, runtime: str = "auto") -> Any:
        fe, be = cls.get_endpoints()

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
                ws = get_active_workspace()
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
            url = cls._resolve_client_url(runtime=runtime, target=target)
            res = cls._rpc_call(url, "oumv/observe", {"level": level, "target": target, "componentId": target})
            if isinstance(res, dict) and "evidence" not in res:
                res["evidence"] = {"provenance": "runtime", "confidence": "confirmed", "sourceAdapter": "live-devserver"}
            return res
        except RuntimeError:
            # Level 1 Static Fallback via GraphEngine
            from src.graph_engine import GraphEngine
            ws = get_active_workspace()
            graph = GraphEngine.get_instance(ws)
            return graph.get_static_oumv_slice(level=level, target=target)

    # 3. Tool: oumv_understand
    @classmethod
    def understand(cls, query: str = "causality", target: Optional[str] = None, runtime: str = "auto") -> Any:
        fe, be = cls.get_endpoints()

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

            # Out-of-the-box static fallback if neither is online
            if not be and not fe:
                from src.graph_engine import GraphEngine
                ws = get_active_workspace()
                graph = GraphEngine.get_instance(ws)
                report["staticAnalysis"] = graph.get_static_oumv_slice(level="L1", target=target)
                report["evidence"] = {"provenance": "static", "confidence": "confirmed", "sourceAdapter": "tokensafe-graph-engine"}

            return report

        try:
            url = cls._resolve_client_url(runtime=runtime, target=target)
            return cls._rpc_call(url, "oumv/understand", {"query": query, "target": target, "componentId": target})
        except RuntimeError:
            from src.graph_engine import GraphEngine
            ws = get_active_workspace()
            graph = GraphEngine.get_instance(ws)
            return graph.get_static_oumv_slice(level="L1", target=target)

    # 4. Tool: oumv_mutate
    @classmethod
    def mutate(cls, path: str, symbol: Optional[str] = None, replacement: Optional[str] = None, expected_version: Optional[str] = None, runtime: str = "auto") -> Any:
        # Check workspace permissions
        ws = get_active_workspace()
        cfg_file = os.path.join(ws, ".tokensafe", "oumv.json")
        if os.path.isfile(cfg_file):
            try:
                with open(cfg_file, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                if not cfg.get("permissions", {}).get("mutate", False):
                    raise RuntimeError("[OUMV PERMISSION_DENIED: -32007] Workspace policy prohibits mutations (.tokensafe/oumv.json: permissions.mutate=false). Set mutate: true to allow agent edits.")
            except RuntimeError:
                raise
            except Exception:
                pass

        url = cls._resolve_client_url(runtime=runtime, target=path)
        return cls._rpc_call(url, "oumv/applyEdit", {
            "path": path,
            "filePath": path,
            "symbol": symbol,
            "replacement": replacement,
            "source": replacement,
            "expectedVersion": expected_version,
        })

    # 5. Tool: oumv_verify
    @classmethod
    def verify(cls, target: str, action: str = "invoke", payload: Optional[dict] = None, runtime: str = "auto") -> Any:
        url = cls._resolve_client_url(runtime=runtime, target=target)
        return cls._rpc_call(url, "oumv/verify", {
            "target": target,
            "componentId": target,
            "action": action,
            "payload": payload or {},
        })
