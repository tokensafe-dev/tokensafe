"""
Unit tests for the TokenSafe OUMV Retrofit & Stack Detection Engine.
"""

import unittest
import os
import tempfile
import json
import shutil

from src.retrofit import (
    detect_project_stack,
    generate_config,
    get_adapters_catalog,
    run_doctor,
    OUMVProjectContext,
)


class TestRetrofitEngine(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_detect_react_project(self):
        pkg = {
            "name": "my-react-app",
            "dependencies": {
                "react": "^18.2.0",
                "react-dom": "^18.2.0",
                "zustand": "^4.5.0",
            }
        }
        with open(os.path.join(self.test_dir, "package.json"), "w", encoding="utf-8") as f:
            json.dump(pkg, f)

        stack = detect_project_stack(self.test_dir)
        self.assertIsNotNone(stack["frontend"])
        self.assertEqual(stack["frontend"]["framework"], "react")
        self.assertEqual(stack["frontend"]["provider"], "react")
        self.assertIn("zustand", stack["frontend"]["state_managers"])
        self.assertIsNone(stack["backend"])

    def test_detect_go_gin_project(self):
        go_mod_content = """module myapp

go 1.22

require (
    github.com/gin-gonic/gin v1.9.1
)
"""
        with open(os.path.join(self.test_dir, "go.mod"), "w", encoding="utf-8") as f:
            f.write(go_mod_content)

        stack = detect_project_stack(self.test_dir)
        self.assertIsNotNone(stack["backend"])
        self.assertEqual(stack["backend"]["framework"], "gin")
        self.assertEqual(stack["backend"]["provider"], "go")
        self.assertEqual(stack["backend"]["adapter"], "tokensafe-backend/go")

    def test_detect_fastapi_project(self):
        pyproject = """[project]
name = "my-fastapi-app"
dependencies = [
    "fastapi>=0.100.0",
    "uvicorn>=0.23.0"
]
"""
        with open(os.path.join(self.test_dir, "pyproject.toml"), "w", encoding="utf-8") as f:
            f.write(pyproject)

        stack = detect_project_stack(self.test_dir)
        self.assertIsNotNone(stack["backend"])
        self.assertEqual(stack["backend"]["framework"], "fastapi")
        self.assertEqual(stack["backend"]["adapter"], "tokensafe-backend/python")

    def test_generate_config_idempotency_and_safe_defaults(self):
        pkg = {"dependencies": {"react": "^18.0.0"}}
        with open(os.path.join(self.test_dir, "package.json"), "w", encoding="utf-8") as f:
            json.dump(pkg, f)

        cfg, conf_file, created = generate_config(self.test_dir)
        self.assertTrue(created)
        self.assertTrue(os.path.isfile(conf_file))
        self.assertFalse(cfg["permissions"]["mutate"])
        self.assertTrue(cfg["permissions"]["observe"])

        # Second call does not overwrite
        cfg2, conf_file2, created2 = generate_config(self.test_dir)
        self.assertFalse(created2)
        self.assertEqual(cfg, cfg2)

    def test_adapters_catalog(self):
        catalog = get_adapters_catalog()
        self.assertGreaterEqual(len(catalog), 5)
        adapter_ids = [a["id"] for a in catalog]
        self.assertIn("frontend/react", adapter_ids)
        self.assertIn("frontend/solid", adapter_ids)
        self.assertIn("backend/go", adapter_ids)
        self.assertIn("backend/fastapi", adapter_ids)

    def test_project_context_resolver(self):
        pkg = {"dependencies": {"react": "^18.0.0"}}
        with open(os.path.join(self.test_dir, "package.json"), "w", encoding="utf-8") as f:
            json.dump(pkg, f)

        ctx = OUMVProjectContext(self.test_dir)
        self.assertEqual(ctx.resolve_target_runtime("POST /api/orders"), "backend")
        self.assertEqual(ctx.resolve_target_runtime("<CheckoutButton />"), "frontend")
        self.assertEqual(ctx.resolve_target_runtime("button#submit"), "frontend")
        self.assertEqual(ctx.resolve_target_runtime("/api/v1/users"), "backend")


if __name__ == "__main__":
    unittest.main()
