"""The npm launcher must not carry a command list of its own.

`bin/tokensafe.js` used to print a hardcoded help screen that fell behind the
dispatcher in `src/cli.py`: `dev`, `inject`, `adapters` and `bench-oumv` were
missing, so `npx tokensafe help` advertised a subset of the real CLI while the
commands themselves worked. Help is forwarded to Python now, and these tests
pin both halves of that contract: one list, one owner.
"""

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = PROJECT_ROOT / "bin" / "tokensafe.js"
# Spot-check one runtime command family plus the measured benchmark: the four
# the stale list was missing.
RUNTIME_COMMANDS = (
    "dev [dir]",
    "inject [status|apply|revert]",
    "adapters [list]",
    "bench-oumv",
)


class LauncherHelpTest(unittest.TestCase):
    def test_launcher_no_longer_ships_its_own_help_screen(self):
        source = LAUNCHER.read_text(encoding="utf-8")
        self.assertNotIn("Usage:\n  tokensafe [command] [options]", source)
        self.assertIn("wantsHelp", source)
        self.assertIn("printHelpFallback", source)

    def test_python_help_lists_the_runtime_commands(self):
        from src.cli import _print_help

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _print_help()
        text = buf.getvalue()
        for needle in RUNTIME_COMMANDS:
            with self.subTest(needle=needle):
                self.assertIn(needle, text)

    @unittest.skipIf(shutil.which("node") is None, "node is not installed")
    @unittest.skipIf(sys.version_info < (3, 10), "the launcher requires CPython 3.10+")
    def test_launcher_forwards_help_to_the_python_cli(self):
        home = tempfile.mkdtemp(prefix="tokensafe-help-")
        env = dict(os.environ)
        env.update(
            TOKENSAFE_PYTHON=sys.executable,
            TOKENSAFE_HOME=home,
            # Help must never trigger the managed-runtime download.
            TOKENSAFE_NO_DOWNLOAD="1",
        )
        proc = subprocess.run(
            ["node", str(LAUNCHER), "--help"],
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
        try:
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("tokensafe <command>", proc.stdout)
            for needle in RUNTIME_COMMANDS:
                with self.subTest(needle=needle):
                    self.assertIn(needle, proc.stdout)
            self.assertNotIn("is not available yet", proc.stdout)
        finally:
            shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
