"""Static regression guard for the install CLI surface.

`tokensafe install --all` crashed with AttributeError at its tail for every
run since v4.2.0 because the retired interactive proxy prompt was still
referenced AFTER install_all had already asserted proxy default-on
(_ensure_proxy_default inside installer). The suite never executes the
interactive cli branch, so a source-level assertion is the guard."""
import os
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

RETIRED_INSTALLER_METHODS = ("maybe_prompt_proxy_enable",)


class TestCliInstallerCallSurface(unittest.TestCase):
    def test_cli_never_calls_retired_installer_methods(self):
        cli_path = os.path.join(PROJECT_ROOT, "src", "cli.py")
        with open(cli_path, encoding="utf-8") as f:
            cli_src = f.read()
        for name in RETIRED_INSTALLER_METHODS:
            self.assertNotIn(name, cli_src, f"cli.py references retired installer method {name!r}")

    def test_install_all_still_asserts_proxy_default(self):
        installer_path = os.path.join(PROJECT_ROOT, "src", "installer.py")
        with open(installer_path, encoding="utf-8") as f:
            installer_src = f.read()
        self.assertIn('_ensure_proxy_default("install")', installer_src)


if __name__ == "__main__":
    unittest.main()
