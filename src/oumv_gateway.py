"""
Universal OUMV (Observe, Understand, Mutate, Verify) Runtime Gateway for TokenSafe.
Connects AI coding agents to live Frontend (tokensafe-ui, Vite, React) and Backend
(FastAPI, Go, Node.js) DevServers via UARP JSON-RPC 2.0.
"""

import os
import json
import time
import socket
import hashlib
import urllib.request
import urllib.error
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Any, Optional, Tuple, List
from src.paths import get_active_workspace


class OUMVRpcError(RuntimeError):
    """Raised when a live OUMV runtime explicitly returns a JSON-RPC error."""


class OUMVUnsupportedMethodError(OUMVRpcError):
    """The runtime answered but does not implement the requested method.

    This is not a safety rejection: the runtime cannot mediate the operation at
    all, so the gateway may perform the guarded offline edit instead.
    """


_UNSUPPORTED_HINTS = (
    "unknown method",
    "method not found",
    "-32601",
    "not implemented",
    "not supported",
)


class OUMVGateway:
    """Dispatches OUMV protocol calls to live DevServers with zero-config discovery."""

    # Per-workspace endpoint cache: one process serves several workspace roots
    # (multi-root sessions), so root A's live runtime must never leak into
    # root B's status/observe answer.
    _workspace_states: Dict[str, Dict[str, Any]] = {}
    _SCAN_INTERVAL_SEC: float = 5.0
    # Liveness cache for descriptor-derived ports: the bash guard runs per command,
    # so a TCP probe must be memoized instead of blocking every invocation.
    _port_liveness: Dict[Tuple[str, int], Tuple[float, bool]] = {}
    _PORT_TTL_SEC: float = 8.0
    _PORT_PROBE_TIMEOUT: float = 0.05
    # Descriptors left behind by a crashed dev server must not stay authoritative forever.
    _DESCRIPTOR_MAX_AGE_SEC: float = 12 * 3600.0

    @classmethod
    def _is_port_open(cls, host: str, port: int, timeout: Optional[float] = None, use_cache: bool = True) -> bool:
        """Cheap TCP liveness check with a short TTL cache (never raises)."""
        key = (host or "127.0.0.1", int(port))
        now = time.time()
        if use_cache:
            hit = cls._port_liveness.get(key)
            if hit and (now - hit[0]) < cls._PORT_TTL_SEC:
                return hit[1]
        alive = False
        try:
            with socket.create_connection(key, timeout=timeout or cls._PORT_PROBE_TIMEOUT):
                alive = True
        except OSError:
            alive = False
        cls._port_liveness[key] = (now, alive)
        return alive

    @classmethod
    def _is_url_live(cls, url: Optional[str]) -> bool:
        """True when a runtime URL's port accepts connections (port-less URLs are trusted)."""
        if not url:
            return False
        try:
            parsed = urllib.parse.urlparse(url)
            port = parsed.port
        except ValueError:
            return False
        except Exception:
            return True
        if not port:
            return True
        return cls._is_port_open(parsed.hostname or "127.0.0.1", int(port))

    @classmethod
    def _ws_key(cls, workspace_dir: Optional[str] = None) -> str:
        """Canonical per-workspace cache key (realpath'd, never empty)."""
        base = workspace_dir or get_active_workspace() or os.getcwd()
        return os.path.realpath(os.path.abspath(base))

    @classmethod
    def _state_for(cls, workspace_dir: Optional[str] = None) -> Dict[str, Any]:
        """Per-workspace endpoint state: {"fe", "be", "last_scan"}."""
        return cls._workspace_states.setdefault(
            cls._ws_key(workspace_dir), {"fe": None, "be": None, "last_scan": 0.0}
        )

    @classmethod
    def _drop_stale_descriptor_endpoints(cls, workspace_dir: Optional[str] = None) -> List[Dict[str, Any]]:
        """Clears descriptor-sourced endpoints whose port no longer listens.

        A descriptor is a hint written by the dev server; after a crash it keeps
        pointing at a dead (or reused) port and every consumer would report CONNECTED.
        Without a workspace_dir, every cached workspace is checked; with one, only
        that root's state is touched (and its drops reported to the caller).
        Returns the dropped entries so callers can explain the downgrade.
        """
        dropped: List[Dict[str, Any]] = []
        if workspace_dir is not None:
            states = [cls._state_for(workspace_dir)]
        elif cls._workspace_states:
            states = list(cls._workspace_states.values())
        else:
            states = [cls._state_for(None)]
        for st in states:
            for slot in ("fe", "be"):
                entry = st.get(slot)
                if entry and entry.get("source") == "descriptor":
                    url = entry.get("url")
                    if url and not cls._is_url_live(url):
                        dropped.append(entry)
                        st[slot] = None
        return dropped

    @classmethod
    def get_endpoints(cls, workspace_dir: Optional[str] = None) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        """Returns (frontend_endpoint, backend_endpoint) for the given workspace root.

        Each root has its own scan timestamp and endpoint pair; a scan performed for
        root A never satisfies root B's discovery.
        """
        st = cls._state_for(workspace_dir)
        now = time.time()
        if (now - st["last_scan"] > cls._SCAN_INTERVAL_SEC) or (not st["fe"] and not st["be"]):
            cls.scan(workspace_dir=workspace_dir)
        cls._drop_stale_descriptor_endpoints(workspace_dir)
        return st["fe"], st["be"]

    @classmethod
    def scan(cls, workspace_dir: Optional[str] = None):
        """Scans workspace runtime lockfiles and local ports in parallel."""
        ws = workspace_dir or get_active_workspace() or os.getcwd()
        st = cls._state_for(ws)
        st["last_scan"] = time.time()

        # 1. Check workspace runtime files (.tokensafe/runtime.json or .oumv/runtime.json)
        cls._check_workspace_runtime_files(ws, st)

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
            future_fe = executor.submit(cls._probe_candidates, fe_candidates, "frontend") if not st["fe"] else None
            future_be = executor.submit(cls._probe_candidates, be_candidates, "backend") if not st["be"] else None

            if future_fe:
                found_fe = future_fe.result()
                if found_fe:
                    st["fe"] = found_fe
            if future_be:
                found_be = future_be.result()
                if found_be:
                    st["be"] = found_be

    @staticmethod
    def global_runtime_file(workspace_dir: str) -> str:
        """Zero-footprint global runtime descriptor path outside the target repo."""
        canonical = os.path.realpath(os.path.abspath(workspace_dir))
        ws_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        tokensafe_home = os.environ.get("TOKENSAFE_HOME") or os.path.expanduser("~/.tokensafe")
        return os.path.join(tokensafe_home, "runtimes", f"{ws_id}.json")

    @staticmethod
    def _descriptor_path_is_trusted(path: str, workspace_dir: Optional[str] = None) -> bool:
        """A global descriptor whose workspace died must not be adopted.

        Transient roots (system temp dirs) outlive their dev servers: a leftover
        descriptor keeps describing a directory that no longer exists. Such an
        entry is trusted only while the recorded root is still on disk (so live
        smoke-test/benchmark workspaces in temp dirs keep working); user-project
        roots are always trusted and rely on the port-liveness check instead.
        """
        try:
            canonical = os.path.realpath(os.path.abspath(path))
            # Only the global registry entry needs this check: workspace-local files
            # live inside the root they describe, so they cannot outlive it this way.
            runtimes_dir = os.path.realpath(os.path.join(
                os.environ.get("TOKENSAFE_HOME") or os.path.expanduser("~/.tokensafe"), "runtimes"
            ))
            if os.path.dirname(canonical) != runtimes_dir:
                return True
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            recorded = (data.get("frontend") or {}).get("workspaceRoot") or (data.get("backend") or {}).get("workspaceRoot")
            if not recorded:
                # Legacy descriptors without a workspaceRoot: judge by the file's
                # own location — the sha256 key cannot be reverse-mapped.
                return True
            recorded_real = os.path.realpath(os.path.abspath(recorded))
            if workspace_dir and recorded_real != os.path.realpath(os.path.abspath(workspace_dir)):
                return False  # entry under this key describes another root: stale reuse
            from src.paths import is_transient_dir
            if is_transient_dir(recorded_real) and not os.path.isdir(recorded_real):
                return False
            return True
        except Exception:
            return True

    @classmethod
    def _check_workspace_runtime_files(cls, workspace_dir: str, st: Optional[Dict[str, Any]] = None):
        st = st if st is not None else cls._state_for(workspace_dir)
        lock_paths = [lp for lp in (
            cls.global_runtime_file(workspace_dir),
            os.path.join(workspace_dir, ".tokensafe", "runtime.json"),
            os.path.join(workspace_dir, ".oumv", "runtime.json"),
        ) if cls._descriptor_path_is_trusted(lp, workspace_dir)]
        for lp in lock_paths:
            if os.path.isfile(lp):
                try:
                    with open(lp, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    fe_info = data.get("frontend")
                    if fe_info and fe_info.get("url"):
                        st["fe"] = {
                            "kind": "frontend",
                            "name": fe_info.get("name", "tokensafe-ui"),
                            "version": fe_info.get("version", "1.0.0"),
                            "url": fe_info["url"],
                            "sessionToken": fe_info.get("sessionToken"),
                            "healthy": True,
                            "source": "descriptor",
                            "capabilities": fe_info.get("capabilities", {}),
                        }
                    be_info = data.get("backend")
                    if be_info and be_info.get("url"):
                        st["be"] = {
                            "kind": "backend",
                            "name": be_info.get("name", "tokensafe-backend"),
                            "version": be_info.get("version", "1.0.0"),
                            "url": be_info["url"],
                            "sessionToken": be_info.get("session_token") or be_info.get("sessionToken"),
                            "healthy": True,
                            "source": "descriptor",
                            "capabilities": be_info.get("capabilities", {}),
                        }
                except Exception:
                    pass
        return st

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
        # Session tokens live in per-workspace states; a URL identifies its runtime
        # unambiguously, so scan every root's state for a match.
        token = None
        for st in cls._workspace_states.values():
            if st.get("fe", {}) and st["fe"].get("url") == endpoint_url:
                token = st["fe"].get("sessionToken")
            elif st.get("be", {}) and st["be"].get("url") == endpoint_url:
                token = st["be"].get("sessionToken")

        if token:
            headers["X-OUMV-Session-Token"] = token

        req = urllib.request.Request(
            endpoint_url,
            data=payload,
            headers=headers,
            method="POST",
        )

        try:
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
        except urllib.error.HTTPError as exc:
            # Some bridges answer an unsupported method with HTTP 400 instead of a
            # JSON-RPC envelope. Treat that as a protocol error (RuntimeError) so
            # callers can fall back to offline operation instead of crashing on OSError.
            code, msg = cls._http_error_detail(exc)
            raise OUMVRpcError(f"JSON-RPC error ({code}) from {endpoint_url}: {msg}") from exc
        except urllib.error.URLError as exc:
            raise OUMVRpcError(f"runtime unreachable at {endpoint_url}: {exc.reason}") from exc
        except (TimeoutError, OSError) as exc:
            raise OUMVRpcError(f"runtime unreachable at {endpoint_url}: {exc}") from exc

    @staticmethod
    def _http_error_detail(exc: urllib.error.HTTPError) -> Tuple[Optional[int], str]:
        """Extracts (JSON-RPC code, message) from an HTTP error body (read once)."""
        try:
            raw = exc.read().decode("utf-8", "replace")
        except Exception:
            return None, str(exc)
        try:
            err = json.loads(raw).get("error")
            if isinstance(err, dict):
                return err.get("code"), err.get("message") or raw.strip() or str(exc)
            if isinstance(err, str):
                return None, err
        except Exception:
            pass
        return None, raw.strip() or str(exc)

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

    @classmethod
    def _runtime_marker_path(cls, workspace_dir: Optional[str] = None) -> str:
        from src.paths import resolve_data_dir
        ws = os.path.realpath(os.path.abspath(workspace_dir or get_active_workspace()))
        ws_id = hashlib.sha256(ws.encode("utf-8")).hexdigest()[:16]
        return os.path.join(resolve_data_dir(), f".oumv_used_{ws_id}")

    @classmethod
    def mark_oumv_used(cls, workspace_dir: Optional[str] = None) -> None:
        try:
            marker = cls._runtime_marker_path(workspace_dir)
            os.makedirs(os.path.dirname(marker), exist_ok=True)
            with open(marker, "w", encoding="utf-8") as f:
                f.write(str(time.time()))
        except Exception:
            pass

    @classmethod
    def was_recently_used(cls, workspace_dir: Optional[str] = None, max_age_sec: float = 1800.0) -> bool:
        try:
            marker = cls._runtime_marker_path(workspace_dir)
            if not os.path.isfile(marker):
                return False
            return (time.time() - os.path.getmtime(marker)) < max_age_sec
        except Exception:
            return False

    @classmethod
    def get_live_ports(cls, workspace_dir: Optional[str] = None) -> List[int]:
        """Ports from OUMV runtime descriptors that are fresh AND actually listening.

        Guards that hard-block raw curl rely on this list, so a descriptor left behind
        by a crashed dev server (or a port later reused by another process) must not
        count as a live runtime.
        """
        ws = workspace_dir or get_active_workspace()
        now = time.time()
        ports: List[int] = []
        for lp in (
            cls.global_runtime_file(ws),
            os.path.join(ws, ".tokensafe", "runtime.json"),
            os.path.join(ws, ".oumv", "runtime.json"),
        ):
            if not os.path.isfile(lp) or not cls._descriptor_path_is_trusted(lp, ws):
                continue
            try:
                if (now - os.path.getmtime(lp)) > cls._DESCRIPTOR_MAX_AGE_SEC:
                    continue
                with open(lp, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for sec in ("frontend", "backend"):
                    url = (data.get(sec) or {}).get("url")
                    if not url:
                        continue
                    parsed = urllib.parse.urlparse(url)
                    if not parsed.port:
                        continue
                    port = int(parsed.port)
                    if port in ports:
                        continue
                    if cls._is_port_open(parsed.hostname or "127.0.0.1", port):
                        ports.append(port)
            except Exception:
                pass
        return ports

    # 1. Tool: oumv_status
    @classmethod
    def status(cls, workspace_dir: Optional[str] = None) -> Dict[str, Any]:
        cls.mark_oumv_used(workspace_dir)
        ws = workspace_dir or get_active_workspace() or os.getcwd()
        cls.scan(workspace_dir=ws)
        # A descriptor whose port is closed must not be reported as a live runtime.
        dropped = cls._drop_stale_descriptor_endpoints(ws)
        st = cls._state_for(ws)
        fe, be = st["fe"], st["be"]

        # Out-of-the-box stack detection
        stack_info = {}
        try:
            from src.retrofit import detect_project_stack
            stack_info = detect_project_stack(ws)
        except Exception:
            pass

        be_stack = stack_info.get("backend") or {}
        be_unsupported = bool(be_stack) and not be_stack.get("oumv_supported", True) and not be

        result: Dict[str, Any] = {
            "mode": "OUT_OF_THE_BOX_HYBRID",
            "frontend": fe or {
                "status": "DISCONNECTED",
                "framework": (stack_info.get("frontend") or {}).get("framework", "None"),
                "tip": "Static AST analysis active. Run 'tokensafe dev' (zero repo footprint) for live Vite+React OUMV.",
            },
            "backend": be or ({
                "status": "UNSUPPORTED",
                "framework": be_stack.get("framework", "node"),
                "provider": be_stack.get("provider", "node"),
                "tip": f"No OUMV runtime adapter for {be_stack.get('framework', 'Node.js')} yet (planned). Use repo_map, smart_read_file, and scoped_search for backend code.",
            } if be_unsupported else {
                "status": "DISCONNECTED",
                "framework": be_stack.get("framework", "None"),
                "tip": "Static AST analysis active. When backend DevServer starts (port 8080/8000), live OUMV auto-connects.",
            }),
            "adapters": cls._adapter_lifecycle(stack_info, fe, be),
            "staticIntelligence": "ACTIVE (L0/L1/L2 AST observation & cross-stack linking ready)",
            "detectedStack": stack_info,
            "workspace": ws,
        }
        if dropped:
            result["staleRuntimes"] = [
                {
                    "kind": d.get("kind"),
                    "url": d.get("url"),
                    "reason": "runtime descriptor present but its port is closed (crashed dev server?)",
                }
                for d in dropped
            ]
        return result

    @staticmethod
    def _adapter_lifecycle(stack_info: Dict[str, Any], fe: Optional[Dict[str, Any]], be: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Per-adapter readiness per the OUMV Adapter Contract (schemas/adapter.json).

        Every entry carries ONE machine-readable readiness verdict (`status`, mirrored
        by `effective`): active > supported > partial > planned > unsupported. A runtime
        that answers UNSUPPORTED for a detected stack can no longer be reported as a
        `ready` adapter from the catalog at the same time.
        """
        adapters: List[Dict[str, Any]] = []
        fe_info = stack_info.get("frontend") or {}
        be_info = stack_info.get("backend") or {}
        fe_detected = bool(fe_info)
        be_detected = bool(be_info)
        if not fe_detected and not be_detected:
            return adapters

        try:
            from src.retrofit import get_adapters_catalog
            catalog = get_adapters_catalog()
        except Exception:
            catalog = []

        def _find_adapter(profile: str, provider: Optional[str]) -> Dict[str, Any]:
            """Exact catalog match only — never substitute a ready adapter of the same
            profile for a different provider (that is how planned stacks looked ready)."""
            if provider:
                for a in catalog:
                    if a.get("id") == f"{profile}/{provider}":
                        return a
            return {}

        def _entry(profile: str, connected: Optional[Dict[str, Any]], detected: bool, info: Dict[str, Any]) -> Dict[str, Any]:
            provider = info.get("provider")
            adapter = _find_adapter(profile, provider)
            catalog_status = adapter.get("status", "planned")
            catalog_ready = catalog_status == "ready"
            exact_match = bool(adapter)
            detection_planned = info.get("status") == "planned" or info.get("oumv_supported") is False

            if connected:
                effective = "active"
            elif not detected:
                effective = "unsupported"
            elif detection_planned or not catalog_ready:
                effective = "planned"
            elif not exact_match:
                # A ready adapter exists for the profile but not for this provider.
                effective = "partial"
            else:
                effective = "supported"

            if effective == "active":
                state, health = "ACTIVE", "healthy"
                detail = f"Live runtime: {connected.get('name', 'unknown')}"
            elif effective in ("supported", "partial"):
                state, health = "HANDSHAKE", "degraded"
                detail = (
                    f"Detected statically ({info.get('framework', 'unknown')}); runtime offline — "
                    "hybrid static fallback active"
                )
                if effective == "partial":
                    detail += f"; no exact adapter for {profile}/{provider} yet"
            elif effective == "planned":
                state, health = "UNSUPPORTED", "unsupported"
                detail = (
                    f"{f'{profile}/{provider}' if provider else profile} has no implemented OUMV runtime adapter yet. "
                    "Do not use tokensafe:oumv for this profile — use repo_map, smart_read_file, and scoped_search instead."
                )
            else:
                state, health = "DISCOVER", "unavailable"
                detail = "Not detected in this workspace"

            caps = adapter.get("capabilities") if effective in ("active", "supported", "partial") else []
            return {
                "profile": profile,
                "adapter": adapter.get("id", f"{profile}/{provider or 'unknown'}"),
                "status": effective,
                "effective": effective,
                "catalogStatus": catalog_status,
                "technology": adapter.get("technology"),
                "capabilities": caps,
                "lifecycle": {"state": state, "health": {"status": health, "detail": detail}},
                "security": {
                    "productionFailClosed": True,
                    "workspaceBoundary": True,
                },
            }

        adapters.append(_entry("frontend", fe, fe_detected, fe_info))
        adapters.append(_entry("backend", be, be_detected, be_info))
        return adapters

    @staticmethod
    def _unsupported_backend_notice(workspace_dir: Optional[str], runtime: str) -> Optional[Dict[str, Any]]:
        if runtime != "backend":
            return None
        ws = workspace_dir or get_active_workspace()
        try:
            from src.retrofit import detect_project_stack
            stack = detect_project_stack(ws)
            be = stack.get("backend") or {}
            if be and not be.get("oumv_supported", True):
                fe = stack.get("frontend")
                supported = [f"frontend/{fe['provider']}"] if fe and fe.get("oumv_supported", True) else []
                return {
                    "unsupportedRuntime": f"backend/{be.get('provider', 'node')}",
                    "framework": be.get("framework"),
                    "supportedProfiles": supported,
                    "guidance": (
                        f"OUMV runtime adapter for {be.get('framework', 'Node.js')} is not available yet (planned). "
                        "Use tokensafe:repo_map, tokensafe:smart_read_file, and tokensafe:scoped_search for this backend; "
                        + ("keep using tokensafe:oumv(runtime='frontend') for UI." if supported else "use standard tests for verification.")
                    ),
                }
        except Exception:
            pass
        return None

    # 2. Tool: oumv_observe
    @classmethod
    def observe(cls, level: str = "L0", target: Optional[str] = None, runtime: str = "auto", workspace_dir: Optional[str] = None) -> Any:
        cls.mark_oumv_used(workspace_dir)
        fe, be = cls.get_endpoints(workspace_dir=workspace_dir)
        if runtime == "backend" and not be:
            notice = cls._unsupported_backend_notice(workspace_dir, runtime)
            if notice:
                from src.graph_engine import GraphEngine
                ws = workspace_dir or get_active_workspace()
                res = GraphEngine.get_instance(ws).get_static_oumv_slice(level=level, target=target)
                res.update(notice)
                return res

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
        cls.mark_oumv_used(workspace_dir)
        fe, be = cls.get_endpoints(workspace_dir=workspace_dir)
        if runtime == "backend" and not be:
            notice = cls._unsupported_backend_notice(workspace_dir, runtime)
            if notice:
                return notice

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

        # Cross-layer causality query. Handled for every runtime value: with an
        # explicit runtime this used to skip the static backbone entirely, and
        # offline it fell through to the generic slice below.
        if query == "causality":
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

        # Static L2 flow (offline contract): handler -> callees -> literal route.
        if query == "flow":
            report = graph.get_static_flow(target or "")
            report.setdefault("query", "flow")
            return report

        try:
            # A live adapter may accept queries beyond the offline contract.
            url = cls._resolve_client_url(runtime=runtime, target=target, workspace_dir=workspace_dir)
            return cls._rpc_call(url, "oumv/understand", {"query": query, "target": target, "componentId": target})
        except RuntimeError:
            # Prose in `query` is not a question: the offline contract answers
            # impact/causality/flow only. Returning a surface dump here looked like
            # an answer (level "L1", confidence "confirmed") to a query nobody parsed.
            return {
                "query": query,
                "target": target,
                "error": "UNSUPPORTED_QUERY",
                "mode": "STATIC_SURFACE",
                "supportedQueries": ["impact", "causality", "flow"],
                "hint": (
                    "query is an enum, not text: 'impact' answers who depends on the target, "
                    "'causality' answers why state X / how frontend calls map to endpoints, "
                    "'flow' answers which client routes a handler reaches (L2). "
                    "For 'what this handler does', read the symbol with smart_read_file."
                ),
                "evidence": {"provenance": "static", "confidence": "confirmed", "sourceAdapter": "tokensafe-graph-engine"},
            }

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
        except RuntimeError as e:
            if isinstance(e, OUMVRpcError) and not isinstance(e, OUMVUnsupportedMethodError):
                if not any(hint in str(e).lower() for hint in _UNSUPPORTED_HINTS):
                    # Explicit safety rejection (conflict, syntax, permission): obey it.
                    raise
                # The runtime cannot mediate the edit at all; the offline path below
                # performs the same guarded edit and reports why.
                e = OUMVUnsupportedMethodError(str(e))
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
                if isinstance(e, OUMVUnsupportedMethodError):
                    # The runtime was reachable but has no applyEdit: the edit was
                    # mediated by the gateway instead. Say so instead of implying
                    # the runtime verified it.
                    result["mode"] = "RUNTIME_LACKS_APPLYEDIT_OFFLINE_FALLBACK"
                    result["runtimeFallbackReason"] = str(e)
                    result["message"] = (
                        "Live runtime does not implement oumv/applyEdit; edit applied by the gateway "
                        "with the same OCC, syntax-guard and backup checks"
                    )
                return result
            raise

    # 5. Tool: oumv_verify
    @classmethod
    def verify(cls, target: str, action: str = "invoke", payload: Optional[dict] = None, runtime: str = "auto", workspace_dir: Optional[str] = None, sandbox: Optional[dict] = None) -> Any:
        cls.mark_oumv_used(workspace_dir)
        if runtime == "backend":
            _, be = cls.get_endpoints(workspace_dir=workspace_dir)
            if not be:
                notice = cls._unsupported_backend_notice(workspace_dir, runtime)
                if notice:
                    return {"status": "unsupported", **notice}
        url = cls._resolve_client_url(runtime=runtime, target=target, workspace_dir=workspace_dir)
        params = {
            "target": target,
            "componentId": target,
            "action": action,
            "payload": payload or {},
        }
        # spec §4 (wire 1.4.0): the runtime decides whether it can honour the
        # sandbox; we forward the request instead of guessing.
        if isinstance(sandbox, dict) and sandbox.get("mode"):
            params["sandbox"] = sandbox
        res = cls._rpc_call(url, "oumv/verify", params)
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

    # 7. Tool: oumv index — repo-wide symbol search (spec §14, optional extension)
    _INDEX_MATCH_CAP = 50

    @classmethod
    def index(cls, query: str, kinds: Optional[List[str]] = None, limit: int = 25, workspace_dir: Optional[str] = None) -> Dict[str, Any]:
        """Repo-wide symbol search over the static code graph (spec `oumv/index`).

        Closes the largest remaining agent token sink: without it, "where is X
        defined / who calls Y" needs raw grep. Ranking: exact name > prefix >
        qualified-suffix > substring; reverse-edge count (who imports/calls this
        symbol) breaks ties. Falls back to the FTS5 content index when the graph
        holds nothing for the query, and labels that fallback honestly.
        """
        from src.graph_engine import GraphEngine
        ws = workspace_dir or get_active_workspace() or os.getcwd()
        q = (query or "").strip()
        if not q:
            return {
                "workspace": ws,
                "query": query,
                "error": "VALIDATION_FAILED",
                "hint": "query is required (a symbol name, Type.member, or substring)",
            }
        kind_filter = {k.strip().lower() for k in (kinds or []) if str(k).strip()}

        graph = GraphEngine.get_instance(ws)
        q_lower = q.lower()
        scored: List[Tuple[int, int, str, Any]] = []  # (rank, -refs, sym_key, sym)
        for key, sym in getattr(graph, "symbols", {}).items():
            name = (sym.name or "").lower()
            if not name:
                continue
            if q_lower == name:
                rank = 0
            elif name.startswith(q_lower):
                rank = 1
            elif key.lower().endswith("." + q_lower) or key.lower().endswith("::" + q_lower) or name.endswith("." + q_lower):
                rank = 2
            elif q_lower in name or q_lower in (sym.signature or "").lower():
                rank = 3
            else:
                continue
            if kind_filter and sym.kind.lower() not in kind_filter:
                continue
            refs = len(getattr(graph, "rev_edges", {}).get(key, ()) or ())
            scored.append((rank, -refs, key, sym))

        scored.sort(key=lambda item: (item[0], item[1], item[2]))
        matches = [
            {
                "resourceId": f"{sym.kind}:{sym.name}@{sym.file_path}",
                "name": sym.name,
                "kind": sym.kind,
                "file": sym.file_path,
                "lines": [int(sym.line_start or 0), int(sym.line_end or 0)],
                "signature": (sym.signature or "").strip(),
                "refs": -neg_refs,
                "match": {0: "exact", 1: "prefix", 2: "qualified-suffix", 3: "substring"}[rank],
            }
            for rank, neg_refs, _key, sym in scored[:limit]
        ]
        provenance = "static-graph"
        if not matches:
            try:
                from src.fts_index import CodeFTSIndex
                fts_rows = CodeFTSIndex(ws).search(q, limit=limit) or []
                matches = [
                    {
                        "resourceId": f"text:{row.get('file')}:{row.get('line')}",
                        "name": str(row.get("content", ""))[:120],
                        "kind": "text",
                        "file": row.get("file"),
                        "lines": [int(row.get("line") or 0), int(row.get("line") or 0)],
                        "signature": "",
                        "refs": 0,
                        "match": "fts-content",
                    }
                    for row in fts_rows
                ]
                provenance = "fts-content-index"
            except Exception:
                matches = []
        matches = matches[: cls._INDEX_MATCH_CAP]
        return {
            "workspace": ws,
            "query": q,
            "matches": matches,
            "totalMatches": len(scored) if provenance == "static-graph" else len(matches),
            "truncated": (len(scored) if provenance == "static-graph" else len(matches)) > len(matches),
            "provenance": provenance,
            "evidence": {"provenance": "static", "confidence": "confirmed", "sourceAdapter": "tokensafe-graph-engine"},
        }

    # 8. Tool: oumv preview — dry-run diff before commit (spec `oumv/previewEdit`)
    @classmethod
    def preview(cls, path: str, symbol: Optional[str] = None, replacement: Optional[str] = None,
                expected_version: Optional[str] = None, batch: Optional[List[dict]] = None,
                runtime: str = "auto", workspace_dir: Optional[str] = None) -> Any:
        """Dry-run an edit: returns the would-be result WITHOUT writing anything.

        Delegates to a live runtime implementing `oumv/previewEdit` when one is
        connected. Without a live runtime, only the honest subset is served
        offline: a whole-file replacement preview (real unified diff against the
        current bytes + syntax check). Symbol-level offline preview is refused
        instead of guessing a diff the runtime never computed.
        """
        ws = workspace_dir or get_active_workspace() or os.getcwd()
        params: Dict[str, Any] = {
            "path": path,
            "filePath": path,
            "symbol": symbol,
            "replacement": replacement,
            "source": replacement,
            "expectedVersion": expected_version,
        }
        if batch:
            params["edits"] = batch
        try:
            url = cls._resolve_client_url(runtime=runtime, target=path, workspace_dir=workspace_dir)
            res = cls._rpc_call(url, "oumv/previewEdit", params)
            if isinstance(res, dict):
                res.setdefault("mode", "LIVE_RUNTIME_PREVIEW")
            return res
        except OUMVRpcError as e:
            if not any(hint in str(e).lower() for hint in _UNSUPPORTED_HINTS):
                raise  # explicit protocol rejection (conflict/syntax/permission) — obey it
            raise OUMVUnsupportedMethodError(str(e))
        except RuntimeError as e:
            if not any(hint in str(e).lower() for hint in _UNSUPPORTED_HINTS):
                offline = {
                    "mode": "OFFLINE_FULL_FILE_PREVIEW",
                    "path": path,
                    "workspace": ws,
                    "requiresRuntime": True,
                    "hint": (
                        "mutate.preview needs a live OUMV runtime implementing oumv/previewEdit "
                        "(wire 1.6.0+). Only whole-file replacement previews work offline."
                    ),
                }
                if symbol:
                    offline["error"] = "UNSUPPORTED_OFFLINE"
                    offline["detail"] = "symbol-level preview needs the runtime's AST; nothing was computed or written"
                    return offline
                if replacement is None:
                    offline["error"] = "VALIDATION_FAILED"
                    offline["detail"] = "offline preview requires 'replacement' (whole-file content)"
                    return offline
                abs_path = os.path.normpath(os.path.join(ws, path))
                if not abs_path.startswith(os.path.abspath(ws)):
                    return {"mode": "OFFLINE_FULL_FILE_PREVIEW", "error": "SECURITY_VIOLATION", "detail": f"Path traversal blocked: {path}"}
                current = ""
                if os.path.isfile(abs_path):
                    with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                        current = f.read()
                import difflib
                diff_lines = list(difflib.unified_diff(
                    current.splitlines(keepends=True),
                    (replacement or "").splitlines(keepends=True),
                    fromfile=f"a/{path}", tofile=f"b/{path}",
                ))
                offline["diff"] = "".join(diff_lines)
                offline["unchanged"] = current == (replacement or "")
                offline["currentVersion"] = cls._hash_version(abs_path)
                from src.syntax_guard import check_syntax
                offline["syntaxCheck"] = check_syntax(abs_path, replacement)
                return offline
            raise

    @staticmethod
    def _hash_version(abs_path: str) -> Optional[str]:
        import hashlib as _hashlib
        if not os.path.isfile(abs_path):
            return None
        with open(abs_path, "rb") as f:
            return "sha256:" + _hashlib.sha256(f.read()).hexdigest()
