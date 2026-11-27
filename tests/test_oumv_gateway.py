"""
Unit tests for OUMVGateway inside TokenSafe.
"""

import unittest
import json
import socket
import tempfile
import os
import shutil
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch, MagicMock

from src.oumv_gateway import OUMVGateway, OUMVRpcError


class TestOUMVGateway(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        OUMVGateway._workspace_states.clear()
        OUMVGateway._port_liveness.clear()

    def tearDown(self):
        OUMVGateway._port_liveness.clear()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _prime_state(self, ws=None, fe=None, be=None, fresh=True):
        """Seeds the per-workspace gateway state (fresh scan time skips live probing)."""
        st = OUMVGateway._state_for(ws or self.test_dir)
        if fresh:
            st["last_scan"] = 9999999999.0
        st["fe"] = fe
        st["be"] = be
        return st

    def _listener_port(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(8)
        self.addCleanup(srv.close)
        return srv.getsockname()[1]

    @patch.object(OUMVGateway, "_probe_candidates", return_value=None)
    def test_status_disconnected_default(self, _mock_probe):
        status = OUMVGateway.status(workspace_dir=self.test_dir)
        self.assertIn("frontend", status)
        self.assertIn("backend", status)
        self.assertEqual(status["frontend"]["status"], "DISCONNECTED")
        self.assertEqual(status["backend"]["status"], "DISCONNECTED")
        # No detected stack -> no adapter lifecycle entries (honest empty list)
        self.assertEqual(status.get("adapters"), [])

    def test_status_reports_adapter_lifecycle_when_stack_detected(self):
        package_json = {"dependencies": {"react": "^18.2.0"}, "devDependencies": {"vite": "^5.0.0"}}
        with open(os.path.join(self.test_dir, "package.json"), "w", encoding="utf-8") as f:
            json.dump(package_json, f)
        with open(os.path.join(self.test_dir, "pyproject.toml"), "w", encoding="utf-8") as f:
            f.write("[project]\nname = 'svc'\ndependencies = ['fastapi']\n")

        with patch("src.oumv_gateway.get_active_workspace", return_value=self.test_dir):
            self._prime_state()
            status = OUMVGateway.status(workspace_dir=self.test_dir)

        adapters = status.get("adapters", [])
        self.assertEqual(len(adapters), 2)
        by_profile = {a["profile"]: a for a in adapters}
        # Detected statically, runtime offline -> HANDSHAKE / degraded
        fe_adapter = by_profile["frontend"]
        self.assertEqual(fe_adapter["lifecycle"]["state"], "HANDSHAKE")
        self.assertEqual(fe_adapter["lifecycle"]["health"]["status"], "degraded")
        self.assertEqual(fe_adapter["adapter"], "frontend/react")
        be_adapter = by_profile["backend"]
        self.assertEqual(be_adapter["lifecycle"]["state"], "HANDSHAKE")
        self.assertEqual(be_adapter["adapter"], "backend/fastapi")
        # Adapter Contract fields present
        self.assertTrue(fe_adapter["capabilities"])
        self.assertEqual(fe_adapter["security"]["workspaceBoundary"], True)

    def test_discovery_from_runtime_json(self):
        # Descriptor adoption now requires the advertised port to be listening.
        fe_port = self._listener_port()
        be_port = self._listener_port()
        oumv_dir = os.path.join(self.test_dir, ".oumv")
        os.makedirs(oumv_dir, exist_ok=True)
        runtime_file = os.path.join(oumv_dir, "runtime.json")

        with open(runtime_file, "w", encoding="utf-8") as f:
            json.dump({
                "frontend": {
                    "name": "tokensafe-ui",
                    "version": "1.0.0",
                    "url": f"http://127.0.0.1:{fe_port}/__oumv/rpc"
                },
                "backend": {
                    "name": "tokensafe-python",
                    "version": "0.1.0",
                    "url": f"http://127.0.0.1:{be_port}/__oumv/rpc"
                }
            }, f)

        status = OUMVGateway.status(workspace_dir=self.test_dir)
        self.assertEqual(status["frontend"]["name"], "tokensafe-ui")
        self.assertEqual(status["backend"]["name"], "tokensafe-python")
        self.assertEqual(status["backend"]["url"], f"http://127.0.0.1:{be_port}/__oumv/rpc")
        self.assertNotIn("staleRuntimes", status)

    def test_descriptor_with_closed_port_is_not_adopted(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        dead_port = srv.getsockname()[1]
        srv.close()

        oumv_dir = os.path.join(self.test_dir, ".oumv")
        os.makedirs(oumv_dir, exist_ok=True)
        with open(os.path.join(oumv_dir, "runtime.json"), "w", encoding="utf-8") as f:
            json.dump({"frontend": {"name": "dead", "url": f"http://127.0.0.1:{dead_port}/__oumv/rpc"}}, f)

        with patch.object(OUMVGateway, "_probe_candidates", return_value=None):
            status = OUMVGateway.status(workspace_dir=self.test_dir)
        self.assertEqual(status["frontend"]["status"], "DISCONNECTED")
        self.assertTrue(status.get("staleRuntimes"))

    @patch.object(OUMVGateway, "_rpc_call")
    def test_observe_fullstack_pulse(self, mock_rpc):
        with patch("src.oumv_gateway.get_active_workspace", return_value=self.test_dir):
            self._prime_state(fe={"url": "http://localhost:5173/__oumv/rpc"}, be={"url": "http://localhost:8000/__oumv/rpc"})
            mock_rpc.side_effect = [
                {"status": "HEALTHY", "route": "/"},
                {"status": "HEALTHY", "routesCount": 5}
            ]

            res = OUMVGateway.observe(level="L0", runtime="auto")
        self.assertEqual(res["mode"], "FULLSTACK_PULSE")
        self.assertEqual(res["frontend"]["status"], "HEALTHY")
        self.assertEqual(res["backend"]["routesCount"], 5)

    @patch.object(OUMVGateway, "_rpc_call")
    def test_verify_dispatch(self, mock_rpc):
        with patch("src.oumv_gateway.get_active_workspace", return_value=self.test_dir):
            self._prime_state(be={"url": "http://localhost:8000/__oumv/rpc"})
            mock_rpc.return_value = {
                "status": "PASS",
                "statusCode": 201,
                "rolledBack": True
            }

            res = OUMVGateway.verify(target="POST /api/products", payload={"title": "Widget"})
            self.assertEqual(res["status"], "PASS")
            self.assertEqual(res["statusCode"], 201)
            self.assertTrue(res["rolledBack"])
            sent = mock_rpc.call_args[0][2]
            self.assertNotIn("sandbox", sent)

            OUMVGateway.verify(
                target="POST /api/products",
                payload={"title": "Widget"},
                sandbox={"mode": "rollback"},
            )
        sent = mock_rpc.call_args[0][2]
        self.assertEqual(sent["sandbox"], {"mode": "rollback"})

    def test_mutate_out_of_the_box_direct_edit(self):
        with patch("src.oumv_gateway.get_active_workspace", return_value=self.test_dir):
            file_path = "src/hello.py"
            full_path = os.path.join(self.test_dir, file_path)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "w", encoding="utf-8") as f:
                f.write("def hello(): return 'old'")

            # Out-of-the-box edit without .tokensafe/oumv.json and with devserver offline
            res = OUMVGateway.mutate(path=file_path, replacement="def hello(): return 'new'")
            self.assertTrue(res["success"])
            self.assertEqual(res["mode"], "OUT_OF_THE_BOX_DIRECT_EDIT")
            with open(full_path, "r", encoding="utf-8") as f:
                self.assertEqual(f.read(), "def hello(): return 'new'")

    def test_mutate_blocked_only_on_explicit_opt_out(self):
        with patch("src.oumv_gateway.get_active_workspace", return_value=self.test_dir):
            cfg_dir = os.path.join(self.test_dir, ".tokensafe")
            os.makedirs(cfg_dir, exist_ok=True)
            with open(os.path.join(cfg_dir, "oumv.json"), "w", encoding="utf-8") as f:
                json.dump({"permissions": {"mutate": False}}, f)

            with self.assertRaises(RuntimeError) as cm:
                OUMVGateway.mutate(path="test.py", replacement="foo")
            self.assertIn("-32007", str(cm.exception))

    def test_mutate_syntax_guard_rejects_broken_python(self):
        with patch("src.oumv_gateway.get_active_workspace", return_value=self.test_dir):
            file_path = "src/broken.py"
            full_path = os.path.join(self.test_dir, file_path)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            original = "def broken(): return 'original'"
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(original)

            with self.assertRaises(RuntimeError) as cm:
                OUMVGateway.mutate(path=file_path, replacement="def broken(: return 'oops'")
            self.assertIn("-32002", str(cm.exception))
            # File must remain untouched when the syntax guard rejects the edit
            with open(full_path, "r", encoding="utf-8") as f:
                self.assertEqual(f.read(), original)

    def test_mutate_direct_edit_reports_syntax_check_and_backup(self):
        with patch("src.oumv_gateway.get_active_workspace", return_value=self.test_dir):
            file_path = "src/guarded.py"
            full_path = os.path.join(self.test_dir, file_path)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "w", encoding="utf-8") as f:
                f.write("def value(): return 1")

            res = OUMVGateway.mutate(path=file_path, replacement="def value(): return 2")
            self.assertTrue(res["success"])
            self.assertEqual(res["syntaxCheck"]["status"], "passed")
            self.assertEqual(res["syntaxCheck"]["tool"], "python-ast")
            self.assertTrue(res["backupPath"])
            self.assertIn(".tokensafe", res["backupPath"])
            self.assertTrue(res["rollbackHint"])
            # Backup contains the previous content
            with open(res["backupPath"], "r", encoding="utf-8") as f:
                self.assertEqual(f.read(), "def value(): return 1")

    def test_mutate_unknown_extension_reports_skipped_syntax_check(self):
        with patch("src.oumv_gateway.get_active_workspace", return_value=self.test_dir):
            file_path = "src/component.tsx"
            full_path = os.path.join(self.test_dir, file_path)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "w", encoding="utf-8") as f:
                f.write("export const A = () => <div/>")

            res = OUMVGateway.mutate(path=file_path, replacement="export const A = () => <span/>")
            self.assertTrue(res["success"])
            # Honest reporting: TSX has no checker, must not imply validation
            self.assertEqual(res["syntaxCheck"]["status"], "skipped")

    def _make_fullstack_fixture(self):
        backend_dir = os.path.join(self.test_dir, "backend")
        os.makedirs(backend_dir, exist_ok=True)
        with open(os.path.join(backend_dir, "orders.py"), "w", encoding="utf-8") as f:
            f.write("from fastapi import APIRouter\n\nrouter = APIRouter()\n\n"
                    "@router.post('/api/orders')\ndef create_order(payload: dict):\n    return {'ok': True}\n")
        frontend_dir = os.path.join(self.test_dir, "frontend")
        os.makedirs(frontend_dir, exist_ok=True)
        with open(os.path.join(frontend_dir, "api.ts"), "w", encoding="utf-8") as f:
            f.write("export async function submitOrder(order: any) {\n"
                    "    return axios.post('/api/orders', order);\n}\n")

    def test_understand_causality_includes_cross_stack_edges(self):
        self._make_fullstack_fixture()
        with patch("src.oumv_gateway.get_active_workspace", return_value=self.test_dir):
            self._prime_state()

            res = OUMVGateway.understand(query="causality", target="/api/orders", runtime="auto")
            self.assertEqual(res["query"], "causality")
            self.assertIn("crossStack", res)
            self.assertGreaterEqual(res["crossStack"]["count"], 1)
            edge = res["crossStack"]["edges"][0]
            self.assertEqual(edge["source"]["type"], "frontend.action")
            self.assertEqual(edge["target"]["type"], "backend.endpoint")
            self.assertEqual(edge["evidence"]["provenance"], "static")
            # Static fallback present because both runtimes are offline
            self.assertIn("staticAnalysis", res)

    def test_understand_impact_returns_static_impact_and_callers(self):
        self._make_fullstack_fixture()
        with patch("src.oumv_gateway.get_active_workspace", return_value=self.test_dir):
            self._prime_state()

            res = OUMVGateway.understand(query="impact", target="/api/orders", runtime="auto")
            self.assertEqual(res["query"], "impact")
            self.assertEqual(res["mode"], "STATIC_IMPACT")
            self.assertGreaterEqual(res["crossStack"]["count"], 1)
            self.assertEqual(
                res["crossStack"]["frontendCallers"][0]["source"]["file"], "frontend/api.ts"
            )
            self.assertTrue(res["missingEvidence"])

    def test_understand_rejects_prose_query_instead_of_dumping_surface(self):
        self._make_fullstack_fixture()
        with patch("src.oumv_gateway.get_active_workspace", return_value=self.test_dir):
            self._prime_state()
            with patch.object(OUMVGateway, "get_endpoints", return_value=(None, None)):
                res = OUMVGateway.understand(
                    query="WheelModal spin button click handler chain", target=None, runtime="auto"
                )
        self.assertEqual(res["error"], "UNSUPPORTED_QUERY")
        self.assertEqual(res["supportedQueries"], ["impact", "causality", "flow"])
        self.assertNotIn("clientCalls", res)
        self.assertIn("smart_read_file", res["hint"])

    def test_understand_flow_returns_static_l2_flow(self):
        frontend_dir = os.path.join(self.test_dir, "frontend")
        os.makedirs(os.path.join(frontend_dir, "api"), exist_ok=True)
        with open(os.path.join(frontend_dir, "api", "wheel.ts"), "w", encoding="utf-8") as f:
            f.write("export async function spinRequest(p: any) {\n"
                    "    return request('/wheel/spin', p)\n}\n")
        with open(os.path.join(frontend_dir, "handler.ts"), "w", encoding="utf-8") as f:
            f.write("import { spinRequest } from './api/wheel'\n"
                    "export async function spinWheel(p: any) { return spinRequest(p) }\n")
        with patch("src.oumv_gateway.get_active_workspace", return_value=self.test_dir):
            self._prime_state()
            res = OUMVGateway.understand(query="flow", target="spinWheel", runtime="auto")
        self.assertEqual(res["mode"], "STATIC_FLOW")
        self.assertEqual(res["level"], "L2")
        self.assertIn("/wheel/spin", {r["route"] for r in res["clientCalls"]})

    def test_understand_causality_with_explicit_runtime_keeps_static_backbone(self):
        self._make_fullstack_fixture()
        with patch("src.oumv_gateway.get_active_workspace", return_value=self.test_dir):
            self._prime_state()
            with patch.object(OUMVGateway, "get_endpoints", return_value=(None, None)):
                res = OUMVGateway.understand(query="causality", target="/api/orders", runtime="frontend")
        self.assertEqual(res["query"], "causality")
        self.assertIn("staticAnalysis", res)
        self.assertGreaterEqual(res["crossStack"]["count"], 1)

    @patch.object(OUMVGateway, "_rpc_call", side_effect=OUMVRpcError("JSON-RPC error (-32002): SYNTAX_ERROR"))
    def test_mutate_propagates_live_rpc_error_without_offline_bypass(self, _mock_rpc):
        with patch("src.oumv_gateway.get_active_workspace", return_value=self.test_dir):
            self._prime_state(fe={"url": "http://localhost:5173/__oumv/rpc"})
            file_path = "src/App.tsx"
            full_path = os.path.join(self.test_dir, file_path)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "w", encoding="utf-8") as f:
                f.write("export const App = () => <div/>;")

            with self.assertRaises(OUMVRpcError):
                OUMVGateway.mutate(path=file_path, replacement="broken(", runtime="frontend")
            with open(full_path, "r", encoding="utf-8") as f:
                self.assertEqual(f.read(), "export const App = () => <div/>;")

    def test_mutate_falls_back_when_runtime_lacks_apply_edit(self):
        """A bridge that answers HTTP 400 "Unknown method" cannot mediate the edit.

        The gateway must convert that into a protocol error (not crash on OSError)
        and apply the same guarded offline edit, labelling the fallback honestly.
        """
        class UnknownMethodHandler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - http.server API
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                body = b'{"error":"Unknown method: oumv/applyEdit"}'
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):  # silence test output
                pass

        server = HTTPServer(("127.0.0.1", 0), UnknownMethodHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch("src.oumv_gateway.get_active_workspace", return_value=self.test_dir):
                self._prime_state(fe={"url": f"http://127.0.0.1:{server.server_port}/__oumv/rpc"})

                file_path = "src/App.py"
                full_path = os.path.join(self.test_dir, file_path)
                os.makedirs(os.path.dirname(full_path), exist_ok=True)
                with open(full_path, "w", encoding="utf-8") as f:
                    f.write("value = 1\n")

                res = OUMVGateway.mutate(path=file_path, replacement="value = 2\n", runtime="frontend")
                self.assertEqual(res["mode"], "RUNTIME_LACKS_APPLYEDIT_OFFLINE_FALLBACK")
                self.assertIn("Unknown method", res["runtimeFallbackReason"])
                self.assertEqual(res["syntaxCheck"]["status"], "passed")
                with open(full_path, "r", encoding="utf-8") as f:
                    self.assertEqual(f.read(), "value = 2\n")
        finally:
            server.shutdown()
            server.server_close()

    def test_rpc_call_surfaces_http_error_as_rpc_error(self):
        """HTTP 4xx from a bridge becomes OUMVRpcError, never an OSError."""
        class BadRequestHandler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - http.server API
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                body = b'{"jsonrpc":"2.0","id":1,"error":{"code":-32001,"message":"CONFLICT_DETECTED"}}'
                self.send_response(409)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), BadRequestHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/__oumv/rpc"
            with self.assertRaises(OUMVRpcError) as ctx:
                OUMVGateway._rpc_call(url, "oumv/applyEdit", {"path": "a.py"})
            self.assertIn("-32001", str(ctx.exception))
            self.assertIn("CONFLICT_DETECTED", str(ctx.exception))
        finally:
            server.shutdown()
            server.server_close()

    def test_global_runtime_descriptor_zero_footprint_discovery(self):
        tmp_home = tempfile.mkdtemp(prefix="tokensafe_home_")
        try:
            with patch.dict(os.environ, {"TOKENSAFE_HOME": tmp_home}):
                global_rt = OUMVGateway.global_runtime_file(self.test_dir)
                os.makedirs(os.path.dirname(global_rt), exist_ok=True)
                with open(global_rt, "w", encoding="utf-8") as f:
                    json.dump({
                        "frontend": {
                            "name": "react",
                            "version": "19.0.0",
                            "url": "http://localhost:5173/__oumv/rpc",
                            "sessionToken": "zf-token",
                        }
                    }, f)

                OUMVGateway._workspace_states.clear()
                st = OUMVGateway._check_workspace_runtime_files(self.test_dir)
                self.assertIsNotNone(st["fe"])
                self.assertEqual(st["fe"]["url"], "http://localhost:5173/__oumv/rpc")
                self.assertEqual(st["fe"]["sessionToken"], "zf-token")
                self.assertFalse(os.path.exists(os.path.join(self.test_dir, ".tokensafe")))
        finally:
            shutil.rmtree(tmp_home, ignore_errors=True)

    def test_resolve_zero_footprint_vite_command(self):
        from src.retrofit import resolve_zero_footprint_vite_command
        cmd, _ = resolve_zero_footprint_vite_command(self.test_dir, port=5179)
        self.assertIn("dev", cmd)
        self.assertIn(os.path.abspath(self.test_dir), cmd)
        self.assertIn("--port=5179", cmd)

    def test_endpoints_are_isolated_per_workspace_root(self):
        """A live runtime discovered for root A must never satisfy root B.

        Multi-root sessions answer several repos from one process: the gateway
        cache is keyed by canonical workspace root, so root B's status (and any
        RPC routed through it) cannot see root A's endpoints.
        """
        ws_a = tempfile.mkdtemp(prefix="oumv_ws_a_")
        ws_b = tempfile.mkdtemp(prefix="oumv_ws_b_")
        self.addCleanup(shutil.rmtree, ws_a, ignore_errors=True)
        self.addCleanup(shutil.rmtree, ws_b, ignore_errors=True)

        fe_port = self._listener_port()
        # Root A advertises a live frontend via its workspace descriptor
        a_dir = os.path.join(ws_a, ".tokensafe")
        os.makedirs(a_dir, exist_ok=True)
        with open(os.path.join(a_dir, "runtime.json"), "w", encoding="utf-8") as f:
            json.dump({"frontend": {"name": "a-ui", "url": f"http://127.0.0.1:{fe_port}/__oumv/rpc"}}, f)

        with patch.object(OUMVGateway, "_probe_candidates", return_value=None):
            fe_a, be_a = OUMVGateway.get_endpoints(workspace_dir=ws_a)
            self.assertIsNotNone(fe_a, "root A must adopt its own descriptor")
            fe_b, be_b = OUMVGateway.get_endpoints(workspace_dir=ws_b)
            self.assertIsNone(fe_b, "root B must not inherit root A's frontend")
            self.assertIsNone(be_b)

        # The same isolation holds for the status report
        with patch.object(OUMVGateway, "_probe_candidates", return_value=None):
            status_b = OUMVGateway.status(workspace_dir=ws_b)
            self.assertNotEqual(status_b["frontend"].get("name", ""), "a-ui")
            self.assertEqual(status_b["workspace"], ws_b)

    def test_status_scan_never_resets_another_workspaces_state(self):
        """Scanning one root must not drop a concurrent root's discovered endpoints."""
        ws_a = tempfile.mkdtemp(prefix="oumv_scan_a_")
        ws_b = tempfile.mkdtemp(prefix="oumv_scan_b_")
        self.addCleanup(shutil.rmtree, ws_a, ignore_errors=True)
        self.addCleanup(shutil.rmtree, ws_b, ignore_errors=True)

        with patch.object(OUMVGateway, "_probe_candidates", return_value=None):
            self._prime_state(ws_a, fe={"url": "http://localhost:5173/__oumv/rpc"}, fresh=False)
            OUMVGateway.scan(workspace_dir=ws_b)
            st_a = OUMVGateway._state_for(ws_a)
            self.assertEqual(st_a["fe"]["url"], "http://localhost:5173/__oumv/rpc")

    def test_ghost_transient_descriptor_is_not_adopted(self):
        """A global descriptor of a deleted temp workspace is a ghost: never adopted.

        Smoke tests and benchmarks legitimately develop in temp dirs, so the rule is
        existence, not transience: the descriptor is trusted only while the recorded
        root is still on disk. Even a listening port does not resurrect the ghost.
        """
        port = self._listener_port()
        tmp_home = tempfile.mkdtemp(prefix="tokensafe_home_")
        self.addCleanup(shutil.rmtree, tmp_home, ignore_errors=True)
        ghost_ws = tempfile.mkdtemp(prefix="react-oumv-smoke-")
        ghost_real = os.path.realpath(ghost_ws)
        try:
            with patch.dict(os.environ, {"TOKENSAFE_HOME": tmp_home}):
                rt = OUMVGateway.global_runtime_file(ghost_ws)
                os.makedirs(os.path.dirname(rt), exist_ok=True)
                with open(rt, "w", encoding="utf-8") as f:
                    json.dump({
                        "frontend": {
                            "name": "ghost-ui",
                            "url": f"http://127.0.0.1:{port}/__oumv/rpc",
                            "workspaceRoot": ghost_real,
                        }
                    }, f)
                shutil.rmtree(ghost_ws, ignore_errors=True)  # workspace dies, descriptor stays

                st = OUMVGateway._state_for(ghost_ws)
                OUMVGateway._check_workspace_runtime_files(ghost_ws, st)
                self.assertIsNone(st["fe"], "ghost descriptor must not be adopted")
        finally:
            shutil.rmtree(ghost_ws, ignore_errors=True)

    def test_key_mismatched_global_descriptor_is_not_adopted(self):
        """An entry that describes another root must not answer for this one."""
        port = self._listener_port()
        tmp_home = tempfile.mkdtemp(prefix="tokensafe_home_")
        self.addCleanup(shutil.rmtree, tmp_home, ignore_errors=True)
        other_ws = tempfile.mkdtemp(prefix="oumv_other_")
        self.addCleanup(shutil.rmtree, other_ws, ignore_errors=True)
        try:
            with patch.dict(os.environ, {"TOKENSAFE_HOME": tmp_home}):
                # Descriptor for ws "other" filed under this test dir's registry key
                rt = OUMVGateway.global_runtime_file(self.test_dir)
                os.makedirs(os.path.dirname(rt), exist_ok=True)
                with open(rt, "w", encoding="utf-8") as f:
                    json.dump({
                        "frontend": {
                            "name": "other-ui",
                            "url": f"http://127.0.0.1:{port}/__oumv/rpc",
                            "workspaceRoot": os.path.realpath(other_ws),
                        }
                    }, f)
                st = OUMVGateway._state_for(self.test_dir)
                OUMVGateway._check_workspace_runtime_files(self.test_dir, st)
                self.assertIsNone(st["fe"], "foreign-root descriptor must not be adopted")
        finally:
            pass


class TestOumvIndexAndPreview(unittest.TestCase):
    """oumv index (repo-wide symbol search) and oumv preview (dry-run edit)."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="oumv_index_")
        OUMVGateway._workspace_states.clear()
        OUMVGateway._port_liveness.clear()

    def tearDown(self):
        OUMVGateway._port_liveness.clear()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _prime_state(self, ws=None, fe=None, be=None, fresh=True):
        st = OUMVGateway._state_for(ws or self.test_dir)
        if fresh:
            st["last_scan"] = 9999999999.0
        st["fe"] = fe
        st["be"] = be
        return st

    def _write(self, rel, content):
        full = os.path.join(self.test_dir, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)

    def test_index_ranks_exact_above_substring_with_refs(self):
        self._write("orders.py",
                    "def create_order(payload):\n    return {'ok': True}\n\n"
                    "class OrderService:\n    def create_order(self, p):\n        return create_order(p)\n")
        self._write("orders_client.py",
                    "from orders import create_order\n\n"
                    "def submit():\n    return create_order({})\n")
        res = OUMVGateway.index(query="create_order", limit=10, workspace_dir=self.test_dir)
        self.assertEqual(res["provenance"], "static-graph")
        names = [m["name"] for m in res["matches"]]
        self.assertIn("create_order", names)
        top = res["matches"][0]
        self.assertEqual(top["match"], "exact")
        self.assertGreaterEqual(top["refs"], 1, "two call sites -> reverse-edge count >= 1")
        self.assertEqual(top["file"], "orders.py")
        self.assertGreater(top["lines"][0], 0)

    def test_index_kinds_filter_and_honest_empty(self):
        self._write("m.py", "class Repo:\n    pass\n\n\ndef helper():\n    return Repo\n")
        res = OUMVGateway.index(query="Repo", kinds=["class"], workspace_dir=self.test_dir)
        self.assertTrue(res["matches"])
        self.assertEqual({m["kind"] for m in res["matches"]}, {"class"})
        res2 = OUMVGateway.index(query="definitely_not_present_zz", workspace_dir=self.test_dir)
        self.assertEqual(res2["matches"], [])
        self.assertIn("provenance", res2)

    def test_index_requires_query(self):
        res = OUMVGateway.index(query="", workspace_dir=self.test_dir)
        self.assertEqual(res.get("error"), "VALIDATION_FAILED")

    def test_preview_offline_full_file_diff_without_writing(self):
        self._write("app.py", "def a():\n    return 1\n")
        res = OUMVGateway.preview(path="app.py", replacement="def a():\n    return 2\n", workspace_dir=self.test_dir)
        self.assertEqual(res["mode"], "OFFLINE_FULL_FILE_PREVIEW")
        self.assertFalse(res["unchanged"])
        self.assertIn("-    return 1", res["diff"])
        self.assertIn("+    return 2", res["diff"])
        self.assertEqual(res["syntaxCheck"]["status"], "passed")
        with open(os.path.join(self.test_dir, "app.py"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "def a():\n    return 1\n", "preview must never write")

    def test_preview_refuses_symbol_level_without_runtime(self):
        self._write("app.py", "def a():\n    return 1\n")
        res = OUMVGateway.preview(path="app.py", symbol="a", replacement="def a():\n    return 9\n", workspace_dir=self.test_dir)
        self.assertEqual(res["error"], "UNSUPPORTED_OFFLINE")
        self.assertNotIn("diff", res)
        with open(os.path.join(self.test_dir, "app.py"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "def a():\n    return 1\n")

    def test_preview_blocks_path_traversal(self):
        res = OUMVGateway.preview(path="../outside.py", replacement="x", workspace_dir=self.test_dir)
        self.assertEqual(res["error"], "SECURITY_VIOLATION")

    @patch.object(OUMVGateway, "_rpc_call")
    def test_preview_delegates_to_live_runtime(self, mock_rpc):
        mock_rpc.return_value = {"status": "PREVIEW", "path": "a.py", "diff": "-1\n+2\n"}
        self._prime_state(fe={"url": "http://localhost:5173/__oumv/rpc"})
        res = OUMVGateway.preview(path="a.py", replacement="2", runtime="frontend", workspace_dir=self.test_dir)
        self.assertEqual(res["mode"], "LIVE_RUNTIME_PREVIEW")
        method = mock_rpc.call_args[0][1]
        self.assertEqual(method, "oumv/previewEdit")


class TestOumvWorkspaceResolution(unittest.TestCase):
    """Root-less oumv calls resolve per server, not per machine.

    The active-workspace marker is rewritten by every concurrent session's
    hooks; a multi-root host's MCP server must default to its own spawn cwd
    instead of another session's last-touched repo."""

    def _repo_root(self):
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def test_default_prefers_cwd_over_machine_marker(self):
        from src import mcp_server
        proj = tempfile.mkdtemp(prefix="oumv_proj_")
        # mkdtemp is transient; promote it to a plausible root by patching the filter
        self.addCleanup(shutil.rmtree, proj, ignore_errors=True)
        old = os.getcwd()
        os.chdir(proj)
        try:
            with patch("src.mcp_server.current_root", return_value=None), \
                 patch("src.mcp_server.get_active_workspace", return_value=self._repo_root()), \
                 patch("src.paths.is_transient_dir", return_value=False):
                self.assertEqual(
                    os.path.realpath(mcp_server._oumv_default_workspace()),
                    os.path.realpath(proj),
                )
        finally:
            os.chdir(old)

    def test_default_falls_back_to_marker_when_cwd_is_scratch(self):
        from src import mcp_server
        old = os.getcwd()
        os.chdir(tempfile.gettempdir())
        try:
            with patch("src.mcp_server.current_root", return_value=None), \
                 patch("src.mcp_server.get_active_workspace", return_value=self._repo_root()):
                self.assertEqual(mcp_server._oumv_default_workspace(), self._repo_root())
        finally:
            os.chdir(old)

    def test_session_scope_wins(self):
        from src import mcp_server
        proj = tempfile.mkdtemp(prefix="oumv_session_root_")
        self.addCleanup(shutil.rmtree, proj, ignore_errors=True)
        with patch("src.mcp_server.current_root", return_value=proj), \
             patch("src.mcp_server.get_active_workspace", return_value=self._repo_root()):
            self.assertEqual(mcp_server._oumv_default_workspace(), proj)


if __name__ == "__main__":
    unittest.main()