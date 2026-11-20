"""install_project idempotency: an existing tokensafe entry in a project's
.cursor/mcp.json must be left verbatim.

Regression: `tokensafe install --all` from the tokensafe repo root rewrote
the repo's own TRACKED .cursor/mcp.json with the machine-specific PYTHON_BIN
venv path and dropped a .backup file on every run (shipped as dirt in the
working tree before v4.3.1). Missing entries are still injected fresh."""
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys_path = PROJECT_ROOT

import sys
if sys_path not in sys.path:
    sys.path.insert(0, sys_path)


class TestProjectCursorMcpInjection(unittest.TestCase):
    def setUp(self):
        self._proj = tempfile.mkdtemp(prefix="tokensafe_projinj_")
        self.addCleanup(shutil.rmtree, self._proj, ignore_errors=True)

    def _mcp_path(self) -> str:
        return os.path.join(self._proj, ".cursor", "mcp.json")

    def _write_mcp(self, command: str) -> None:
        os.makedirs(os.path.dirname(self._mcp_path()), exist_ok=True)
        with open(self._mcp_path(), "w", encoding="utf-8") as f:
            json.dump({"mcpServers": {"tokensafe": {"command": command, "args": ["x"]}}}, f)

    def _run(self):
        from src.installer import AgentInstaller
        return AgentInstaller.install_project(self._proj)

    def test_existing_entry_left_verbatim(self):
        self._write_mcp("generic-python")
        before = open(self._mcp_path(), encoding="utf-8").read()
        self._run()
        self.assertEqual(open(self._mcp_path(), encoding="utf-8").read(), before)
        self.assertFalse(os.path.exists(self._mcp_path() + ".backup"),
                         "no side-effect backup when nothing is written")

    def test_machine_path_churn_case_untouched(self):
        # The literal incident: a committed entry with a non-venv command.
        self._write_mcp("/Library/Frameworks/Python.framework/Versions/3.13/bin/python3")
        before = open(self._mcp_path(), encoding="utf-8").read()
        self._run()
        self.assertIn("Library/Frameworks", open(self._mcp_path(), encoding="utf-8").read())
        self.assertEqual(open(self._mcp_path(), encoding="utf-8").read(), before)

    def test_missing_entry_injected(self):
        os.makedirs(os.path.dirname(self._mcp_path()), exist_ok=True)
        with open(self._mcp_path(), "w", encoding="utf-8") as f:
            json.dump({"mcpServers": {}}, f)
        self._run()
        data = json.load(open(self._mcp_path(), encoding="utf-8"))
        self.assertIn("tokensafe", data["mcpServers"])
        self.assertTrue(data["mcpServers"]["tokensafe"].get("command"))
        self.assertTrue(data["mcpServers"]["tokensafe"].get("args"))

    def test_no_file_created_fresh(self):
        self._run()
        self.assertTrue(os.path.exists(self._mcp_path()))
        data = json.load(open(self._mcp_path(), encoding="utf-8"))
        self.assertIn("tokensafe", data["mcpServers"])


if __name__ == "__main__":
    unittest.main()
