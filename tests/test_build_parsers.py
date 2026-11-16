import unittest
from src.build_parsers import BuildOutputParser
from src.command_sanitizer import CommandSanitizer


class TestBuildParsers(unittest.TestCase):

    def test_clean_progress_noise(self):
        raw = (
            "vite v6.4.5 building for production...\n"
            "transforming (1) src/main.tsx\rtransforming (7) node_modules/foo.js\r✓ 2018 modules transformed.\n"
            "rendering chunks (1)...\rrendering chunks (36)...\n"
            "computing gzip size (0)...\rcomputing gzip size (38)...\n"
            "dist/index.html 21.15 kB\n"
            "✓ built in 2.71s\n"
        )
        cleaned = BuildOutputParser.clean_progress_noise(raw)
        self.assertNotIn("rendering chunks (1)...", cleaned)
        self.assertNotIn("computing gzip size (0)...", cleaned)
        self.assertIn("✓ 2018 modules transformed.", cleaned)
        self.assertIn("✓ built in 2.71s", cleaned)

    def test_vite_build_success_compaction(self):
        user_raw = (
            "vite v6.4.5 building for production...\n"
            "transforming (1) src/main.tsxtransforming (7) node_modules/foo.js✓ 2018 modules transformed.\n"
            "rendering chunks (1)...rendering chunks (2)...\n"
            "computing gzip size (0)...computing gzip size (1)...\n"
            "dist/index.html                                                            21.15 kB │ gzip:   5.69 kB\n"
            "dist/assets/index-DSE8n-C2.css                                             68.59 kB │ gzip:  12.56 kB\n"
            "dist/assets/mail-Dxfxw3lz.js                                                0.38 kB │ gzip:   0.30 kB\n"
            "dist/assets/index-DT951L4I.js                                             557.86 kB │ gzip: 151.27 kB\n"
            "✓ built in 2.71s\n"
            "✓ Regenerated dist/llms.txt blog section (11 article(s))\n"
            "✓ Prerendered /blog + 11 article page(s), sitemap.xml (21 URLs), purge-urls.txt\n"
        )
        res, was_build = BuildOutputParser.compact(user_raw, exit_code=0)
        self.assertTrue(was_build)
        self.assertIn("✓ vite build succeeded", res)
        self.assertIn("2.71s", res)
        self.assertIn("2,018 modules transformed", res)
        self.assertIn("dist/index.html", res)
        self.assertIn("Prerendered /blog", res)

    def test_vite_build_failure_preserves_error(self):
        error_raw = (
            "vite v6.4.5 building for production...\n"
            "transforming (1)... ✓ 2018 modules transformed.\n"
            "[vite]: Rollup failed to resolve import \"missing-pkg\" from \"src/main.tsx\".\n"
            "error during build:\n"
            "Error: [vite]: Rollup failed to resolve import \"missing-pkg\" from \"src/main.tsx\".\n"
            "    at error (file:///.../rollup/dist/es/shared/parseAst.js:337:30)\n"
        )
        res, was_build = BuildOutputParser.compact(error_raw, exit_code=1)
        self.assertTrue(was_build)
        self.assertIn("Rollup failed to resolve import \"missing-pkg\"", res)
        self.assertIn("error during build:", res)
        self.assertNotIn("transforming (1)...", res)

    def test_next_build_compaction(self):
        next_raw = (
            "▲ Next.js 14.1.0\n"
            "   Creating an optimized production build ...\n"
            " ✓ Compiled successfully\n"
            "   Generating static pages (0/18) ...\n"
            "   Generating static pages (18/18)\n"
            "   Finalizing page optimization ...\n"
            "Route (app)                              Size     First Load JS\n"
            "┌ ○ /                                    5.32 kB        92.1 kB\n"
            "├ ○ /_not-found                          871 B          87.6 kB\n"
            "├ ● /blog/[slug]                         2.18 kB        88.9 kB\n"
            "└ λ /api/webhook                         0 B                0 B\n"
            "+ First Load JS shared by all            86.8 kB\n"
            "○  (Static)   prerendered as static content\n"
        )
        res, was_build = BuildOutputParser.compact(next_raw, exit_code=0)
        self.assertTrue(was_build)
        self.assertIn("✓ Next.js build succeeded", res)
        self.assertIn("static pages", res)
        self.assertIn("Shared JS bundle: 86.8 kB", res)

    def test_cargo_build_compaction(self):
        cargo_raw = (
            "   Compiling libc v0.2.153\n"
            "   Compiling proc-macro2 v1.0.81\n"
            "   Compiling unicode-ident v1.0.12\n"
            "   Compiling my-crate v0.1.0 (/path/to/my-crate)\n"
            "    Finished release [optimized] target(s) in 12.34s\n"
        )
        res, was_build = BuildOutputParser.compact(cargo_raw, exit_code=0)
        self.assertTrue(was_build)
        self.assertIn("✓ Finished release [optimized] target(s) in 12.34s (compiled 4 crates)", res)

    def test_command_sanitizer_build_rewriting(self):
        cmd = "npm --prefix /path/to/frontend run build"
        new_cmd, saved, reason = CommandSanitizer.sanitize(cmd)
        self.assertIsNotNone(new_cmd)
        self.assertTrue(new_cmd.startswith("tokensafe wrap "))
        self.assertIn("npm --prefix /path/to/frontend run build", new_cmd)
        self.assertGreaterEqual(saved, 1000)

        vite_cmd = "vite build"
        new_vite, _, _ = CommandSanitizer.sanitize(vite_cmd)
        self.assertIsNotNone(new_vite)
        self.assertTrue(new_vite.startswith("tokensafe wrap "))

        cargo_cmd = "cargo build --release"
        new_cargo, _, _ = CommandSanitizer.sanitize(cargo_cmd)
        self.assertIsNotNone(new_cargo)
        self.assertTrue(new_cargo.startswith("tokensafe wrap "))


if __name__ == "__main__":
    unittest.main()
