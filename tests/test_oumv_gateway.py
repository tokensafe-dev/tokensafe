"""
Unit tests for OUMVGateway inside TokenSafe.
"""

import unittest
import json
import tempfile
import os
import shutil
from unittest.mock import patch, MagicMock

from src.oumv_gateway import OUMVGateway


class TestOUMVGateway(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        OUMVGateway._cached_fe = None
        OUMVGateway._cached_be = None
        OUMVGateway._last_scan_time = 0.0

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

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
        oumv_dir = os.path.join(self.test_dir, ".oumv")
        os.makedirs(oumv_dir, exist_ok=True)
        runtime_file = os.path.join(oumv_dir, "runtime.json")

        with open(runtime_file, "w", encoding="utf-8") as f:
            json.dump({
                "frontend": {
                    "name": "tokensafe-ui",
                    "version": "1.0.0",
                    "url": "http://localhost:5173/__oumv/rpc"
                },
                "backend": {
                    "name": "tokensafe-python",
                    "version": "0.1.0",
                    "url": "http://localhost:8000/__oumv/rpc"
                }
            }, f)

        status = OUMVGateway.status(workspace_dir=self.test_dir)
        self.assertEqual(status["frontend"]["name"], "tokensafe-ui")
        self.assertEqual(status["backend"]["name"], "tokensafe-python")
        self.assertEqual(status["backend"]["url"], "http://localhost:8000/__oumv/rpc")

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


if __name__ == "__main__":
    unittest.main()
