"""Every detected manifest appears in `packages`, including unsupported ones.

The 2026-11-28 paidz session: `detected_files` listed paidz-backend/package.json and
paidz-workers/package.json, but `packages` held only the three supported ones — a Bun
Elysia service and a Bun worker looked like they did not exist. Both are unsupported,
and the honest answer is a row that says so.
"""
import json
import os
import shutil
import tempfile
import unittest

from src.retrofit import detect_project_stack


class StackPackagesTest(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp(prefix="tokensafe_stack_")
        self.addCleanup(shutil.rmtree, self.ws, ignore_errors=True)

    def write(self, rel, content):
        path = os.path.join(self.ws, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        return path

    def pkg(self, rel_dir, **fields):
        return self.write(os.path.join(rel_dir, "package.json"), json.dumps(fields))

    def test_elysia_and_bun_worker_are_reported_as_unsupported_packages(self):
        self.pkg(
            "paidz-backend",
            dependencies={"elysia": "^1.2.0"},
            devDependencies={"@types/bun": "^1.2.0"},
        )
        self.write("paidz-backend/bun.lockb", "")
        self.pkg("paidz-workers", devDependencies={"@types/bun": "^1.2.0"})
        self.write("paidz-core/go.mod", "module example.com/core\n\ngo 1.22\n")

        stack = detect_project_stack(self.ws)
        packages = {entry["path"]: entry for entry in stack["packages"]}

        self.assertIn("paidz-backend", packages)
        self.assertIn("paidz-workers", packages)
        self.assertIn("paidz-core", packages)

        elysia = packages["paidz-backend"]
        self.assertEqual(elysia["framework"], "elysia")
        self.assertEqual(elysia["provider"], "elysia")
        self.assertEqual(elysia["adapter"], "tokensafe-backend/elysia")
        self.assertTrue(elysia["oumvSupported"])
        self.assertEqual(elysia["status"], "ready")

        worker = packages["paidz-workers"]
        self.assertEqual(worker["framework"], "bun")
        self.assertEqual(worker["role"], "worker")
        self.assertFalse(worker["oumvSupported"])
        self.assertIn("reason", worker)

        # Elysia is a supported HTTP backend, so the first supported service keeps the
        # top-level slot (paidz-backend sorts before paidz-core); the Go row stays in
        # `packages` and is served by the Go adapter.
        self.assertEqual(stack["backend"]["framework"], "elysia")
        go_row = packages["paidz-core"]
        self.assertTrue(go_row["oumvSupported"])
        self.assertEqual(go_row["adapter"], "tokensafe-backend/go")
        self.assertIn("bun", stack["package_managers"])

    def test_a_manifest_without_any_framework_is_still_accounted_for(self):
        self.pkg("scripts-repo", dependencies={"zod": "^3.0.0"})
        stack = detect_project_stack(self.ws)
        entry = next((p for p in stack["packages"] if p["path"] == "scripts-repo"), None)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["role"], "package")
        self.assertFalse(entry["oumvSupported"])
        self.assertEqual(entry["status"], "unsupported")

    def test_express_keeps_its_ready_row_unchanged(self):
        self.pkg("api", dependencies={"express": "^4.19.0"})
        stack = detect_project_stack(self.ws)
        entry = next(p for p in stack["packages"] if p["path"] == "api")
        self.assertEqual(entry["framework"], "express")
        self.assertEqual(entry["provider"], "node")
        self.assertTrue(entry["oumvSupported"])
        self.assertEqual(entry["status"], "ready")


if __name__ == "__main__":
    unittest.main()
