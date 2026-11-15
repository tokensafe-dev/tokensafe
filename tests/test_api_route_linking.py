"""
Unit tests for Cross-Boundary Semantic API Route Linking in TokenSafe CodeGraph.
Verifies matching of frontend client calls (fetch, axios, useSWR, ky) with
backend route endpoints (FastAPI, Express, Next.js, Spring Boot, Go Gin, ASP.NET, Laravel),
generating cross-file and cross-symbol directed dependency edges.
"""

import os
import shutil
import tempfile
import unittest

from src.graph_engine import CodeGraph


class TestApiRouteLinking(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="tokensafe_routes_test_")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_fastapi_python_to_react_fetch_linking(self):
        # Backend: FastAPI endpoint
        backend_dir = os.path.join(self.test_dir, "backend")
        os.makedirs(backend_dir, exist_ok=True)
        py_file = os.path.join(backend_dir, "users.py")
        with open(py_file, "w") as f:
            f.write("""from fastapi import APIRouter

router = APIRouter()

@router.get("/api/v1/users/{id}")
def get_user_profile(id: int):
    return {"id": id, "name": "Alice"}
""")

        # Frontend: React component calling fetch
        frontend_dir = os.path.join(self.test_dir, "frontend", "src")
        os.makedirs(frontend_dir, exist_ok=True)
        ts_file = os.path.join(frontend_dir, "UserProfile.tsx")
        with open(ts_file, "w") as f:
            f.write("""export async function fetchUserProfile(userId: string) {
    const res = await fetch(`/api/v1/users/${userId}`);
    return res.json();
}
""")

        graph = CodeGraph(self.test_dir, use_disk_cache=False)
        graph.scan()

        # 1. Check file-level cross-boundary edge
        caller_file = "frontend/src/UserProfile.tsx"
        target_file = "backend/users.py"
        self.assertIn(f"file:{target_file}", graph.edges.get(f"file:{caller_file}", set()))

        # 2. Check symbol-level edge: fetchUserProfile -> get_user_profile
        caller_sym = f"{caller_file}::fetchUserProfile"
        target_sym = f"{target_file}::get_user_profile"
        self.assertIn(target_sym, graph.edges.get(caller_sym, set()))

        # 3. Check bidirectional neighborhood:
        # Looking at backend endpoint should reveal frontend caller in 1-hop
        backend_nbrs = graph.get_neighborhood("get_user_profile")
        self.assertIn(caller_sym, backend_nbrs["one_hop"])

        # Looking at frontend caller should reveal backend endpoint in 1-hop
        frontend_nbrs = graph.get_neighborhood("fetchUserProfile")
        self.assertIn(target_sym, frontend_nbrs["one_hop"])

    def test_express_ts_to_axios_linking(self):
        # Backend: Express router
        backend_dir = os.path.join(self.test_dir, "server")
        os.makedirs(backend_dir, exist_ok=True)
        srv_file = os.path.join(backend_dir, "checkout.ts")
        with open(srv_file, "w") as f:
            f.write("""import express from 'express';
const router = express.Router();

router.post('/api/checkout/pay', async (req, res) => {
    res.json({ success: true });
});
""")

        # Frontend: Axios client
        client_dir = os.path.join(self.test_dir, "client")
        os.makedirs(client_dir, exist_ok=True)
        client_file = os.path.join(client_dir, "api.ts")
        with open(client_file, "w") as f:
            f.write("""import axios from 'axios';

export async function processPayment(cartData: any) {
    return axios.post('/api/checkout/pay', cartData);
}
""")

        graph = CodeGraph(self.test_dir, use_disk_cache=False)
        graph.scan()

        self.assertIn("file:server/checkout.ts", graph.edges.get("file:client/api.ts", set()))

    def test_nextjs_app_router_route_linking(self):
        # Next.js App Router API route file: app/api/auth/session/route.ts
        api_route_dir = os.path.join(self.test_dir, "app", "api", "auth", "session")
        os.makedirs(api_route_dir, exist_ok=True)
        route_file = os.path.join(api_route_dir, "route.ts")
        with open(route_file, "w") as f:
            f.write("""export async function GET(request: Request) {
    return Response.json({ user: 'auth-user' });
}
""")

        # Frontend hook using client.get
        hook_dir = os.path.join(self.test_dir, "components")
        os.makedirs(hook_dir, exist_ok=True)
        hook_file = os.path.join(hook_dir, "SessionBadge.tsx")
        with open(hook_file, "w") as f:
            f.write("""export function SessionBadge() {
    const session = client.get('/api/auth/session');
    return session;
}
""")

        graph = CodeGraph(self.test_dir, use_disk_cache=False)
        graph.scan()

        self.assertIn("file:app/api/auth/session/route.ts", graph.edges.get("file:components/SessionBadge.tsx", set()))

    def test_spring_boot_controller_prefix_linking(self):
        # Java Spring Boot controller with class-level @RequestMapping
        java_dir = os.path.join(self.test_dir, "src", "main", "java", "com", "app")
        os.makedirs(java_dir, exist_ok=True)
        ctrl_file = os.path.join(java_dir, "OrderController.java")
        with open(ctrl_file, "w") as f:
            f.write("""package com.app;

@RestController
@RequestMapping("/api/v1/orders")
public class OrderController {

    @GetMapping("/{orderId}")
    public Order getOrder(@PathVariable String orderId) {
        return new Order(orderId);
    }
}
""")

        # Frontend using useSWR
        web_dir = os.path.join(self.test_dir, "web")
        os.makedirs(web_dir, exist_ok=True)
        hook_file = os.path.join(web_dir, "useOrder.ts")
        with open(hook_file, "w") as f:
            f.write("""export function useOrder(orderId: string) {
    const { data } = useSWR(`/api/v1/orders/${orderId}`);
    return data;
}
""")

        graph = CodeGraph(self.test_dir, use_disk_cache=False)
        graph.scan()

        java_rel = "src/main/java/com/app/OrderController.java"
        self.assertIn(f"file:{java_rel}", graph.edges.get("file:web/useOrder.ts", set()))

    def test_go_gin_and_aspnet_and_laravel_endpoints(self):
        # 1. Go Gin
        go_file = os.path.join(self.test_dir, "main.go")
        with open(go_file, "w") as f:
            f.write("""package main

func SetupRoutes(r *gin.Engine) {
    r.GET("/api/v1/health", HealthHandler)
}
""")

        # 2. C# ASP.NET Core
        cs_file = os.path.join(self.test_dir, "ProductController.cs")
        with open(cs_file, "w") as f:
            f.write("""namespace Api;

public class ProductController {
    [HttpGet("/api/v1/products")]
    public IActionResult ListProducts() {
        return Ok();
    }
}
""")

        # 3. PHP Laravel
        php_file = os.path.join(self.test_dir, "routes.php")
        with open(php_file, "w") as f:
            f.write("""<?php
Route::post("/api/v1/tokens", [TokenController::class, "issue"]);
""")

        # Frontend calls
        client_file = os.path.join(self.test_dir, "client.ts")
        with open(client_file, "w") as f:
            f.write("""export async function checkHealth() {
    return ky.get("/api/v1/health");
}

export async function getProducts() {
    return axios.get("/api/v1/products");
}

export async function issueToken() {
    return fetch("/api/v1/tokens", { method: 'POST' });
}
""")

        graph = CodeGraph(self.test_dir, use_disk_cache=False)
        graph.scan()

        client_edges = graph.edges.get("file:client.ts", set())
        self.assertIn("file:main.go", client_edges)
        self.assertIn("file:ProductController.cs", client_edges)
        self.assertIn("file:routes.php", client_edges)

    def test_god_nodes_centrality_boost_from_api_links(self):
        # Backend user service
        be_file = os.path.join(self.test_dir, "users_service.py")
        with open(be_file, "w") as f:
            f.write("""from fastapi import APIRouter
router = APIRouter()

@router.get("/api/users")
def get_users():
    return []
""")

        # 3 independent frontend components all calling /api/users
        for comp in ["Header.tsx", "Sidebar.tsx", "UserList.tsx"]:
            with open(os.path.join(self.test_dir, comp), "w") as f:
                f.write(f"""export function {comp.split('.')[0]}() {{
    fetch("/api/users");
}}
""")

        graph = CodeGraph(self.test_dir, use_disk_cache=False)
        graph.scan()

        god_nodes = graph.get_god_nodes(top_n=3)
        top_node = god_nodes[0]
        self.assertEqual(top_node["file"], "users_service.py")
        # 3 frontend callers depend on users_service.py
        self.assertGreaterEqual(top_node["dependents"], 3)


if __name__ == "__main__":
    unittest.main()
