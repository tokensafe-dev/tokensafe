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
        OUMVGateway._cached_fe = None
        OUMVGateway._cached_be = None
        OUMVGateway._last_scan_time = 0.0
        OUMVGateway._port_liveness.clear()

    def tearDown(self):
        OUMVGateway._port_liveness.clear()
        shutil.rmtree(self.test_dir, ignore_errors=True)

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
            OUMVGateway._last_scan_time = 9999999999.0
            OUMVGateway._cached_fe = None
            OUMVGateway._cached_be = None
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
        OUMVGateway._last_scan_time = 9999999999.0
        OUMVGateway._cached_fe = {"url": "http://localhost:5173/__oumv/rpc"}
        OUMVGateway._cached_be = {"url": "http://localhost:8000/__oumv/rpc"}
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
        OUMVGateway._last_scan_time = 9999999999.0
        OUMVGateway._cached_be = {"url": "http://localhost:8000/__oumv/rpc"}
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
            OUMVGateway._last_scan_time = 9999999999.0  # skip live port probing
            OUMVGateway._cached_fe = None
            OUMVGateway._cached_be = None

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
            OUMVGateway._last_scan_time = 9999999999.0
            OUMVGateway._cached_fe = None
            OUMVGateway._cached_be = None

            res = OUMVGateway.understand(query="impact", target="/api/orders", runtime="auto")
            self.assertEqual(res["query"], "impact")
            self.assertEqual(res["mode"], "STATIC_IMPACT")
            self.assertGreaterEqual(res["crossStack"]["count"], 1)
            self.assertEqual(
                res["crossStack"]["frontendCallers"][0]["source"]["file"], "frontend/api.ts"
            )
            self.assertTrue(res["missingEvidence"])

    @patch.object(OUMVGateway, "_rpc_call", side_effect=OUMVRpcError("JSON-RPC error (-32002): SYNTAX_ERROR"))
    def test_mutate_propagates_live_rpc_error_without_offline_bypass(self, _mock_rpc):
        with patch("src.oumv_gateway.get_active_workspace", return_value=self.test_dir):
            OUMVGateway._last_scan_time = 9999999999.0
            OUMVGateway._cached_fe = {"url": "http://localhost:5173/__oumv/rpc"}
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
                OUMVGateway._last_scan_time = 9999999999.0
                OUMVGateway._cached_fe = {"url": f"http://127.0.0.1:{server.server_port}/__oumv/rpc"}

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

                OUMVGateway._cached_fe = None
                OUMVGateway._check_workspace_runtime_files(self.test_dir)
                self.assertIsNotNone(OUMVGateway._cached_fe)
                self.assertEqual(OUMVGateway._cached_fe["url"], "http://localhost:5173/__oumv/rpc")
                self.assertEqual(OUMVGateway._cached_fe["sessionToken"], "zf-token")
                self.assertFalse(os.path.exists(os.path.join(self.test_dir, ".tokensafe")))
        finally:
            shutil.rmtree(tmp_home, ignore_errors=True)

    def test_resolve_zero_footprint_vite_command(self):
        from src.retrofit import resolve_zero_footprint_vite_command
        cmd, _ = resolve_zero_footprint_vite_command(self.test_dir, port=5179)
        self.assertIn("dev", cmd)
        self.assertIn(os.path.abspath(self.test_dir), cmd)
        self.assertIn("--port=5179", cmd)


if __name__ == "__main__":
    unittest.main()
