"""
Unit tests for TokenSafe Greenfield Project Scaffolder.
"""

import unittest
import os
import json
import shutil
import tempfile
from unittest.mock import patch

from src.scaffold import ProjectScaffolder
from src.cli import main as cli_main


class TestProjectScaffolder(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_scaffold_fullstack(self):
        target = os.path.join(self.test_dir, "my-app")
        res = ProjectScaffolder.scaffold(
            target_dir=target,
            project_name="my-app",
            template="fullstack",
        )
        self.assertTrue(res["success"])
        self.assertEqual(res["template"], "fullstack")

        # Frontend files
        self.assertTrue(os.path.isfile(os.path.join(target, "frontend", "package.json")))
        self.assertTrue(os.path.isfile(os.path.join(target, "frontend", "vite.config.ts")))
        self.assertTrue(os.path.isfile(os.path.join(target, "frontend", "src", "App.tsx")))
        self.assertTrue(os.path.isfile(os.path.join(target, "frontend", "src", "main.ts")))

        # Backend files
        self.assertTrue(os.path.isfile(os.path.join(target, "backend", "main.py")))
        self.assertTrue(os.path.isfile(os.path.join(target, "backend", "pyproject.toml")))

        # Root files
        self.assertTrue(os.path.isfile(os.path.join(target, ".tokensafe", "runtime.json")))
        self.assertTrue(os.path.isfile(os.path.join(target, "AGENTS.md")))
        self.assertTrue(os.path.isfile(os.path.join(target, "README.md")))
        self.assertTrue(os.path.isfile(os.path.join(target, ".gitignore")))

        # Verify runtime.json has both endpoints
        with open(os.path.join(target, ".tokensafe", "runtime.json"), "r") as f:
            cfg = json.load(f)
            self.assertIn("frontend", cfg)
            self.assertIn("backend", cfg)
            self.assertEqual(cfg["frontend"]["name"], "tokensafe-ui")
            self.assertEqual(cfg["backend"]["name"], "tokensafe-python")

    def test_scaffold_ui_only(self):
        target = os.path.join(self.test_dir, "my-ui")
        res = ProjectScaffolder.scaffold(
            target_dir=target,
            project_name="my-ui",
            template="ui",
        )
        self.assertTrue(res["success"])
        self.assertTrue(os.path.isfile(os.path.join(target, "package.json")))
        self.assertTrue(os.path.isfile(os.path.join(target, "src", "App.tsx")))
        self.assertFalse(os.path.exists(os.path.join(target, "main.py")))

        with open(os.path.join(target, ".tokensafe", "runtime.json"), "r") as f:
            cfg = json.load(f)
            self.assertIn("frontend", cfg)
            self.assertNotIn("backend", cfg)

    def test_scaffold_backend_only(self):
        target = os.path.join(self.test_dir, "my-api")
        res = ProjectScaffolder.scaffold(
            target_dir=target,
            project_name="my-api",
            template="backend",
        )
        self.assertTrue(res["success"])
        self.assertTrue(os.path.isfile(os.path.join(target, "main.py")))
        self.assertTrue(os.path.isfile(os.path.join(target, "pyproject.toml")))
        self.assertFalse(os.path.exists(os.path.join(target, "package.json")))

        with open(os.path.join(target, ".tokensafe", "runtime.json"), "r") as f:
            cfg = json.load(f)
            self.assertIn("backend", cfg)
            self.assertNotIn("frontend", cfg)

    def test_cli_create(self):
        orig_cwd = os.getcwd()
        try:
            os.chdir(self.test_dir)
            with patch("sys.stdout"):
                cli_main(["create", "cli-test-app", "--template", "fullstack"])

            app_dir = os.path.join(self.test_dir, "cli-test-app")
            self.assertTrue(os.path.isdir(app_dir))
            self.assertTrue(os.path.isfile(os.path.join(app_dir, "AGENTS.md")))
            self.assertTrue(os.path.isfile(os.path.join(app_dir, ".tokensafe", "runtime.json")))
        finally:
            os.chdir(orig_cwd)


if __name__ == "__main__":
    unittest.main()
