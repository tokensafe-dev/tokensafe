"""Spec §8.5 (revision v1.11): a live read-only runtime must not be edited around.

The engine used to treat `-32601` as "the runtime cannot mediate, do the edit
offline". That fallback is right for a dev server that died, and wrong for a
deployment that deliberately registers no mutating method: writing the file anyway
would restore the write path the deployment removed.
"""

import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from src.oumv_gateway import OUMVGateway, OUMVRpcError

URL = "http://127.0.0.1:7331/__oumv/rpc"
ABSENT_EDIT = OUMVRpcError("METHOD_NOT_FOUND: oumv/applyEdit", code=-32601)


def _rpc_returning(handshake_payload, edit_error=ABSENT_EDIT):
    def fake(endpoint_url, method, params=None, timeout=3.0):
        if method == "oumv/handshake":
            return handshake_payload
        raise edit_error

    return fake


def _target_url(**kwargs):
    return URL


class ReadOnlyRuntimeRefusalTest(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp(prefix="tokensafe_readonly_")
        self.addCleanup(shutil.rmtree, self.ws, ignore_errors=True)
        self.rel = "src/app.py"
        self.abs = os.path.join(self.ws, self.rel)
        os.makedirs(os.path.dirname(self.abs), exist_ok=True)
        with open(self.abs, "w", encoding="utf-8") as handle:
            handle.write("VALUE = 1\n")

    def _run_mutate(self, handshake_payload):
        with patch.object(OUMVGateway, "_resolve_client_url", _target_url), patch.object(
            OUMVGateway, "_rpc_call", _rpc_returning(handshake_payload)
        ):
            return OUMVGateway.mutate(
                path=self.rel,
                replacement="VALUE = 2\n",
                workspace_dir=self.ws,
            )

    def test_a_live_read_only_runtime_is_refused_not_written_offline(self):
        payload = {
            "protocolVersion": "1.6.0",
            "runtimeInfo": {"name": "tokensafe-python", "version": "0.10.0"},
            "capabilities": {"observe": {"levels": ["L0"]}, "readOnly": True},
        }

        with self.assertRaises(RuntimeError) as caught:
            self._run_mutate(payload)

        self.assertIn("-32007", str(caught.exception))
        self.assertIn("read-only", str(caught.exception))
        with open(self.abs, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "VALUE = 1\n", "the file must stay untouched")

    def test_a_live_runtime_without_apply_edit_is_also_refused(self):
        """Absence is authoritative even when the runtime does not say `readOnly`."""
        payload = {
            "protocolVersion": "1.6.0",
            "runtimeInfo": {"name": "partial-adapter", "version": "0.1.0"},
            "capabilities": {"observe": {"levels": ["L0"]}},
        }

        with self.assertRaises(RuntimeError) as caught:
            self._run_mutate(payload)

        self.assertIn("read-only", str(caught.exception))
        with open(self.abs, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "VALUE = 1\n")

    def test_an_unreachable_runtime_keeps_the_offline_fallback(self):
        """No runtime is live at all: the guarded offline edit still happens."""
        def no_runtime(**kwargs):
            raise RuntimeError("no OUMV runtime is live in this workspace")

        def never_called(endpoint_url, method, params=None, timeout=3.0):
            raise AssertionError("no RPC may be attempted when nothing is live")

        with patch.object(OUMVGateway, "_resolve_client_url", no_runtime), patch.object(
            OUMVGateway, "_rpc_call", never_called
        ):
            result = OUMVGateway.mutate(
                path=self.rel,
                replacement="VALUE = 2\n",
                workspace_dir=self.ws,
            )

        self.assertTrue(result.get("success"))
        self.assertEqual(result.get("mode"), "OUT_OF_THE_BOX_DIRECT_EDIT")
        with open(self.abs, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "VALUE = 2\n")

    def test_probe_surfaces_the_read_only_flag(self):
        payload = {
            "protocolVersion": "1.6.0",
            "runtimeInfo": {"name": "tokensafe-python", "version": "0.10.0"},
            "capabilities": {"observe": {"levels": ["L0"]}, "readOnly": True},
        }

        with patch.object(OUMVGateway, "_rpc_call", _rpc_returning(payload)):
            probed = OUMVGateway._probe_candidates([URL], "backend")

        self.assertIsNotNone(probed)
        self.assertTrue(probed["capabilities"]["readOnly"])

    def test_probe_reports_a_writable_runtime_as_not_read_only(self):
        payload = {
            "protocolVersion": "1.6.0",
            "runtimeInfo": {"name": "tokensafe-python", "version": "0.10.0"},
            "capabilities": {"observe": {"levels": ["L0"]}, "mutate": {"occ": True}},
        }

        with patch.object(OUMVGateway, "_rpc_call", _rpc_returning(payload)):
            probed = OUMVGateway._probe_candidates([URL], "backend")

        self.assertFalse(probed["capabilities"]["readOnly"])


if __name__ == "__main__":
    unittest.main()
