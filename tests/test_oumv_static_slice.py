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


class TestPrefixComposition(unittest.TestCase):
    """Routes are not what clients call: prefixes come from the framework
    constructor (Elysia/FastAPI) or from the mount site (app.use)."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="tokensafe_prefix_test_")
        self.graph = None

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _scan(self):
        self.graph = CodeGraph(self.test_dir, use_disk_cache=False)
        self.graph.scan()
        return self.graph

    def test_elysia_constructor_prefix_and_chained_routes(self):
        _write(os.path.join(self.test_dir, "backend", "wheel.ts"), """import { Elysia } from "elysia";

export const wheelRoutes = new Elysia({ prefix: "/wheel" })
  .get("/status", () => ({ ok: true }))
  .post("/spin", () => ({ ok: true }));
""")
        _write(os.path.join(self.test_dir, "frontend", "src", "wheel.ts"), """import { request } from './client'

export async function spinWheel() {
    return request<SpinWheelResponse>('/wheel/spin')
}
""")
        g = self._scan()
        eps = {(e[0], e[2]) for e in g.file_endpoints.get("backend/wheel.ts", [])}
        self.assertIn(("/wheel/spin", "POST"), eps)
        self.assertIn(("/wheel/status", "GET"), eps)
        links = [
            lk for lk in g.api_links
            if lk["source"]["file"] == "frontend/src/wheel.ts" and lk["target"]["route"] == "/wheel/spin"
        ]
        self.assertTrue(links, g.api_links)

    def test_fastapi_router_prefix_is_applied(self):
        _write(os.path.join(self.test_dir, "backend", "orders.py"), """from fastapi import APIRouter

router = APIRouter(prefix="/api/v2")

@router.get("/orders")
def list_orders():
    return []
""")
        _write(os.path.join(self.test_dir, "frontend", "src", "orders.ts"),
               "export const load = () => fetch('/api/v2/orders');\n")
        g = self._scan()
        routes = {e[0] for e in g.file_endpoints.get("backend/orders.py", [])}
        self.assertIn("/api/v2/orders", routes)
        self.assertTrue(
            [lk for lk in g.api_links if lk["target"]["route"] == "/api/v2/orders"],
            g.api_links,
        )

    def test_mount_site_prefix_composes_across_files(self):
        _write(os.path.join(self.test_dir, "backend", "users.ts"), """import { Router } from 'express'

export const usersRouter = Router()

usersRouter.get('/users', (req, res) => res.json([]))
""")
        _write(os.path.join(self.test_dir, "backend", "app.ts"), """import express from 'express'
import { usersRouter } from './users'

const app = express()
app.use('/v1', usersRouter)
""")
        _write(os.path.join(self.test_dir, "frontend", "src", "users.ts"),
               "export const load = () => fetch('/v1/users');\n")
        g = self._scan()
        self.assertIn("/users", {e[0] for e in g.file_endpoints.get("backend/users.ts", [])})
        self.assertEqual(g.file_mounts.get("backend/app.ts"), [("/v1", "usersRouter", "./users")])
        links = [
            lk for lk in g.api_links
            if lk["source"]["file"] == "frontend/src/users.ts" and lk["target"]["route"] == "/v1/users"
        ]
        self.assertTrue(links, [(lk["source"], lk["target"]) for lk in g.api_links])

    def test_prefix_is_not_applied_twice(self):
        _write(os.path.join(self.test_dir, "backend", "dup.ts"), """import { Elysia } from "elysia";

export const routes = new Elysia({ prefix: "/wheel" })
  .post("/wheel/spin", () => ({ ok: true }));
""")
        g = self._scan()
        routes = [e[0] for e in g.file_endpoints.get("backend/dup.ts", [])]
        self.assertEqual(routes, ["/wheel/spin"])

    def test_mounts_and_links_survive_the_disk_cache(self):
        _write(os.path.join(self.test_dir, "backend", "users.ts"), """import { Router } from 'express'

export const usersRouter = Router()

usersRouter.get('/users', (req, res) => res.json([]))
""")
        _write(os.path.join(self.test_dir, "backend", "app.ts"), """import express from 'express'
import { usersRouter } from './users'

const app = express()
app.use('/v1', usersRouter)
""")
        _write(os.path.join(self.test_dir, "frontend", "src", "users.ts"),
               "export const load = () => fetch('/v1/users');\n")
        data_dir = tempfile.mkdtemp(prefix="tokensafe_graph_cache_")
        self.addCleanup(shutil.rmtree, data_dir, ignore_errors=True)
        old_data = os.environ.get("TOKENSAFE_DATA_DIR")
        os.environ["TOKENSAFE_DATA_DIR"] = data_dir
        try:
            first = CodeGraph(self.test_dir, use_disk_cache=True)
            first.scan()
            second = CodeGraph(self.test_dir, use_disk_cache=True)
        finally:
            if old_data is None:
                os.environ.pop("TOKENSAFE_DATA_DIR", None)
            else:
                os.environ["TOKENSAFE_DATA_DIR"] = old_data
        self.assertEqual(second.file_mounts.get("backend/app.ts"), [("/v1", "usersRouter", "./users")])
        self.assertTrue(
            [lk for lk in second.api_links if lk["target"]["route"] == "/v1/users"],
            second.api_links,
        )


if __name__ == "__main__":
    unittest.main()
