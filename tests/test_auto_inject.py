"""Tests for the automatic adapter wiring (`tokensafe inject`)."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from src import auto_inject

_GO_MODULE_VERSION = auto_inject._GO_MODULE_VERSION
_SIDECAR_RELEASE_TAG = auto_inject._SIDECAR_RELEASE_TAG


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

DOTNET_PROGRAM = '''using Microsoft.EntityFrameworkCore;

var builder = WebApplication.CreateBuilder(args);
builder.Services.AddDbContext<AppDbContext>(options => options.UseSqlite("Data Source=app.db"));

var app = builder.Build();
app.MapGet("/", () => "ok");
app.Run();
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

    def test_elysia_app_gets_a_plugin_line_and_a_clean_revert(self):
        with open(os.path.join(self.tmp, "package.json"), "w", encoding="utf-8") as f:
            json.dump(
                {
                    "name": "api",
                    "scripts": {"dev": "bun --watch src/index.ts"},
                    "dependencies": {"elysia": "^1.2.20"},
                    "devDependencies": {"@types/bun": "latest"},
                },
                f,
            )
        os.makedirs(os.path.join(self.tmp, "src"))
        path = os.path.join(self.tmp, "src", "index.ts")
        original = (
            "import { Elysia } from 'elysia';\n\n"
            "export const app = new Elysia()\n"
            "  .get('/api/orders', () => ({ orders: [] }));\n\n"
            "app.listen(3000);\n"
        )
        with open(path, "w", encoding="utf-8") as f:
            f.write(original)

        plan = auto_inject.plan(self.tmp)
        action = next(a for a in plan["actions"] if a["kind"] == "elysia_plugin_patch")
        self.assertEqual(action["framework"], "elysia")
        self.assertIn("bun add tokensafe-backend-node", action["install"])

        patched = auto_inject.apply_elysia_patch(self.tmp)
        self.assertEqual(patched["status"], "patched")
        after = open(path, encoding="utf-8").read()
        self.assertIn("// tokensafe:oumv:begin", after)
        self.assertIn("app.use(_oumv());", after)
        # Inserted before `.listen(`: the `.use(...)` chain above stays intact.
        self.assertLess(after.index("app.use(_oumv());"), after.index("app.listen(3000)"))

        again = auto_inject.apply_elysia_patch(self.tmp)
        self.assertEqual(again["status"], "already_patched")
        reverted = auto_inject.revert_elysia_patch(self.tmp)
        self.assertEqual(reverted["status"], "reverted")
        self.assertEqual(open(path, encoding="utf-8").read(), original)

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
        self.assertIn(_SIDECAR_RELEASE_TAG, note)
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
            f.write(f'module example.com/app\n\ngo 1.21\n\nrequire github.com/tokensafe-dev/tokensafe-backend/go {_GO_MODULE_VERSION}\n')
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
            f.write(f'module example.com/app\n\ngo 1.21\n\nrequire github.com/tokensafe-dev/tokensafe-backend/go {_GO_MODULE_VERSION}\n')
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
            f.write(f'module example.com/app\n\ngo 1.21\n\nrequire github.com/tokensafe-dev/tokensafe-backend/go {_GO_MODULE_VERSION}\n')
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


    def _write_dotnet_project(self):
        with open(os.path.join(self.tmp, "Cert.csproj"), "w", encoding="utf-8") as f:
            f.write('<Project Sdk="Microsoft.NET.Sdk.Web">\n  <PropertyGroup>'
                    "<TargetFramework>net10.0</TargetFramework></PropertyGroup>\n</Project>\n")
        with open(os.path.join(self.tmp, "Program.cs"), "w", encoding="utf-8") as f:
            f.write(DOTNET_PROGRAM)
        return os.path.join(self.tmp, "Program.cs")

    def test_dotnet_patch_is_reversible_byte_for_byte(self):
        program = self._write_dotnet_project()
        original = open(program, "rb").read()

        with patch.object(auto_inject, "_ensure_dotnet_dependency", return_value=None), \
             patch.object(auto_inject, "_dotnet_build", return_value=(True, "")):
            patched = auto_inject.apply_dotnet_patch(self.tmp)

        self.assertEqual(patched["status"], "patched")
        self.assertEqual(patched["mode"], "dotnet_mount")
        after = open(program, encoding="utf-8").read()
        self.assertIn("// tokensafe:oumv:begin", after)
        self.assertIn("using TokenSafe.Oumv;", after)
        self.assertIn("builder.Services.AddTokensafe();", after)
        self.assertIn("app.UseTokensafe();", after)
        self.assertLess(after.index("using TokenSafe.Oumv;"), after.index("var builder"))

        reverted = auto_inject.revert_dotnet_patch(self.tmp)
        self.assertEqual(reverted["status"], "reverted")
        self.assertEqual(open(program, "rb").read(), original)

        # A .NET manifest must never be consumed by the Go/Node reverters: they
        # used to share one manifest.json and stole each other's backups.
        self.assertEqual(auto_inject.revert_go_patch(self.tmp)["status"], "nothing-to-revert")
        self.assertEqual(auto_inject.revert_node_patch(self.tmp)["status"], "clean")

    def test_dotnet_patch_rolls_back_when_build_fails(self):
        program = self._write_dotnet_project()
        original = open(program, "rb").read()

        with patch.object(auto_inject, "_ensure_dotnet_dependency", return_value=None), \
             patch.object(auto_inject, "_dotnet_build", return_value=(False, "error CS0246")):
            result = auto_inject.apply_dotnet_patch(self.tmp)

        self.assertEqual(result["status"], "reverted")
        self.assertEqual(open(program, "rb").read(), original)

    def test_dotnet_plan_offers_the_middleware_patch(self):
        self._write_dotnet_project()
        data = auto_inject.plan(self.tmp)
        action = next(item for item in data["actions"] if item["profile"] == "backend")
        self.assertEqual(action["kind"], "dotnet_one_line_patch")
        self.assertEqual(action["framework"], "aspnetcore")
        self.assertTrue(action["writes_repo"])
        self.assertFalse(action["already_patched"])
        self.assertIn("TokenSafe.Oumv.AspNetCore", action["install"])



if __name__ == "__main__":
    unittest.main()


def _write_next_app(tmp_path, version="15.4.0", src=False):
    import json as _json
    (tmp_path / "package.json").write_text(
        _json.dumps({
            "name": "next-app",
            "dependencies": {"next": version, "react": "19.0.0", "react-dom": "19.0.0"},
        }),
        encoding="utf-8",
    )
    app = tmp_path / "src" / "app" if src else tmp_path / "app"
    app.mkdir(parents=True)
    return app


def test_next_app_gets_the_three_file_bridge_patch_and_a_clean_revert(tmp_path):
    """Next >= 15.3 inject: RPC route + bridge route + instrumentation-client."""
    from src import auto_inject

    _write_next_app(tmp_path, "15.4.0")

    plan = auto_inject.plan(str(tmp_path))
    kinds = [action["kind"] for action in plan["actions"]]
    assert "next_route_handler" in kinds
    assert "frontend_zero_footprint" not in kinds
    next_action = next(action for action in plan["actions"] if action["kind"] == "next_route_handler")
    assert next_action["writes_repo"] is True
    assert next_action["bridge"] == "instrumentation"
    assert len(next_action["files"]) == 3
    assert next_action["db"]["sandbox"] == "none"

    applied = auto_inject.apply(str(tmp_path))
    wrote = [action for action in applied["actions"] if action["kind"] == "next_route_handler"]
    assert wrote and wrote[0]["status"] == "patched"
    rpc = tmp_path / "app" / "api" / "tokensafe" / "route.ts"
    bridge = tmp_path / "app" / "api" / "tokensafe" / "bridge" / "route.ts"
    instrumentation = tmp_path / "instrumentation-client.ts"
    assert rpc.is_file() and "bridge: true" in rpc.read_text(encoding="utf-8")
    assert bridge.is_file() and "createNextBridgeHandlers" in bridge.read_text(encoding="utf-8")
    assert instrumentation.is_file() and "installNextBridge" in instrumentation.read_text(encoding="utf-8")

    reverted = auto_inject.revert(str(tmp_path))
    assert reverted["next"]["status"] == "reverted"
    assert not rpc.exists() and not bridge.exists() and not instrumentation.exists()
    assert not (tmp_path / "app" / "api" / "tokensafe").exists()
    assert not (tmp_path / "app" / "api").exists()


def test_next_below_15_3_gets_routes_and_a_manual_bridge_snippet(tmp_path):
    """A Next that cannot load instrumentation-client is told so, not patched anyway."""
    from src import auto_inject

    _write_next_app(tmp_path, "15.0.0")

    plan = auto_inject.plan(str(tmp_path))
    next_action = next(action for action in plan["actions"] if action["kind"] == "next_route_handler")
    assert next_action["bridge"] == "manual"
    assert "installNextBridge" in next_action["bridge_manual"]["snippet"]

    applied = auto_inject.apply(str(tmp_path))
    wrote = next(action for action in applied["actions"] if action["kind"] == "next_route_handler")
    assert wrote["bridge"] == "manual"
    assert (tmp_path / "app" / "api" / "tokensafe" / "route.ts").is_file()
    assert (tmp_path / "app" / "api" / "tokensafe" / "bridge" / "route.ts").is_file()
    assert not (tmp_path / "instrumentation-client.ts").exists()


def test_next_src_app_project_keeps_instrumentation_under_src(tmp_path):
    """Next resolves instrumentation-client from src/ when the app lives there."""
    from src import auto_inject

    app = _write_next_app(tmp_path, "15.5.0", src=True)
    health = app / "api" / "health" / "route.ts"
    health.parent.mkdir(parents=True)
    health.write_text('export async function GET() { return new Response("ok"); }\n', encoding="utf-8")

    auto_inject.apply(str(tmp_path))
    assert (app / "api" / "tokensafe" / "route.ts").is_file()
    assert (app / "api" / "tokensafe" / "bridge" / "route.ts").is_file()
    assert (tmp_path / "src" / "instrumentation-client.ts").is_file()

    auto_inject.revert(str(tmp_path))
    assert not (app / "api" / "tokensafe").exists()
    # A neighbour route and its directories are untouched.
    assert health.is_file()


def test_next_foreign_route_is_never_overwritten(tmp_path):
    """An existing route we did not generate is the user's: skip, do not guess."""
    from src import auto_inject

    app = _write_next_app(tmp_path, "15.4.0")
    route = app / "api" / "tokensafe" / "route.ts"
    route.parent.mkdir(parents=True)
    route.write_text("// hand-written\nexport const POST = () => new Response('mine');\n", encoding="utf-8")

    applied = auto_inject.apply(str(tmp_path))
    wrote = next(action for action in applied["actions"] if action["kind"] == "next_route_handler")
    assert wrote["status"] == "skipped"
    assert "without bridge" in wrote["reason"]
    assert route.read_text(encoding="utf-8").startswith("// hand-written")
    assert not (app / "api" / "tokensafe" / "bridge").exists()


def test_next_v1_route_is_upgraded_in_place(tmp_path):
    """The generated v1 route has no bridge; recognising our own output lets us upgrade."""
    from src import auto_inject

    app = _write_next_app(tmp_path, "15.4.0")
    route = app / "api" / "tokensafe" / "route.ts"
    route.parent.mkdir(parents=True)
    route.write_text(auto_inject._NEXT_LEGACY_ROUTE_SOURCE, encoding="utf-8")

    applied = auto_inject.apply(str(tmp_path))
    wrote = next(action for action in applied["actions"] if action["kind"] == "next_route_handler")
    assert wrote["status"] == "patched"
    assert "bridge: true" in route.read_text(encoding="utf-8")
    assert (app / "api" / "tokensafe" / "bridge" / "route.ts").is_file()

    auto_inject.revert(str(tmp_path))
    assert not route.exists()


def test_inject_plan_reports_the_orm_sandbox_mode(tmp_path):
    """The ORM hint names the auto/manual split per stack (the .NET scan is the model)."""
    import json as _json
    from src import auto_inject

    (tmp_path / "package.json").write_text(
        _json.dumps({"name": "svc", "dependencies": {"express": "4.0.0", "@prisma/client": "5.0.0"}}),
        encoding="utf-8",
    )
    plan = auto_inject.plan(str(tmp_path))
    assert plan["actions"][0]["db"]["orm"] == "prisma"
    assert plan["actions"][0]["db"]["sandbox"] == "auto"
