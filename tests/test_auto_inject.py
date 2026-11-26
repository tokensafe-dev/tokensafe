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
            f.write("module example.com/app\n\ngo 1.21\n")
        main_path = os.path.join(self.tmp, "main.go")
        with open(main_path, "w", encoding="utf-8") as f:
            f.write(GIN_MAIN)
        original = open(main_path, "rb").read()

        with patch.object(auto_inject, "_go_build", return_value=(True, "")):
            patched = auto_inject.apply_go_patch(self.tmp)

        self.assertEqual(patched["status"], "patched")
        after = open(main_path, encoding="utf-8").read()
        self.assertIn("// tokensafe:oumv:begin", after)
        self.assertIn("oumvgin.AutoMount(r)", after)
        self.assertIn('oumvgin "github.com/tokensafe-dev/tokensafe-backend/go/pkg/adapters/gin"', after)

        reverted = auto_inject.revert_go_patch(self.tmp)
        self.assertEqual(reverted["status"], "reverted")
        self.assertEqual(open(main_path, "rb").read(), original)

    def test_go_patch_rolls_back_when_build_fails(self):
        with open(os.path.join(self.tmp, "go.mod"), "w", encoding="utf-8") as f:
            f.write("module example.com/app\n\ngo 1.21\n")
        main_path = os.path.join(self.tmp, "main.go")
        with open(main_path, "w", encoding="utf-8") as f:
            f.write(GIN_MAIN)
        original = open(main_path, "rb").read()

        with patch.object(auto_inject, "_go_build", return_value=(False, "cannot find package")):
            result = auto_inject.apply_go_patch(self.tmp)

        self.assertEqual(result["status"], "reverted")
        self.assertEqual(open(main_path, "rb").read(), original)

    def test_unknown_backend_is_reported_as_unsupported(self):
        with open(os.path.join(self.tmp, "package.json"), "w", encoding="utf-8") as f:
            json.dump({"name": "api", "dependencies": {"express": "^4.19.0"}}, f)

        data = auto_inject.plan(self.tmp)
        kinds = [a["kind"] for a in data["actions"]]
        self.assertIn("unsupported", kinds)


if __name__ == "__main__":
    unittest.main()
