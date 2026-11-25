"""
Unified Cross-Stack Intelligence Graph (OUMV spec §11).

Merges three independent evidence sources into one resource graph:
1. Static analysis (frontend API call sites -> backend endpoints, from the AST graph engine).
2. Live frontend runtime surface (mounted components / actions, provenance "runtime").
3. Live backend runtime surface (endpoints actually served, provenance "runtime").

Every node and edge carries EvidenceMetadata (spec §6.2). Missing runtime evidence
is reported explicitly instead of being inferred.
"""

import time
from typing import Any, Callable, Dict, List, Optional


def _static_evidence() -> Dict[str, str]:
    return {"provenance": "static", "sourceAdapter": "tokensafe-graph-engine", "confidence": "probable"}


def _runtime_evidence(source: str) -> Dict[str, str]:
    return {"provenance": "runtime", "sourceAdapter": source, "confidence": "confirmed"}


def build_unified_graph(
    workspace_dir: Optional[str] = None,
    fe: Optional[Dict[str, Any]] = None,
    be: Optional[Dict[str, Any]] = None,
    rpc_call: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """Builds the unified cross-stack graph for a workspace.

    Args:
        workspace_dir: workspace root (defaults to the active workspace).
        fe / be: live runtime descriptors from OUMVGateway.get_endpoints().
        rpc_call: callable(url, method, params) used to observe live surfaces.
    """
    from src.graph_engine import GraphEngine
    from src.paths import get_active_workspace

    ws = workspace_dir or get_active_workspace()
    static = GraphEngine.get_instance(ws)

    nodes: Dict[str, Dict[str, Any]] = {}
    edges: List[Dict[str, Any]] = []

    def _endpoint_node_id(method: Optional[str], route: str) -> str:
        return f"backend.endpoint:{method or 'ANY'}:{route}"

    # 1. Static backend endpoints
    for rel_path, eps in getattr(static, "file_endpoints", {}).items():
        for route, sym, method in eps:
            node_id = _endpoint_node_id(method, route)
            nodes.setdefault(node_id, {
                "id": node_id,
                "type": "backend.endpoint",
                "route": route,
                "method": method,
                "file": rel_path,
                "symbol": sym,
                "evidence": _static_evidence(),
            })

    # 2. Static frontend action (API call) sites
    for rel_path, calls in getattr(static, "file_api_calls", {}).items():
        for route, sym, method in calls:
            node_id = f"frontend.action:{method or 'ANY'}:{route}:{rel_path}:{sym or ''}"
            nodes.setdefault(node_id, {
                "id": node_id,
                "type": "frontend.action",
                "route": route,
                "method": method,
                "file": rel_path,
                "symbol": sym,
                "evidence": _static_evidence(),
            })

    # 3. Static cross-stack edges (frontend call -> backend endpoint)
    for link in getattr(static, "api_links", []):
        source = link["source"]
        target = link["target"]
        src_id = f"frontend.action:{source.get('method') or 'ANY'}:{source.get('route')}:{source.get('file')}:{source.get('symbol') or ''}"
        dst_id = _endpoint_node_id(target.get("method"), target.get("route") or "")
        edges.append({
            "source": src_id,
            "target": dst_id,
            "relation": "calls",
            "evidence": _static_evidence(),
        })

    runtime_fe_ok = False
    runtime_be_ok = False

    # 4. Live backend surface: confirms endpoints actually served
    if be and rpc_call:
        try:
            surface = rpc_call(be["url"], "oumv/observe", {"level": "L1"})
            if isinstance(surface, dict):
                runtime_be_ok = True
                for ep in surface.get("endpoints", []) or []:
                    node_id = _endpoint_node_id(ep.get("method"), ep.get("path") or ep.get("route") or "")
                    if not node_id.endswith(":"):
                        if node_id in nodes:
                            nodes[node_id]["runtimeConfirmed"] = True
                        else:
                            nodes[node_id] = {
                                "id": node_id,
                                "type": "backend.endpoint",
                                "route": ep.get("path") or ep.get("route"),
                                "method": ep.get("method"),
                                "evidence": _runtime_evidence("live-backend-runtime"),
                            }
        except Exception:
            pass

    # 5. Live frontend surface: mounted components / actions
    if fe and rpc_call:
        try:
            surface = rpc_call(fe["url"], "oumv/observe", {"level": "L1"})
            if isinstance(surface, dict):
                runtime_fe_ok = True
                for comp in surface.get("components", []) or surface.get("elements", []) or []:
                    comp_id = comp.get("id") or comp.get("agentId") or comp.get("componentId")
                    if not comp_id:
                        continue
                    node_id = f"frontend.component:{comp_id}"
                    nodes.setdefault(node_id, {
                        "id": node_id,
                        "type": "frontend.component",
                        "evidence": _runtime_evidence("live-frontend-runtime"),
                    })
        except Exception:
            pass

    provenance_summary = {
        "staticNodes": sum(1 for n in nodes.values() if n["evidence"]["provenance"] == "static"),
        "runtimeNodes": sum(1 for n in nodes.values() if n["evidence"]["provenance"] == "runtime"),
        "runtimeConfirmedEndpoints": sum(1 for n in nodes.values() if n.get("runtimeConfirmed")),
        "edges": len(edges),
    }

    return {
        "mode": "UNIFIED_CROSS_STACK_GRAPH",
        "workspace": ws,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "nodes": list(nodes.values()),
        "edges": edges,
        "provenanceSummary": provenance_summary,
        "missingEvidence": [
            "Frontend call origins are statically inferred; runtime fetch tracing is not yet recorded by the frontend bridge.",
        ],
        "runtimes": {
            "frontend": "live" if runtime_fe_ok else ("unreachable" if fe else "offline"),
            "backend": "live" if runtime_be_ok else ("unreachable" if be else "offline"),
        },
    }


def who_depends_on(target: str, graph: Dict[str, Any]) -> Dict[str, Any]:
    """Answers §11.4: which frontend resources call a given backend endpoint."""
    t = (target or "").strip()
    result: Dict[str, Any] = {
        "query": "who_depends_on",
        "target": t,
        "frontendCallers": [],
        "runtimeConfirmed": False,
        "evidence": _static_evidence(),
    }
    if not t:
        result["error"] = "target is required (e.g. '/api/orders' or 'POST /api/orders')"
        return result

    t_lower = t.lower()
    for edge in graph.get("edges", []):
        target_route = (edge.get("target") or "").lower()
        if t_lower in target_route:
            result["frontendCallers"].append({
                "source": edge["source"],
                "relation": "calls",
                "evidence": edge.get("evidence"),
            })

    nodes = {n["id"]: n for n in graph.get("nodes", [])}
    for node_id, node in nodes.items():
        if node.get("type") == "backend.endpoint" and t_lower in f"{node.get('method') or ''} {node.get('route') or ''}".lower():
            if node.get("runtimeConfirmed"):
                result["runtimeConfirmed"] = True

    result["frontendCallers"] = result["frontendCallers"][:30]
    return result


def impact_from_graph(target: str, graph: Dict[str, Any]) -> Dict[str, Any]:
    """Graph-based impact view: affected cross-stack callers with suggested verification."""
    callers = who_depends_on(target, graph)
    verification_steps = [
        f"oumv_verify(target='POST {target}') — in-memory endpoint check",
    ]
    for caller in callers.get("frontendCallers", [])[:3]:
        parts = caller["source"].split(":")
        route_hint = parts[2] if len(parts) > 2 else caller["source"]
        verification_steps.append(
            f"oumv_understand(query='causality', target='{route_hint}') — confirm the calling code still matches"
        )
    return {
        "query": "impact",
        "target": target,
        "frontendCallers": callers.get("frontendCallers", []),
        "runtimeConfirmed": callers.get("runtimeConfirmed", False),
        "suggestedVerification": verification_steps,
        "missingEvidence": graph.get("missingEvidence", []),
    }
