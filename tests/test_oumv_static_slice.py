"""
Unit tests for the static OUMV slice (Level 0/1) and static impact analysis
in TokenSafe CodeGraph: cross-stack relationships (frontend API call ->
backend endpoint) with evidence metadata, HTTP method capture, and honest
impact reporting.
"""

import os
import shutil
import tempfile
import unittest

from src.graph_engine import CodeGraph


def _write(path: str, content: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


class TestStaticOumvSlice(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="tokensafe_slice_test_")
        # Fullstack fixture: FastAPI backend + React frontend calling it
        _write(os.path.join(self.test_dir, "backend", "users.py"), """from fastapi import APIRouter

router = APIRouter()

@router.get("/api/v1/users/{id}")
def get_user_profile(id: int):
    return {"id": id}

@router.post("/api/v1/orders")
def create_order(payload: dict):
    return {"ok": True}
""")
        _write(os.path.join(self.test_dir, "frontend", "src", "api.ts"), """export async function fetchUserProfile(userId: string) {
    return axios.get(`/api/v1/users/${userId}`);
}

export async function submitOrder(order: any) {
    return axios.post('/api/v1/orders', order);
}
""")
        self.graph = CodeGraph(self.test_dir, use_disk_cache=False)
        self.graph.scan()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_l0_pulse_counts_relationships(self):
        res = self.graph.get_static_oumv_slice(level="L0")
        self.assertEqual(res["mode"], "STATIC_OFFLINE_PULSE")
        self.assertGreaterEqual(res["totalEndpoints"], 2)
        self.assertGreaterEqual(res["totalClientCalls"], 2)
        self.assertGreaterEqual(res["totalRelationships"], 2)
        self.assertIn("cross-stack links", res["summary"])

    def test_l1_slice_exports_relationships_with_evidence(self):
        res = self.graph.get_static_oumv_slice(level="L1")
        self.assertEqual(res["mode"], "STATIC_SURFACE")
        self.assertTrue(res["relationships"])
        rel = res["relationships"][0]
        # Canonical OUMV resource references
        self.assertEqual(rel["source"]["type"], "frontend.action")
        self.assertEqual(rel["target"]["type"], "backend.endpoint")
        self.assertEqual(rel["source"]["file"], "frontend/src/api.ts")
        self.assertEqual(rel["target"]["file"], "backend/users.py")
        # Evidence metadata (spec §6.2)
        self.assertEqual(rel["evidence"]["provenance"], "static")
        self.assertEqual(rel["evidence"]["sourceAdapter"], "tokensafe-graph-engine")
        self.assertEqual(rel["evidence"]["confidence"], "probable")

    def test_l1_relationships_filtered_by_target(self):
        res = self.graph.get_static_oumv_slice(level="L1", target="orders")
        self.assertTrue(res["relationships"])
        for rel in res["relationships"]:
            hay = (rel["source"]["route"] or "") + (rel["target"]["route"] or "")
            self.assertIn("orders", hay.lower())

    def test_http_methods_captured_on_endpoints_and_calls(self):
        res = self.graph.get_static_oumv_slice(level="L1")
        methods = {(e["route"], e["method"]) for e in res["endpoints"]}
        self.assertIn(("/api/v1/users/*", "GET"), methods)
        self.assertIn(("/api/v1/orders", "POST"), methods)
        call_methods = {(c["route"], c["method"]) for c in res["clientCalls"]}
        self.assertIn(("/api/v1/users/*", "GET"), call_methods)
        self.assertIn(("/api/v1/orders", "POST"), call_methods)

    def test_relationships_include_http_methods(self):
        res = self.graph.get_static_oumv_slice(level="L1")
        by_route = {
            rel["target"]["route"]: rel for rel in res["relationships"]
        }
        self.assertEqual(by_route["/api/v1/users/*"]["source"]["method"], "GET")
        self.assertEqual(by_route["/api/v1/users/*"]["target"]["method"], "GET")
        self.assertEqual(by_route["/api/v1/orders"]["source"]["method"], "POST")

    def test_get_static_impact_frontend_callers_of_endpoint(self):
        res = self.graph.get_static_impact("/api/v1/orders")
        self.assertEqual(res["mode"], "STATIC_IMPACT")
        self.assertGreaterEqual(res["crossStack"]["count"], 1)
        caller = res["crossStack"]["frontendCallers"][0]
        self.assertEqual(caller["source"]["type"], "frontend.action")
        self.assertEqual(caller["source"]["file"], "frontend/src/api.ts")
        self.assertEqual(caller["target"]["route"], "/api/v1/orders")
        # Honest reporting: missing runtime evidence is explicit
        self.assertTrue(res["missingEvidence"])
        self.assertEqual(res["evidence"]["provenance"], "static")

    def test_get_static_impact_requires_target(self):
        res = self.graph.get_static_impact("")
        self.assertIn("error", res)

    def test_get_static_impact_symbol_dependents(self):
        res = self.graph.get_static_impact("users.py")
        self.assertIn("directDependents", res)
        self.assertIn("transitiveDependents", res)
        files = [d.get("file") for d in res["directDependents"] if d.get("type") == "file"]
        self.assertIn("frontend/src/api.ts", files)

    def test_requested_level_above_l1_is_reported_as_degraded(self):
        res = self.graph.get_static_oumv_slice(level="L3")
        self.assertEqual(res["level"], "L1")
        self.assertEqual(res["requestedLevel"], "L3")
        self.assertTrue(res["degraded"])
        self.assertIn("live OUMV runtime", res["degradedReason"])

    def test_served_level_carries_no_degradation_marker(self):
        res = self.graph.get_static_oumv_slice(level="L1")
        self.assertNotIn("degraded", res)
        self.assertNotIn("requestedLevel", res)

    def test_matching_symbols_accept_a_file_path_target(self):
        res = self.graph.get_static_oumv_slice(level="L1", target="frontend/src/api.ts")
        files = {sym["file"] for sym in res["matchingSymbols"]}
        self.assertIn("frontend/src/api.ts", files)

    def test_client_calls_capture_local_http_wrapper_literals(self):
        _write(os.path.join(self.test_dir, "frontend", "src", "wheel.ts"), """import { request } from './client'

export async function spinWheel() {
    return request('/api/v1/wheel/spin')
}
""")
        self.graph.scan()
        res = self.graph.get_static_oumv_slice(level="L1", target="wheel")
        routes = {call["route"] for call in res["clientCalls"]}
        self.assertIn("/api/v1/wheel/spin", routes)


if __name__ == "__main__":
    unittest.main()
