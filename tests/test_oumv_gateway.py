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


if __name__ == "__main__":
    unittest.main()
