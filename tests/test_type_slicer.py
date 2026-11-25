import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import json
import tempfile
import unittest
from unittest.mock import patch

from src.type_slicer import TypeSlicer


class TestTypeSlicer(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.orig_data_dir = os.environ.get("TOKENSAFE_DATA_DIR")
        os.environ["TOKENSAFE_DATA_DIR"] = self.tmp_dir.name
        TypeSlicer.clear_memory_cache()

    def tearDown(self):
        TypeSlicer.clear_memory_cache()
        if self.orig_data_dir is not None:
            os.environ["TOKENSAFE_DATA_DIR"] = self.orig_data_dir
        else:
            os.environ.pop("TOKENSAFE_DATA_DIR", None)
        self.tmp_dir.cleanup()

    def test_upward_node_modules_traversal(self):
        # Create repo_root/node_modules/my-lib
        repo_root = os.path.join(self.tmp_dir.name, "my-repo")
        sub_package = os.path.join(repo_root, "packages", "sub-app")
        os.makedirs(sub_package, exist_ok=True)

        lib_dir = os.path.join(repo_root, "node_modules", "my-lib")
        os.makedirs(lib_dir, exist_ok=True)
        with open(os.path.join(lib_dir, "package.json"), "w") as f:
            json.dump({"name": "my-lib", "version": "1.2.3", "types": "index.d.ts"}, f)

        # Calling find_package_dir from deep sub-package should find node_modules in repo_root
        found_dir = TypeSlicer.find_package_dir("my-lib", start_dir=sub_package)
        self.assertIsNotNone(found_dir)
        self.assertEqual(os.path.realpath(found_dir), os.path.realpath(lib_dir))

    def test_slice_symbol_with_reexport_hops(self):
        # Create a package with index.d.ts re-exporting from ./types
        pkg_dir = os.path.join(self.tmp_dir.name, "node_modules", "express-style")
        os.makedirs(pkg_dir, exist_ok=True)

        with open(os.path.join(pkg_dir, "package.json"), "w") as f:
            json.dump({"name": "express-style", "version": "4.18.0", "types": "index.d.ts"}, f)

        with open(os.path.join(pkg_dir, "index.d.ts"), "w") as f:
            f.write("export * from './types';\nexport interface App { name: string; }\n")

        with open(os.path.join(pkg_dir, "types.d.ts"), "w") as f:
            f.write("""
export interface Request {
    body: any;
    params: Record<string, string>;
    headers: Record<string, string>;
}

export interface Response {
    status(code: number): this;
    send(body: any): this;
}
""")

        # Slice Request (requires 1-hop re-export follow)
        res = TypeSlicer.slice_package_type("express-style", "Request", project_root=self.tmp_dir.name)
        self.assertTrue(res["found"])
        self.assertEqual(res["version"], "4.18.0")
        self.assertIn("interface Request {", res["type_code"])
        self.assertIn("body: any;", res["type_code"])
        self.assertNotIn("interface Response", res["type_code"])

        # Slice App (direct from index.d.ts)
        res_app = TypeSlicer.slice_package_type("express-style", "App", project_root=self.tmp_dir.name)
        self.assertTrue(res_app["found"])
        self.assertIn("interface App", res_app["type_code"])

    def test_l1_l2_caching(self):
        pkg_dir = os.path.join(self.tmp_dir.name, "node_modules", "simple-lib")
        os.makedirs(pkg_dir, exist_ok=True)
        with open(os.path.join(pkg_dir, "package.json"), "w") as f:
            json.dump({"name": "simple-lib", "version": "1.0.0", "types": "index.d.ts"}, f)
        with open(os.path.join(pkg_dir, "index.d.ts"), "w") as f:
            f.write("export interface Config { apiKey: string; }")

        # Live parse
        res1 = TypeSlicer.slice_package_type("simple-lib", "Config", project_root=self.tmp_dir.name)
        self.assertTrue(res1["found"])
        self.assertEqual(res1["source"], "parsed_live")

        # L1 Memory Cache hit
        res2 = TypeSlicer.slice_package_type("simple-lib", "Config", project_root=self.tmp_dir.name)
        self.assertTrue(res2["found"])
        self.assertEqual(res2["source"], "L1_memory")

        # Clear L1 to test L2 SQLite hit
        TypeSlicer.clear_memory_cache()
        res3 = TypeSlicer.slice_package_type("simple-lib", "Config", project_root=self.tmp_dir.name)
        self.assertTrue(res3["found"])
        self.assertEqual(res3["source"], "L2_sqlite")

    @patch("src.team_cache.TeamCache.get")
    def test_l3_team_cache_hit(self, mock_team_get):
        mock_team_get.return_value = "export interface CloudSession { id: string; user: string; }"

        # Library not present on disk, but present in L3 Team Cache (legacy raw string)
        res = TypeSlicer.slice_package_type("@cloud/client", "CloudSession", project_root=self.tmp_dir.name)
        self.assertTrue(res["found"])
        self.assertEqual(res["source"], "L3_team_cache")
        self.assertIn("interface CloudSession", res["type_code"])
        self.assertEqual(res.get("_dts_bytes"), 0)

    @patch("src.team_cache.TeamCache.get")
    def test_l3_team_cache_with_dts_bytes_and_l2_backfill(self, mock_team_get):
        mock_team_get.return_value = json.dumps({
            "code": "export interface TeamUser { id: string; role: string; }",
            "dts_bytes": 18500
        })

        # L3 hit with JSON payload containing dts_bytes
        res = TypeSlicer.slice_package_type("@org/team", "TeamUser", project_root=self.tmp_dir.name)
        self.assertTrue(res["found"])
        self.assertEqual(res["source"], "L3_team_cache")
        self.assertIn("interface TeamUser", res["type_code"])
        self.assertEqual(res.get("_dts_bytes"), 18500)

        # L1 hit should preserve _dts_bytes
        res_l1 = TypeSlicer.slice_package_type("@org/team", "TeamUser", project_root=self.tmp_dir.name)
        self.assertEqual(res_l1["source"], "L1_memory")
        self.assertEqual(res_l1.get("_dts_bytes"), 18500)

        # Clear L1: L2 SQLite hit should also preserve dts_bytes backfilled from L3
        TypeSlicer.clear_memory_cache()
        res_l2 = TypeSlicer.slice_package_type("@org/team", "TeamUser", project_root=self.tmp_dir.name)
        self.assertEqual(res_l2["source"], "L2_sqlite")
        self.assertEqual(res_l2.get("_dts_bytes"), 18500)

    def test_large_namespace_truncation_and_balanced_braces(self):
        # Create a huge namespace definition exceeding standard cap
        lines = ["export namespace HugeApi {"]
        for i in range(450):
            lines.append(f"    export interface Item{i} {{\n        id: number;\n        name: string;\n    }}")
        lines.append("}")
        huge_content = "\n".join(lines)

        extracted = TypeSlicer._extract_symbol_block(huge_content, "HugeApi")
        self.assertIsNotNone(extracted)
        self.assertIn("TokenSafe: truncated", extracted)

        # Braces must be balanced!
        open_count = extracted.count("{")
        close_count = extracted.count("}")
        self.assertEqual(open_count, close_count, f"Braces must be balanced: {open_count} open vs {close_count} close")

    def test_dts_bytes_reported_and_cached(self):
        pkg_dir = os.path.join(self.tmp_dir.name, "node_modules", "bytes-lib")
        os.makedirs(pkg_dir, exist_ok=True)
        with open(os.path.join(pkg_dir, "package.json"), "w") as f:
            json.dump({"name": "bytes-lib", "version": "1.0.0", "types": "index.d.ts"}, f)
        dts_content = "export interface MyInterface { field: string; }\n" * 10
        with open(os.path.join(pkg_dir, "index.d.ts"), "w") as f:
            f.write(dts_content)

        # Live parse reports _dts_bytes
        res1 = TypeSlicer.slice_package_type("bytes-lib", "MyInterface", project_root=self.tmp_dir.name)
        self.assertTrue(res1["found"])
        self.assertEqual(res1.get("_dts_bytes"), len(dts_content.encode("utf-8")))

        # L2 hit also reports _dts_bytes
        TypeSlicer.clear_memory_cache()
        res2 = TypeSlicer.slice_package_type("bytes-lib", "MyInterface", project_root=self.tmp_dir.name)
        self.assertTrue(res2["found"])
        self.assertEqual(res2["source"], "L2_sqlite")
        self.assertEqual(res2.get("_dts_bytes"), len(dts_content.encode("utf-8")))

    def test_cache_key_carries_slicer_version(self):
        # The poisoning guard: cache keys must embed the slicer algorithm version,
        # so slices produced by an older algorithm can never resurface from L1/L2/L3.
        pkg_dir = os.path.join(self.tmp_dir.name, "node_modules", "ver-lib")
        os.makedirs(pkg_dir, exist_ok=True)
        with open(os.path.join(pkg_dir, "package.json"), "w") as f:
            json.dump({"name": "ver-lib", "version": "2.0.0", "types": "index.d.ts"}, f)
        with open(os.path.join(pkg_dir, "index.d.ts"), "w") as f:
            f.write("export interface VerSym { ok: boolean; }\n")

        res = TypeSlicer.slice_package_type("ver-lib", "VerSym", project_root=self.tmp_dir.name)
        self.assertTrue(res["found"])

        import sqlite3
        from src.type_slicer import TypeSlicer as TS
        db = sqlite3.connect(os.path.join(self.tmp_dir.name, "type_cache.db"))
        keys = [r[0] for r in db.execute("SELECT type_key FROM type_cache").fetchall()]
        db.close()
        self.assertTrue(keys, "expected at least one cached type entry")
        for k in keys:
            self.assertIn(f"::{TS._SLICER_VERSION}", k)

        # Plant an old-format (unversioned) poisoned row: a fresh lookup must
        # never be served from it — it can only be reached by its own legacy key.
        self.assertIn("ver-lib@2.0.0::VerSym", keys[0])

    def test_exports_import_types_overloads_unions_and_standalone_at_types(self):
        # 1. exports["."]["import"]["types"] + union object literal + overloads
        pkg_dir = os.path.join(self.tmp_dir.name, "node_modules", "modern-lib")
        os.makedirs(pkg_dir, exist_ok=True)
        with open(os.path.join(pkg_dir, "package.json"), "w") as f:
            json.dump({"name": "modern-lib", "version": "1.2.3", "exports": {".": {"import": {"types": "./dist/index.d.ts"}}}}, f)
        os.makedirs(os.path.join(pkg_dir, "dist"), exist_ok=True)
        with open(os.path.join(pkg_dir, "dist", "index.d.ts"), "w") as f:
            f.write(
                "export type Result =\n"
                "  | { ok: true; data: string }\n"
                "  | {\n      ok: false;\n      error: string;\n    };\n\n"
                "export declare function parse(input: string): string;\n"
                "export declare function parse(input: Buffer): string;\n"
            )
        r_union = TypeSlicer.slice_package_type("modern-lib", "Result", project_root=self.tmp_dir.name)
        self.assertTrue(r_union["found"])
        self.assertIn("ok: false", r_union["type_code"])
        r_ov = TypeSlicer.slice_package_type("modern-lib", "parse", project_root=self.tmp_dir.name)
        self.assertTrue(r_ov["found"])
        self.assertIn("input: Buffer", r_ov["type_code"])

        # 2. Standalone @types/<pkg> without runtime package directory
        types_dir = os.path.join(self.tmp_dir.name, "node_modules", "@types", "onlytypes")
        os.makedirs(types_dir, exist_ok=True)
        with open(os.path.join(types_dir, "package.json"), "w") as f:
            json.dump({"name": "@types/onlytypes", "version": "1.0.0"}, f)
        with open(os.path.join(types_dir, "index.d.ts"), "w") as f:
            f.write("export interface OnlyType { flag: boolean; }\n")
        r_at = TypeSlicer.slice_package_type("onlytypes", "OnlyType", project_root=self.tmp_dir.name)
        self.assertTrue(r_at["found"])
        self.assertIn("flag: boolean", r_at["type_code"])


if __name__ == "__main__":
    unittest.main()
