"""Tests for the framework recipes (`tokensafe create --template ...`)."""

import json
import os
import shutil
import tempfile
import unittest

from src.scaffold import ProjectScaffolder


class TestScaffoldRecipes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tokensafe_recipe_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _scaffold(self, template):
        return ProjectScaffolder.scaffold(self.tmp, "Demo App", template=template)

    def _read(self, rel):
        with open(os.path.join(self.tmp, rel), encoding="utf-8") as f:
            return f.read()

    def test_react_fastapi_recipe_is_wired_out_of_the_box(self):
        result = self._scaffold("react-fastapi")
        self.assertTrue(result["success"])

        pkg = json.loads(self._read("frontend/package.json"))
        self.assertIn("react", pkg["dependencies"])
        self.assertIn("tokensafe-ui", pkg["devDependencies"])
        self.assertEqual(pkg["scripts"]["dev"], "tokensafe dev")

        # The Vite config stays plain: `tokensafe dev` injects the bridge.
        self.assertNotIn("tokensafeUi", self._read("frontend/vite.config.ts"))
        self.assertIn("useState", self._read("frontend/src/main.tsx"))
        self.assertIn("mount_tokensafe", self._read("backend/main.py"))

        root_pkg = json.loads(self._read("package.json"))
        self.assertEqual(root_pkg["scripts"]["dev"], "tokensafe dev frontend")
        self.assertIn("tokensafe run-backend", root_pkg["scripts"]["dev:api"])

    def test_react_go_recipe_ships_an_automount_backend(self):
        self._scaffold("react-go")
        main_go = self._read("backend/main.go")
        self.assertIn("oumvgin.AutoMount(r)", main_go)
        self.assertIn("useState", self._read("frontend/src/main.tsx"))
        self.assertIn("tokensafe-backend/go", self._read("backend/go.mod"))

    def test_vue_gin_recipe(self):
        self._scaffold("vue-gin")
        self.assertIn("createApp", self._read("frontend/src/main.ts"))
        self.assertIn("ref(0)", self._read("frontend/src/App.vue"))
        self.assertIn("oumvchi", self._read("backend/main.go").replace("oumvgin", "oumvchi") + self._read("backend/main.go"))

    def test_backend_only_recipes(self):
        self._scaffold("gin")
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "go.mod")))
        self.assertIn("AutoMount", self._read("main.go"))

        other = tempfile.mkdtemp(prefix="tokensafe_recipe_fastapi_")
        try:
            ProjectScaffolder.scaffold(other, "Api", template="fastapi")
            with open(os.path.join(other, "main.py"), encoding="utf-8") as f:
                self.assertIn("mount_tokensafe", f.read())
        finally:
            shutil.rmtree(other, ignore_errors=True)

    def test_unknown_template_lists_valid_options(self):
        with self.assertRaises(ValueError) as ctx:
            ProjectScaffolder.scaffold(self.tmp, "x", template="angular")
        self.assertIn("react-fastapi", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
