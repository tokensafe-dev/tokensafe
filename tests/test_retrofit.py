"""
Unit tests for the TokenSafe OUMV Retrofit & Stack Detection Engine.
"""

import unittest
import os
import subprocess
import tempfile
import json
import shutil

from src.retrofit import (
    detect_project_stack,
    generate_config,
    run_doctor,
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

    def test_backend_precedence_prefers_supported_stack(self):
        """A supported Go/Python backend must outrank another supported one."""
        os.makedirs(os.path.join(self.test_dir, "api"))
        os.makedirs(os.path.join(self.test_dir, "python"))
        with open(os.path.join(self.test_dir, "api", "package.json"), "w", encoding="utf-8") as f:
            json.dump({"name": "api", "dependencies": {"express": "^4.19.0"}}, f)

        stack = detect_project_stack(self.test_dir)
        self.assertEqual(stack["backend"]["provider"], "node")
        # Express ships a certified adapter now, so the node root is supported too.
        self.assertTrue(stack["backend"]["oumv_supported"])

        with open(os.path.join(self.test_dir, "python", "pyproject.toml"), "w", encoding="utf-8") as f:
            f.write("[project]\ndependencies = ['fastapi']\n")
        stack = detect_project_stack(self.test_dir)
        self.assertEqual(stack["backend"]["provider"], "fastapi")
        self.assertTrue(stack["backend"]["oumv_supported"])
        # The same manifest is reachable from the root scan and monorepo discovery: one entry.
        python_pkgs = [p for p in stack["packages"] if p["path"] == "python"]
        self.assertEqual(len(python_pkgs), 1, stack["packages"])

        with open(os.path.join(self.test_dir, "go.mod"), "w", encoding="utf-8") as f:
            f.write("module app\n\ngo 1.22\n")
        stack = detect_project_stack(self.test_dir)
        self.assertEqual(stack["backend"]["provider"], "go")

    def test_root_frontend_scope_ignores_tooling_scripts(self):
        """Root-level .ts tooling must not be nudged as OUMV-observable frontend."""
        from src.retrofit import is_path_in_supported_oumv_scope
        with open(os.path.join(self.test_dir, "package.json"), "w", encoding="utf-8") as f:
            json.dump({"dependencies": {"react": "^18.2.0"}, "devDependencies": {"vite": "^5.0.0"}}, f)
        os.makedirs(os.path.join(self.test_dir, "scripts"))
        os.makedirs(os.path.join(self.test_dir, "src", "hooks"))
        build_ts = os.path.join(self.test_dir, "scripts", "build.ts")
        hook_ts = os.path.join(self.test_dir, "src", "hooks", "useAuth.ts")
        for p in (build_ts, hook_ts):
            with open(p, "w", encoding="utf-8") as f:
                f.write("export const x = 1;\n")

        self.assertIsNone(is_path_in_supported_oumv_scope(self.test_dir, build_ts))
        self.assertIsNotNone(is_path_in_supported_oumv_scope(self.test_dir, hook_ts))

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
        self.assertTrue(cfg["permissions"]["mutate"])
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
        self.assertIn("backend/aspnetcore", adapter_ids)

    def test_adapters_catalog_follows_adapter_contract(self):
        # Language-neutral OUMV Adapter Contract (agent-oumv/schemas/adapter.json)
        catalog = get_adapters_catalog()
        ready = [a for a in catalog if a.get("status") == "ready"]
        self.assertGreaterEqual(len(ready), 5)
        for adapter in ready:
            for field in ("id", "name", "profiles", "repo", "mode", "status"):
                self.assertIn(field, adapter, f"{adapter.get('id')}: missing {field}")
            tech = adapter.get("technology", {})
            self.assertIn("language", tech, f"{adapter['id']}: missing technology.language")
            package = tech.get("package", {})
            self.assertIn("manager", package, f"{adapter['id']}: missing package.manager")
            self.assertIn(package["manager"], ("npm", "pip", "go", "binary", "nuget"))
            self.assertTrue(package.get("install"), f"{adapter['id']}: missing install command")
            # Baseline capabilities with constraints (Decision 12: no unguarded claims)
            caps = adapter.get("capabilities", [])
            self.assertTrue(caps, f"{adapter['id']}: must declare capabilities")
            for cap in caps:
                self.assertIn("name", cap)
                self.assertIn("constraints", cap)
                self.assertIn("readOnly", cap["constraints"])
            # Mutations must declare a permission constraint
            mutators = [c for c in caps if c["name"].startswith("mutate.")]
            self.assertTrue(mutators, f"{adapter['id']}: mutating adapters must declare mutate capabilities")
            for cap in mutators:
                self.assertTrue(cap["constraints"].get("requiresPermission"))

    def test_project_context_resolver(self):
        pkg = {"dependencies": {"react": "^18.0.0"}}
        with open(os.path.join(self.test_dir, "package.json"), "w", encoding="utf-8") as f:
            json.dump(pkg, f)

        ctx = OUMVProjectContext(self.test_dir)
        self.assertEqual(ctx.resolve_target_runtime("POST /api/orders"), "backend")
        self.assertEqual(ctx.resolve_target_runtime("<CheckoutButton />"), "frontend")
        self.assertEqual(ctx.resolve_target_runtime("button#submit"), "frontend")
        self.assertEqual(ctx.resolve_target_runtime("/api/v1/users"), "backend")



class TestZeroTraceIntegration(unittest.TestCase):
    """The generated manifest must never appear as a git change."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="tokensafe_integrate_")
        subprocess.run(["git", "init", "-q", self.test_dir], check=True)
        with open(os.path.join(self.test_dir, "package.json"), "w", encoding="utf-8") as f:
            json.dump({"name": "app", "dependencies": {"react": "^18.3.1"}, "devDependencies": {"vite": "^5.4.0"}}, f)
        subprocess.run(["git", "-C", self.test_dir, "add", "package.json"], check=True)
        subprocess.run(
            ["git", "-C", self.test_dir, "-c", "user.email=t@example.com", "-c", "user.name=Test",
             "commit", "-qm", "fixture"],
            check=True,
        )

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_generate_config_stays_out_of_git_status(self):
        generate_config(self.test_dir)

        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=self.test_dir, capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertEqual(status, "", f"integration left tracked changes: {status!r}")

        # The local-only exclude is what keeps it invisible (never the tracked .gitignore).
        exclude_path = os.path.join(self.test_dir, ".git", "info", "exclude")
        with open(exclude_path, encoding="utf-8") as f:
            self.assertIn(".tokensafe/", f.read())
        self.assertFalse(os.path.exists(os.path.join(self.test_dir, ".gitignore")))

    def test_doctor_is_not_ready_without_an_installed_adapter(self):
        generate_config(self.test_dir)
        doc = run_doctor(self.test_dir)
        self.assertFalse(doc["ready_for_agent"])
        self.assertTrue(doc["blocking"])
        self.assertIn("node_modules/tokensafe-ui", doc["blocking"][0])

        # Adapter present -> ready (no live runtime needed for the static path).
        pkg_dir = os.path.join(self.test_dir, "node_modules", "tokensafe-ui")
        os.makedirs(pkg_dir)
        with open(os.path.join(pkg_dir, "package.json"), "w", encoding="utf-8") as f:
            json.dump({"name": "tokensafe-ui", "version": "0.1.21"}, f)

        doc = run_doctor(self.test_dir)
        self.assertTrue(doc["ready_for_agent"], doc["blocking"])
        self.assertTrue(doc["adapter_readiness"]["frontend"]["installed"])
        self.assertIn("node_modules/tokensafe-ui", doc["adapter_readiness"]["frontend"]["evidence"])


if __name__ == "__main__":
    unittest.main()
