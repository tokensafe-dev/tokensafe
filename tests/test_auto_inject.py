"""Tests for the automatic adapter wiring (`tokensafe inject`)."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from src import auto_inject


GIN_MAIN = '''package main

import (
	"net/http"

	"github.com/gin-gonic/gin"
)

func main() {
	r := gin.Default()
	r.GET("/ping", func(c *gin.Context) { c.JSON(http.StatusOK, gin.H{"ok": true}) })
	r.Run(":8080")
}
'''


class TestAutoInject(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tokensafe_inject_test_")
        self.home = tempfile.mkdtemp(prefix="tokensafe_home_")
        self.env = patch.dict(os.environ, {"TOKENSAFE_HOME": self.home})
        self.env.start()

    def test_node_patch_is_byte_reversible(self):
        with open(os.path.join(self.tmp, "package.json"), "w", encoding="utf-8") as f:
            json.dump({"name": "api", "main": "server.js", "dependencies": {"express": "^4.19.0"}}, f)
        server_path = os.path.join(self.tmp, "server.js")
        original = "const express = require('express');\nconst app = express();\napp.listen(3000);\n"
        with open(server_path, "w", encoding="utf-8") as f:
            f.write(original)

        plan = auto_inject.plan(self.tmp)
        node_action = next(action for action in plan["actions"] if action["kind"] == "node_one_line_patch")
        self.assertEqual(node_action["framework"], "express")
        self.assertFalse(node_action["already_patched"])

        patched = auto_inject.apply_node_patch(self.tmp)
        self.assertEqual(patched["status"], "patched")
        after = open(server_path, encoding="utf-8").read()
        self.assertIn("// tokensafe:oumv:begin", after)
        self.assertIn("mountTokensafe(app)", after)

        again = auto_inject.apply_node_patch(self.tmp)
        self.assertEqual(again["status"], "already_patched")

        reverted = auto_inject.revert_node_patch(self.tmp)
        self.assertEqual(reverted["status"], "reverted")
        self.assertEqual(open(server_path, encoding="utf-8").read(), original)

    def test_spring_and_php_are_detected_and_get_a_sidecar_action(self):
        os.makedirs(os.path.join(self.tmp, "svc"))
        with open(os.path.join(self.tmp, "svc", "pom.xml"), "w", encoding="utf-8") as f:
            f.write("<project><dependencies><dependency><artifactId>spring-boot-starter-web</artifactId>"
                    "</dependency></dependencies></project>")
        plan = auto_inject.plan(os.path.join(self.tmp, "svc"))
        sidecar = next(action for action in plan["actions"] if action["kind"] == "sidecar")
        self.assertEqual(sidecar["framework"], "spring")
        self.assertFalse(sidecar["writes_repo"])
        # A sibling tokensafe-backend checkout is cached; otherwise the note names
        # OUMV_BACKEND_REPO instead of pretending the sidecar exists.
        self.assertTrue(sidecar.get("run") or "OUMV_BACKEND_REPO" in sidecar.get("install", ""))
        if sidecar.get("run"):
            self.assertIn("--root", sidecar["run"])
            self.assertIn("--port 7331", sidecar["run"])

        php_dir = os.path.join(self.tmp, "phpapp")
        os.makedirs(php_dir)
        with open(os.path.join(php_dir, "composer.json"), "w", encoding="utf-8") as f:
            json.dump({"require": {"laravel/framework": "^11.0"}}, f)
        plan = auto_inject.plan(php_dir)
        sidecar = next(action for action in plan["actions"] if action["kind"] == "sidecar")
        self.assertEqual(sidecar["framework"], "laravel")
        self.assertFalse(sidecar["writes_repo"])
        if sidecar.get("run"):
            self.assertIn("-S 127.0.0.1:7332", sidecar["run"])
            self.assertIn("--root", sidecar["run"])

    def test_sidecar_cache_is_hash_pinned_and_replaces_a_tampered_file(self):
        cache_dir = auto_inject._sidecar_cache_dir()
        os.makedirs(cache_dir, exist_ok=True)
        tampered = os.path.join(cache_dir, "OumvSpring.java")
        with open(tampered, "w", encoding="utf-8") as f:
            f.write("// not the certified sidecar\n")

        path, note = auto_inject.ensure_sidecar("spring")
        self.assertIsNotNone(path)
        self.assertNotEqual(auto_inject._sha256_file(path), "changed")
        self.assertIn("v0.6.0", note)
        self.assertNotIn("mismatch", note)

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.home, ignore_errors=True)

    def _git_init(self):
        subprocess.run(["git", "init", "-q", self.tmp], check=True)
        subprocess.run(["git", "-C", self.tmp, "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", self.tmp, "-c", "user.email=t@example.com", "-c", "user.name=Test",
             "commit", "-qm", "fixture"],
            check=True,
        )

    def _status(self):
        return subprocess.run(
            ["git", "-C", self.tmp, "status", "--porcelain"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

    def test_plan_for_react_is_zero_footprint(self):
        with open(os.path.join(self.tmp, "package.json"), "w", encoding="utf-8") as f:
            json.dump({"name": "app", "dependencies": {"react": "^18.3.1"}, "devDependencies": {"vite": "^5.4.0"}}, f)

        data = auto_inject.plan(self.tmp)
        kinds = [a["kind"] for a in data["actions"]]
        self.assertIn("frontend_zero_footprint", kinds)
        action = next(a for a in data["actions"] if a["kind"] == "frontend_zero_footprint")
        self.assertFalse(action["writes_repo"])
        self.assertEqual(action["run"], "tokensafe dev")

    def test_python_autoload_writes_only_global_hook(self):
        with open(os.path.join(self.tmp, "main.py"), "w", encoding="utf-8") as f:
            f.write("from fastapi import FastAPI\napp = FastAPI()\n")
        with open(os.path.join(self.tmp, "requirements.txt"), "w", encoding="utf-8") as f:
            f.write("fastapi\n")
        self._git_init()

        result = auto_inject.apply(self.tmp)

        kinds = {a["kind"]: a for a in result["actions"]}
        self.assertEqual(kinds["python_autoload"]["status"], "written")
        hook = os.path.join(self.home, "pyhooks", "sitecustomize.py")
        self.assertTrue(os.path.isfile(hook))
        with open(hook, encoding="utf-8") as f:
            self.assertIn("TOKENSAFE_OUMV_AUTOMOUNT", f.read())

        # Nothing in the repository working tree: `.tokensafe/` is excluded locally.
        self.assertEqual(self._status(), "")

    def test_go_patch_is_reversible_byte_for_byte(self):
        with open(os.path.join(self.tmp, "go.mod"), "w", encoding="utf-8") as f:
            f.write('module example.com/app\n\ngo 1.21\n\nrequire github.com/tokensafe-dev/tokensafe-backend/go v0.6.0\n')
        main_path = os.path.join(self.tmp, "main.go")
        with open(main_path, "w", encoding="utf-8") as f:
            f.write(GIN_MAIN)
        original = open(main_path, "rb").read()

        with patch.object(auto_inject, "_go_tidy", return_value=(True, "")), \
             patch.object(auto_inject, "_go_build", return_value=(True, "")):
            patched = auto_inject.apply_go_patch(self.tmp)

        self.assertEqual(patched["status"], "patched")
        self.assertEqual(patched["mode"], "serve")
        after = open(main_path, encoding="utf-8").read()
        self.assertIn("// tokensafe:oumv:begin", after)
        self.assertIn('oumvgin.AutoMountAndServe(r, ":8080")', after)
        self.assertNotIn("r.Run(", after)
        self.assertIn('"log"', after)
        self.assertIn('oumvgin "github.com/tokensafe-dev/tokensafe-backend/go/pkg/adapters/gin"', after)

        reverted = auto_inject.revert_go_patch(self.tmp)
        self.assertEqual(reverted["status"], "reverted")
        self.assertEqual(open(main_path, "rb").read(), original)

    def test_go_patch_falls_back_to_mount_when_serve_call_is_not_simple(self):
        with open(os.path.join(self.tmp, "go.mod"), "w", encoding="utf-8") as f:
            f.write('module example.com/app\n\ngo 1.21\n\nrequire github.com/tokensafe-dev/tokensafe-backend/go v0.6.0\n')
        main_path = os.path.join(self.tmp, "main.go")
        with open(main_path, "w", encoding="utf-8") as f:
            f.write(GIN_MAIN.replace('r.Run(":8080")', "r.Run(port())"))

        with patch.object(auto_inject, "_go_tidy", return_value=(True, "")), \
             patch.object(auto_inject, "_go_build", return_value=(True, "")):
            patched = auto_inject.apply_go_patch(self.tmp)

        self.assertEqual(patched["status"], "patched")
        self.assertEqual(patched["mode"], "mount")
        after = open(main_path, encoding="utf-8").read()
        self.assertIn("oumvgin.AutoMount(r)", after)
        self.assertIn("r.Run(port())", after)

    def test_chi_serve_call_is_wrapped(self):
        source = """package main

import (
\t"net/http"

\t"github.com/go-chi/chi/v5"
)

func main() {
\tr := chi.NewRouter()
\thttp.ListenAndServe(":8080", r)
}
"""
        wrapped = auto_inject._wrap_go_serve(source, "chi")
        self.assertIsNotNone(wrapped)
        patched, preview = wrapped
        self.assertIn('oumvchi.AutoMountAndServe(r, ":8080")', patched)
        self.assertNotIn("http.ListenAndServe", patched)
        self.assertIn("AutoMountAndServe", preview)

    def test_go_patch_rolls_back_when_build_fails(self):
        with open(os.path.join(self.tmp, "go.mod"), "w", encoding="utf-8") as f:
            f.write('module example.com/app\n\ngo 1.21\n\nrequire github.com/tokensafe-dev/tokensafe-backend/go v0.6.0\n')
        main_path = os.path.join(self.tmp, "main.go")
        with open(main_path, "w", encoding="utf-8") as f:
            f.write(GIN_MAIN)
        original = open(main_path, "rb").read()

        with patch.object(auto_inject, "_go_build", return_value=(False, "cannot find package")):
            result = auto_inject.apply_go_patch(self.tmp)

        self.assertEqual(result["status"], "reverted")
        self.assertEqual(open(main_path, "rb").read(), original)

    def test_express_backend_is_wired_with_a_one_line_patch(self):
        with open(os.path.join(self.tmp, "package.json"), "w", encoding="utf-8") as f:
            json.dump({"name": "api", "main": "app.js", "dependencies": {"express": "^4.19.0"}}, f)
        with open(os.path.join(self.tmp, "app.js"), "w", encoding="utf-8") as f:
            f.write("const express = require('express');\nconst app = express();\n")

        data = auto_inject.plan(self.tmp)
        action = next(item for item in data["actions"] if item["profile"] == "backend")
        self.assertEqual(action["kind"], "node_one_line_patch")
        self.assertTrue(action["writes_repo"])
        self.assertIn("mountTokensafe", action["description"])


if __name__ == "__main__":
    unittest.main()
