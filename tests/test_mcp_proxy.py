import os
import json
import shutil
import tempfile
import unittest

from src.mcp_proxy import fold_tool_text, compact_mcp_response
from src.mcp_config_manager import MCPConfigManager
from src.stash import retrieve_block


class TestMCPProxy(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="tokensafe_mcp_proxy_test_")
        self.old_data_dir = os.environ.get("TOKENSAFE_DATA_DIR")
        os.environ["TOKENSAFE_DATA_DIR"] = self.test_dir

    def tearDown(self):
        if self.old_data_dir is not None:
            os.environ["TOKENSAFE_DATA_DIR"] = self.old_data_dir
        else:
            os.environ.pop("TOKENSAFE_DATA_DIR", None)
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_fold_tool_text_short(self):
        short = "This is a short response."
        folded, was_folded = fold_tool_text(short)
        self.assertFalse(was_folded)
        self.assertEqual(folded, short)

    def test_fold_tool_text_long(self):
        long_text = "\n".join([f"Line {i}: some detailed logs or api response payload content" for i in range(100)])
        self.assertGreater(len(long_text), 1500)
        folded, was_folded = fold_tool_text(long_text, tool_name="github_search")
        self.assertTrue(was_folded)
        self.assertIn("folded by TokenSafe MCP Proxy", folded)
        self.assertIn("[TokenSafe folded: #", folded)

        # Retrieve stashed content
        restored = retrieve_block(folded)
        self.assertEqual(restored, long_text)

    def test_compact_mcp_response(self):
        long_text = "\n".join([f"Item {i}: data record with keys and metadata values" for i in range(80)])
        req_resp = {
            "jsonrpc": "2.0",
            "id": 42,
            "result": {
                "content": [
                    {"type": "text", "text": long_text}
                ]
            }
        }
        compacted = compact_mcp_response(req_resp)
        compacted_text = compacted["result"]["content"][0]["text"]
        self.assertIn("folded by TokenSafe MCP Proxy", compacted_text)

    def test_mcp_config_manager_wrap_unwrap(self):
        cfg_file = os.path.join(self.test_dir, "test_mcp.json")
        initial = {
            "mcpServers": {
                "github": {
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-github"]
                }
            }
        }
        with open(cfg_file, "w", encoding="utf-8") as f:
            json.dump(initial, f)

        # Wrap
        ok = MCPConfigManager.wrap_server(cfg_file, "github")
        self.assertTrue(ok)

        with open(cfg_file, "r", encoding="utf-8") as f:
            wrapped = json.load(f)

        s_def = wrapped["mcpServers"]["github"]
        self.assertIn("_tokensafe_orig", s_def)
        self.assertEqual(s_def["_tokensafe_orig"]["command"], "npx")
        self.assertIn("mcp-proxy", " ".join(s_def["args"]))

        # Unwrap
        ok_unwrap = MCPConfigManager.unwrap_server(cfg_file, "github")
        self.assertTrue(ok_unwrap)

        with open(cfg_file, "r", encoding="utf-8") as f:
            unwrapped = json.load(f)

        s_unwrapped = unwrapped["mcpServers"]["github"]
        self.assertNotIn("_tokensafe_orig", s_unwrapped)
        self.assertEqual(s_unwrapped["command"], "npx")
        self.assertEqual(s_unwrapped["args"], ["-y", "@modelcontextprotocol/server-github"])


if __name__ == "__main__":
    unittest.main()
