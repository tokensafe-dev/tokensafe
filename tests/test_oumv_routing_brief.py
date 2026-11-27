"""Tests for the per-workspace OUMV routing brief (hooks + MCP instructions)."""

import json
import os
import shutil
import tempfile
import unittest

from src.session_brief import build_workspace_brief


class TestOumvRoutingBrief(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tokensafe_brief_")
        # A .git entry makes is_project_root() treat the dir as a project.
        open(os.path.join(self.tmp, ".git"), "w").close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, rel, content):
        path = os.path.join(self.tmp, rel)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

    def test_react_brief_routes_through_tokensafe_dev(self):
        self._write("package.json", json.dumps({"dependencies": {"react": "^18.3.1", "vite": "^5.4.0"}}))
        brief = build_workspace_brief(self.tmp)
        self.assertIn("frontend react/react", brief)
        self.assertIn("tokensafe dev", brief)
        self.assertIn("tokensafe:oumv", brief)

    def test_fastapi_brief_routes_through_run_backend(self):
        self._write("requirements.txt", "fastapi\nuvicorn\n")
        brief = build_workspace_brief(self.tmp)
        self.assertIn("backend fastapi/fastapi", brief)
        self.assertIn("tokensafe run-backend", brief)

    def test_go_brief_mentions_reversible_inject(self):
        self._write("go.mod", "module example.com/app\n\ngo 1.21\n")
        self._write(
            "main.go",
            'package main\n\nimport "github.com/gin-gonic/gin"\n\nfunc main() {\n\tr := gin.Default()\n\t_ = r\n}\n',
        )
        brief = build_workspace_brief(self.tmp)
        self.assertIn("backend go", brief)
        self.assertIn("tokensafe inject", brief)

    def test_vue_brief_points_at_the_zero_footprint_dev_server(self):
        # Vue ships a certified adapter: the brief must route agents to it, not to grep.
        self._write("package.json", json.dumps({"dependencies": {"vue": "^3.5.0", "vite": "^6.0.0"}}))
        brief = build_workspace_brief(self.tmp)
        self.assertIn("tokensafe dev --framework vue", brief)
        self.assertIn("tokensafe:oumv", brief)

    def test_node_brief_explains_how_to_mount(self):
        self._write("package.json", json.dumps({"dependencies": {"express": "^4.19.0"}}))
        brief = build_workspace_brief(self.tmp)
        self.assertIn("backend node/express", brief)
        self.assertIn("tokensafe-backend-node", brief)

    def test_mutation_policy_is_reflected(self):
        self._write("package.json", json.dumps({"dependencies": {"react": "^18.3.1", "vite": "^5.4.0"}}))
        self._write(".tokensafe/oumv.json", json.dumps({"permissions": {"observe": True, "mutate": False}}))
        brief = build_workspace_brief(self.tmp)
        self.assertIn("disabled by .tokensafe/oumv.json", brief)

    def test_brief_is_bounded(self):
        self._write("package.json", json.dumps({"dependencies": {"react": "^18.3.1", "vite": "^5.4.0"}}))
        brief = build_workspace_brief(self.tmp, max_chars=120)
        self.assertLessEqual(len(brief), 120)

    def test_non_project_directory_yields_nothing(self):
        plain = tempfile.mkdtemp(prefix="tokensafe_plain_")
        try:
            self.assertEqual(build_workspace_brief(plain), "")
        finally:
            shutil.rmtree(plain, ignore_errors=True)

    def test_claude_session_start_injects_the_brief(self):
        self._write("package.json", json.dumps({"dependencies": {"react": "^18.3.1", "vite": "^5.4.0"}}))
        from hooks.claude_code_hook import handle

        out = handle("SessionStart", {"cwd": self.tmp, "session_id": "s-brief"}, agent="claude-code")
        text = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("[TokenSafe OUMV routing brief]", text)
        self.assertIn("tokensafe dev", text)

    def _go_repo(self):
        other = tempfile.mkdtemp(prefix="tokensafe_brief_go_")
        self.addCleanup(shutil.rmtree, other, ignore_errors=True)
        open(os.path.join(other, ".git"), "w").close()
        with open(os.path.join(other, "go.mod"), "w", encoding="utf-8") as f:
            f.write("module example.com/api\n\ngo 1.21\n")
        with open(os.path.join(other, "main.go"), "w", encoding="utf-8") as f:
            f.write('package main\n\nimport "github.com/gin-gonic/gin"\n\nfunc main() {\n\tr := gin.Default()\n\t_ = r\n}\n')
        return other

    def test_each_extra_root_is_routed_with_its_own_workspace_dir(self):
        # The primary root is Vue; the second root is Go and must be routed on its
        # own instead of inheriting the primary verdict.
        self._write("package.json", json.dumps({"dependencies": {"vue": "^3.5.0", "vite": "^6.0.0"}}))
        other = self._go_repo()
        brief = build_workspace_brief(self.tmp, extra_workspaces=[other])
        self.assertIn("tokensafe dev --framework vue", brief)  # primary verdict stays
        self.assertIn(other, brief)                            # extra root is named
        self.assertIn("backend go", brief)                     # with its own stack
        self.assertIn(f"workspace_dir='{other}'", brief)       # exact call to make

    def test_scope_sentence_is_always_present(self):
        self._write("package.json", json.dumps({"dependencies": {"vue": "^3.5.0", "vite": "^6.0.0"}}))
        brief = build_workspace_brief(self.tmp)
        self.assertIn("scope:", brief)
        self.assertIn("workspace_dir=", brief)

    def test_workspace_roots_helpers(self):
        from src.session_brief import workspace_roots_from_env, workspace_roots_from_payload

        payload = {"workspacePaths": [self.tmp, "/b"]}
        self.assertEqual(workspace_roots_from_payload(payload), [self.tmp, "/b"])
        self.assertEqual(workspace_roots_from_payload({"cwd": self.tmp}), [])
        env = {"TOKENSAFE_WORKSPACES": f"/x{os.pathsep}/y"}
        self.assertEqual(workspace_roots_from_env(env), ["/x", "/y"])

    def test_second_workspace_path_reaches_the_hook_brief(self):
        self._write("package.json", json.dumps({"dependencies": {"react": "^18.3.1", "vite": "^5.4.0"}}))
        other = self._go_repo()
        from hooks.claude_code_hook import handle

        out = handle(
            "SessionStart",
            {"cwd": self.tmp, "session_id": "s-multi", "workspacePaths": [self.tmp, other]},
            agent="claude-code",
        )
        text = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn(other, text)
        self.assertIn(f"workspace_dir='{other}'", text)


if __name__ == "__main__":
    unittest.main()
