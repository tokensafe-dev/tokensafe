"""
Unit tests for the unified cross-stack intelligence graph (src/cross_stack.py)
and the OUMVGateway.graph entry point (spec §11).
"""

import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from src.cross_stack import build_unified_graph, who_depends_on, impact_from_graph
from src.oumv_gateway import OUMVGateway


def _write(path: str, content: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


class TestCrossStackGraph(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="tokensafe_cross_stack_")
        _write(os.path.join(self.test_dir, "backend", "main.py"), """from fastapi import FastAPI

app = FastAPI()

@app.get("/api/orders")
def list_orders():
    return []
""")
        _write(os.path.join(self.test_dir, "frontend", "src", "api.ts"), """export async function loadOrders() {
    return fetch('/api/orders');
}
""")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _build(self, rpc_call=None, fe=None, be=None):
        return build_unified_graph(workspace_dir=self.test_dir, fe=fe, be=be, rpc_call=rpc_call)

    def test_unified_graph_contains_static_nodes_and_edges(self):
        g = self._build()
        self.assertEqual(g["mode"], "UNIFIED_CROSS_STACK_GRAPH")
        types = {n["type"] for n in g["nodes"]}
        self.assertIn("backend.endpoint", types)
        self.assertIn("frontend.action", types)
        self.assertTrue(g["edges"])
        edge = g["edges"][0]
        self.assertEqual(edge["relation"], "calls")
        self.assertEqual(edge["evidence"]["provenance"], "static")
        # Honest missing-evidence note is always present
        self.assertTrue(g["missingEvidence"])
        self.assertEqual(g["runtimes"]["frontend"], "offline")
        self.assertEqual(g["runtimes"]["backend"], "offline")

    def test_runtime_backend_confirms_endpoints(self):
        def fake_rpc(url, method, params):
            if method == "oumv/observe" and params.get("level") == "L1":
                return {"endpoints": [{"method": "GET", "path": "/api/orders"}]}
            return {}

        g = self._build(fe=None, be={"url": "http://localhost:8000/__oumv/rpc"}, rpc_call=fake_rpc)
        confirmed = [n for n in g["nodes"] if n.get("runtimeConfirmed")]
        self.assertEqual(len(confirmed), 1)
        self.assertEqual(confirmed[0]["type"], "backend.endpoint")
        self.assertIn("GET:/api/orders", confirmed[0]["id"])
        self.assertEqual(g["runtimes"]["backend"], "live")
        self.assertGreaterEqual(g["provenanceSummary"]["runtimeConfirmedEndpoints"], 1)

    def test_runtime_backend_adds_unknown_endpoint(self):
        def fake_rpc(url, method, params):
            return {"endpoints": [{"method": "POST", "path": "/api/health/live"}]}

        g = self._build(be={"url": "x"}, rpc_call=fake_rpc)
        live_nodes = [n for n in g["nodes"] if n["evidence"]["provenance"] == "runtime"]
        self.assertEqual(len(live_nodes), 1)
        self.assertEqual(live_nodes[0]["route"], "/api/health/live")

    def test_runtime_frontend_surface_adds_component_nodes(self):
        def fake_rpc(url, method, params):
            return {"components": [{"id": "orders-app"}]}

        g = self._build(fe={"url": "x"}, rpc_call=fake_rpc)
        comps = [n for n in g["nodes"] if n["type"] == "frontend.component"]
        self.assertEqual(len(comps), 1)
        self.assertEqual(comps[0]["id"], "frontend.component:orders-app")
        self.assertEqual(comps[0]["evidence"]["provenance"], "runtime")

    def test_who_depends_on_finds_frontend_callers(self):
        g = self._build()
        res = who_depends_on("/api/orders", g)
        self.assertEqual(res["query"], "who_depends_on")
        self.assertTrue(res["frontendCallers"])
        self.assertIn("frontend.action", res["frontendCallers"][0]["source"])

    def test_who_depends_on_requires_target(self):
        res = who_depends_on("", self._build())
        self.assertIn("error", res)

    def test_impact_from_graph_suggests_verification(self):
        g = self._build()
        res = impact_from_graph("/api/orders", g)
        self.assertTrue(res["frontendCallers"])
        self.assertTrue(res["suggestedVerification"])
        self.assertTrue(any("oumv_verify" in step for step in res["suggestedVerification"]))

    def test_rpc_failure_degrades_gracefully(self):
        def broken_rpc(url, method, params):
            raise RuntimeError("offline")

        g = self._build(fe={"url": "x"}, be={"url": "x"}, rpc_call=broken_rpc)
        # Static backbone still present; discovered-but-unreachable runtimes reported honestly
        self.assertTrue(g["nodes"])
        self.assertEqual(g["runtimes"]["frontend"], "unreachable")
        self.assertEqual(g["runtimes"]["backend"], "unreachable")


class TestGatewayGraphTool(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="tokensafe_gateway_graph_")
        _write(os.path.join(self.test_dir, "backend", "main.py"),
               "from fastapi import FastAPI\napp = FastAPI()\n\n@app.post('/api/items')\ndef create_item():\n    return {}\n")
        _write(os.path.join(self.test_dir, "web", "client.ts"),
               "export async function addItem() {\n    return axios.post('/api/items', {});\n}\n")
        OUMVGateway._cached_fe = None
        OUMVGateway._cached_be = None
        OUMVGateway._last_scan_time = 0.0

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_gateway_graph_overview_and_who_depends(self):
        with patch("src.oumv_gateway.get_active_workspace", return_value=self.test_dir):
            OUMVGateway._last_scan_time = 9999999999.0
            overview = OUMVGateway.graph(query="overview", target="/api/items")
            self.assertEqual(overview["mode"], "UNIFIED_CROSS_STACK_GRAPH")
            self.assertTrue(overview["edges"])

            deps = OUMVGateway.graph(query="who_depends_on", target="/api/items")
            self.assertTrue(deps["frontendCallers"])

            impact = OUMVGateway.graph(query="impact", target="/api/items")
            self.assertTrue(impact["suggestedVerification"])


if __name__ == "__main__":
    unittest.main()
