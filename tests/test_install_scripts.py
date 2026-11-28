"""Windows-safe runtime bootstrap for the shell installers.

A Windows user reported `curl -fsSL https://tokensafe.dev/install.sh | bash`
failing with "Failed to install required 'cryptography>=42.0' dependency" on a
machine that had no Python at all. The installer trusted `command -v python3`,
which on Windows resolves the Microsoft Store App Execution Alias: a placeholder
that prints "Python was not found" and exits 9009. Every pip attempt then failed
for the wrong reason and the user was told to fix a dependency they could not
install.

These tests pin the replacement contract:
  * the Store alias is never executed and never mistaken for an interpreter;
  * a rejected candidate never leaks output into a command substitution;
  * the venv fallback finds Scripts/python.exe on Windows and bin/python3
    elsewhere;
  * the block in tokensafe/install.sh and the one served as
    tokensafe.dev/install.sh stay byte-identical.

The block is exercised through bash with fake interpreters on PATH, so the
Windows branch is covered without a Windows runner.
"""

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REPO_INSTALLER = PROJECT_ROOT / "install.sh"
SITE_INSTALLER = PROJECT_ROOT.parent / "tokensafe-site" / "public" / "install.sh"
BLOCK_START = "# >>> bootstrap-python >>>"
BLOCK_END = "# <<< bootstrap-python <<<"
SHIM_START = "# >>> windows-cli-shim >>>"
SHIM_END = "# <<< windows-cli-shim <<<"

FAKE_PYTHON = """#!{python}
import os, shutil, sys

HERE = os.path.dirname(os.path.realpath(__file__))
args = sys.argv[1:]

if args[:1] == ["-c"]:
    code = args[1]
    if "version_info" in code:
        sys.exit(0)
    if "print(sys.executable)" in code:
        print(os.path.realpath(__file__))
        sys.exit(0)
    if "import cryptography" in code:
        present = os.environ.get("FAKE_CRYPTO") == "1" or os.path.exists(
            os.path.join(HERE, "crypto.present")
        )
        sys.exit(0 if present else 1)
    sys.exit(1)

if args[:2] == ["-m", "pip"]:
    if args[2:3] == ["--version"]:
        sys.exit(0)
    if args[2:3] == ["install"]:
        if "--user" in args:
            sys.exit(0 if os.environ.get("FAKE_USER_OK") == "1" else 1)
        if "--break-system-packages" in args:
            sys.exit(0 if os.environ.get("FAKE_BREAK_OK") == "1" else 1)
        if os.environ.get("FAKE_VENV") == "1":
            open(os.path.join(HERE, "crypto.present"), "w").close()
            sys.exit(0)
        sys.stderr.write("ERROR: No matching distribution found for cryptography\\n")
        sys.exit(1)
    sys.exit(1)

if args[:2] == ["-m", "venv"]:
    target = args[2]
    if os.environ.get("FAKE_LAYOUT") == "windows":
        dest = os.path.join(target, "Scripts", "python.exe")
    else:
        dest = os.path.join(target, "bin", "python3")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copyfile(os.path.realpath(__file__), dest)
    os.chmod(dest, 0o755)
    sys.exit(0)

sys.exit(1)
"""

STORE_ALIAS = """#!/bin/sh
# Microsoft Store App Execution Alias: not an interpreter.
: > "${STORE_ALIAS_MARKER:?}"
echo "Python was not found; run without arguments to install from the Microsoft Store." >&2
exit 9009
"""


def extract_block(path, start=BLOCK_START, end=BLOCK_END):
    text = path.read_text(encoding="utf-8")
    begin = text.index(start)
    stop = text.index(end) + len(end)
    return text[begin:stop]


def write_executable(path, content):
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class InstallerBootstrapTest(unittest.TestCase):
    def setUp(self):
        self.bash = shutil.which("bash")
        if not self.bash:
            self.skipTest("bash is not available")
        self.block = extract_block(REPO_INSTALLER)
        self.shim_block = extract_block(REPO_INSTALLER, SHIM_START, SHIM_END)
        self.tmp = Path(tempfile.mkdtemp(prefix="ts-install-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        # Deterministic npm probe: the shim only prefers npm's global prefix when
        # that directory is already on PATH, so tests must not inherit the host's
        # npm configuration.
        write_executable(self.bin / "npm", "#!/bin/sh\nexit 1\n")
        self.home = self.tmp / "home"
        self.home.mkdir()

    # -- helpers ---------------------------------------------------------

    def install_fake_python(self, name="python3"):
        write_executable(self.bin / name, FAKE_PYTHON.format(python=sys.executable))

    def install_store_alias(self, name="python3"):
        alias_dir = self.tmp / "WindowsApps"
        alias_dir.mkdir(exist_ok=True)
        write_executable(alias_dir / name, STORE_ALIAS)
        return alias_dir

    def run_block(self, body, extra_env=None, path_prefix=None, extra_path=()):
        lib = self.tmp / "lib.sh"
        lib.write_text(self.block + "\n" + self.shim_block + "\n", encoding="utf-8")
        script = "set -eu\nsource \"$1\"\n" + textwrap.dedent(body)
        env = {
            "HOME": str(self.home),
            "PATH": os.pathsep.join(
                [
                    str(path_prefix or self.bin),
                    *[str(item) for item in extra_path],
                    "/usr/bin",
                    "/bin",
                    "/usr/local/bin",
                ]
            ),
            "FAKE_LAYOUT": "posix",
            "FAKE_VENV": "1",
            "STORE_ALIAS_MARKER": str(self.tmp / "store-alias-ran"),
        }
        env.update(extra_env or {})
        return subprocess.run(
            [self.bash, "-c", script, "bash", str(lib)],
            cwd=str(self.tmp),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def uname_stub(self, value, directory=None):
        write_executable((directory or self.bin) / "uname", f"#!/bin/sh\necho {value}\n")

    # -- the reported failure ---------------------------------------------

    def test_store_alias_is_never_treated_as_python(self):
        """The exact Windows report: no interpreter, a Store placeholder in PATH."""
        alias_dir = self.install_store_alias("python3")
        self.install_store_alias("python")
        self.uname_stub("MINGW64_NT-10.0-26100", directory=alias_dir)

        result = self.run_block(
            """
            if ts_resolve_python; then
                echo "RESOLVED ${TS_PYTHON}"
            else
                ts_print_no_python_guidance
            fi
            """,
            path_prefix=alias_dir,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("No usable CPython 3.10+", result.stdout)
        self.assertIn("Microsoft Store placeholder", result.stdout)
        self.assertIn("winget install Python.Python.3.12", result.stdout)
        # The bogus verdict this report was about.
        self.assertNotIn("cryptography", result.stdout)
        # And the alias itself must never be launched: that opens the Store.
        self.assertFalse((self.tmp / "store-alias-ran").exists())

    def test_venv_fallback_uses_the_windows_layout(self):
        self.install_fake_python()
        self.uname_stub("MINGW64_NT-10.0-26100")

        result = self.run_block(
            """
            ts_resolve_python
            ts_bootstrap_dependencies "${TS_PYTHON}" "${HOME}/venv"
            printf 'CHOSEN=%s\\n' "${TS_PYTHON}"
            """,
            extra_env={"FAKE_LAYOUT": "windows"},
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Installing required dependency", result.stdout)
        self.assertIn("CHOSEN=", result.stdout)
        chosen = result.stdout.rsplit("CHOSEN=", 1)[1].strip()
        self.assertTrue(chosen.endswith("Scripts/python.exe"), chosen)
        self.assertTrue(Path(chosen).is_file())

    def test_venv_fallback_uses_the_posix_layout(self):
        self.install_fake_python()
        self.uname_stub("Darwin")

        result = self.run_block(
            """
            ts_resolve_python
            ts_bootstrap_dependencies "${TS_PYTHON}" "${HOME}/venv"
            printf 'CHOSEN=%s\\n' "${TS_PYTHON}"
            """
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        chosen = result.stdout.rsplit("CHOSEN=", 1)[1].strip()
        self.assertTrue(chosen.endswith("bin/python3"), chosen)

    def test_existing_cryptography_skips_pip(self):
        self.install_fake_python()
        self.uname_stub("Linux")

        result = self.run_block(
            """
            ts_resolve_python
            ts_bootstrap_dependencies "${TS_PYTHON}" "${HOME}/venv"
            printf 'CHOSEN=%s\\n' "${TS_PYTHON}"
            """,
            extra_env={"FAKE_CRYPTO": "1"},
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Installing required dependency", result.stdout)
        self.assertFalse((self.home / "venv").exists())
        chosen = result.stdout.rsplit("CHOSEN=", 1)[1].strip()
        self.assertEqual(chosen, str((self.bin / "python3").resolve()))

    def test_unusable_token_safe_python_pin_fails_loudly(self):
        broken = self.tmp / "not-python"
        write_executable(broken, "#!/bin/sh\nexit 2\n")

        result = self.run_block(
            """
            if ts_resolve_python; then echo "RESOLVED"; else echo "REJECTED"; fi
            """,
            extra_env={"TOKENSAFE_PYTHON": str(broken)},
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("REJECTED", result.stdout)
        self.assertIn("does not run CPython 3.10+", result.stdout)
        self.assertNotIn("RESOLVED", result.stdout)

    # -- the Windows command ------------------------------------------------

    def test_windows_shim_is_a_native_cmd_file_on_the_path(self):
        """`tokensafe` used to be a POSIX symlink: cmd.exe never saw a command."""
        local_bin = self.tmp / "local" / "bin"
        powershell_log = self.tmp / "powershell-path.txt"
        write_executable(
            self.bin / "cygpath",
            "#!/bin/sh\n"
            'printf "C:%s\\n" "$2" | tr "/" "\\\\"\n',
        )
        write_executable(
            self.bin / "powershell",
            f'#!/bin/sh\ncat >/dev/null\nprintf "%s\\n" "$TS_PATH_ADD" > "{powershell_log}"\n',
        )
        self.uname_stub("MINGW64_NT-10.0-26100")

        result = self.run_block(
            """
            ts_install_windows_cmd_shim /opt/py/python.exe /opt/ts/bin/tokensafe "$1"
            """.replace("$1", str(local_bin))
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("tokensafe.cmd", result.stdout)

        shim = local_bin / "tokensafe.cmd"
        raw = shim.read_bytes()
        self.assertIn(b"@echo off\r\n", raw)
        self.assertIn(b"%*", raw)
        text = raw.decode()
        self.assertIn(r'"C:\opt\py\python.exe"', text)
        self.assertIn(r'"C:\opt\ts\bin\tokensafe"', text)
        self.assertTrue((local_bin / "agy-tokens.cmd").is_file())
        self.assertTrue(powershell_log.is_file())
        self.assertIn("C:\\", powershell_log.read_text())

    def test_windows_shim_keeps_a_bare_command_name_untranslated(self):
        """The Node fallback passes `node`; cygpath must not turn it into a path."""
        write_executable(
            self.bin / "cygpath",
            "#!/bin/sh\n"
            'printf "C:%s\\n" "$2" | tr "/" "\\\\"\n',
        )
        write_executable(self.bin / "powershell", "#!/bin/sh\ncat >/dev/null\nexit 0\n")

        result = self.run_block(
            'ts_install_windows_cmd_shim node /opt/ts/bin/tokensafe.js "$1"'.replace(
                "$1", str(self.tmp / "local" / "bin")
            )
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        text = (self.tmp / "local" / "bin" / "tokensafe.cmd").read_text()
        self.assertIn('node "C:', text)
        self.assertNotIn("C:\\node", text)

    def test_windows_shim_reports_the_manual_step_without_powershell(self):
        result = self.run_block(
            'ts_install_windows_cmd_shim /opt/py/python.exe /opt/ts/bin/tokensafe "$1"'.replace(
                "$1", str(self.tmp / "local" / "bin")
            )
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("to PATH", result.stdout)
        # The current window gets an instant, copy-pasteable workaround: cmd only
        # knows the PATH it was started with.
        self.assertIn('set "PATH=%PATH%;', result.stdout)

    def test_windows_shim_lands_on_the_current_path_when_it_can(self):
        """No reopen: a directory already on PATH makes the shim work immediately."""
        prefix = self.tmp / "Roaming" / "npm"
        prefix.mkdir(parents=True)
        write_executable(
            self.bin / "npm",
            "#!/bin/sh\nprintf 'C:\\\\Users\\\\test\\\\Roaming\\\\npm\\n'\n",
        )
        write_executable(
            self.bin / "cygpath",
            "#!/bin/sh\n"
            'case "$1" in\n'
            '  -w) printf "C:%s\\n" "$2" | tr "/" "\\\\" ;;\n'
            '  -u) printf "%s\\n" "$FAKE_PREFIX" ;;\n'
            "esac\n",
        )

        result = self.run_block(
            'ts_install_windows_cmd_shim /opt/py/python.exe /opt/ts/bin/tokensafe "$1"'.replace(
                "$1", str(self.tmp / "local" / "bin")
            ),
            extra_env={"FAKE_PREFIX": str(prefix)},
            extra_path=[prefix],
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("works in this window already", result.stdout)
        self.assertTrue((prefix / "tokensafe.cmd").is_file())
        # Nothing to register and nothing to explain: the fallback stays untouched.
        self.assertFalse((self.tmp / "local" / "bin" / "tokensafe.cmd").exists())
        self.assertNotIn("new terminals", result.stdout)

    # -- the copies --------------------------------------------------------

    def test_both_installers_carry_the_same_block(self):
        repo_block = extract_block(REPO_INSTALLER)
        self.assertGreater(len(repo_block), 2000)
        if not SITE_INSTALLER.is_file():
            self.skipTest(f"sibling checkout not present: {SITE_INSTALLER}")
        for start, end in ((BLOCK_START, BLOCK_END), (SHIM_START, SHIM_END)):
            with self.subTest(block=start):
                self.assertEqual(
                    extract_block(REPO_INSTALLER, start, end),
                    extract_block(SITE_INSTALLER, start, end),
                )

    def test_site_installer_no_longer_blames_cryptography_blindly(self):
        if not SITE_INSTALLER.is_file():
            self.skipTest(f"sibling checkout not present: {SITE_INSTALLER}")
        text = SITE_INSTALLER.read_text(encoding="utf-8")
        self.assertNotIn("Failed to install required 'cryptography>=42.0'", text)
        self.assertNotIn("venv/bin/python3\" -m pip", text)


if __name__ == "__main__":
    unittest.main()
