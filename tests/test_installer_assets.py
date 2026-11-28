"""Windows .exe installer assets (installer/tokensafe.iss + bootstrap.ps1).

The .exe is the only install path that needs neither Python, Node nor Git Bash,
and it cannot be exercised on macOS or in the Linux test job. What CI can do is
pin the invariants that keep it honest: the bootstrapper must verify the same
SHA-256 pin the npm launcher uses, the installer must stay per-user so it needs
no admin rights, and the workflow must run the bootstrapper before packaging.
"""

import json
import re
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
INSTALLER_DIR = PROJECT_ROOT / "installer"
ISS_PATH = INSTALLER_DIR / "tokensafe.iss"
BOOTSTRAP_PATH = INSTALLER_DIR / "bootstrap.ps1"
WORKFLOW_PATH = PROJECT_ROOT / ".github" / "workflows" / "installer.yml"
MANIFEST_PATH = PROJECT_ROOT / "bin" / "python-runtime.json"


class WindowsInstallerAssetsTest(unittest.TestCase):
    def test_assets_exist(self):
        for path in (ISS_PATH, BOOTSTRAP_PATH, WORKFLOW_PATH):
            with self.subTest(path=path.name):
                self.assertTrue(path.is_file(), f"missing {path}")

    def test_installer_is_per_user_and_on_the_path(self):
        source = ISS_PATH.read_text(encoding="utf-8")
        # Elevation would make the PATH edit machine-wide and the login prompt
        # impossible to skip in managed environments.
        self.assertIn("PrivilegesRequired=lowest", source)
        # Append-only HKCU PATH entry, preserved as expandsz so %USERPROFILE%
        # keeps expanding, and removed again on uninstall.
        self.assertIn("ChangesEnvironment=yes", source)
        self.assertIn('Root: HKCU; Subkey: "Environment"; ValueType: expandsz', source)
        self.assertIn("NeedsAddPath", source)
        self.assertIn("CurUninstallStepChanged", source)

    def test_installer_runs_the_bootstrapper(self):
        source = ISS_PATH.read_text(encoding="utf-8")
        self.assertIn("bootstrap.ps1", source)
        # Provisioning is unattended and must stay that way: the interactive
        # agent wiring runs from the Finish page, never inside a hidden process
        # that can block on a prompt (a silent install hung exactly like that).
        self.assertIn("-Mode Provision", source)
        self.assertNotIn("-Mode Setup", source)
        self.assertIn("setup --all", source)
        # A failed bootstrap must surface the log path, not a silent no-op.
        self.assertIn("install.log", source)
        # And it must never open a dialog in a silent install: a scripted MsgBox
        # is not covered by /SUPPRESSMSGBOXES and blocks the process forever.
        self.assertIn("if WizardSilent then", source)

    def test_bootstrap_verifies_the_same_pin_as_the_launcher(self):
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        target = manifest["targets"]["win32-x64"]
        source = BOOTSTRAP_PATH.read_text(encoding="utf-8")

        # The digest comes from the manifest at runtime - no second copy to rot.
        self.assertIn("bin\\python-runtime.json", source)
        self.assertIn("Get-FileHash", source)
        self.assertIn("SHA256", source)
        # The download must be verified before it is extracted.
        self.assertLess(source.index("Get-FileHash"), source.index("'-xzf'"))
        # And it must land in the layout the npm launcher recognises.
        self.assertIn("'python\\python.exe'", source)
        self.assertIn(".complete", source)
        self.assertTrue(target["asset"].endswith("-install_only.tar.gz"))

    def test_bootstrap_never_trusts_the_store_alias(self):
        source = BOOTSTRAP_PATH.read_text(encoding="utf-8")
        self.assertIn("WindowsApps", source)
        self.assertIn("TOKENSAFE_FORCE_MANAGED_RUNTIME", source)
        self.assertIn("tokensafe.cmd", source)

    def test_workflow_smoke_tests_before_packaging_and_signs_when_configured(self):
        source = WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertIn("bootstrap.ps1", source)
        self.assertIn("ISCC.exe", source)
        self.assertLess(
            source.index("Smoke-test the bootstrapper"), source.index("Build the installer")
        )
        self.assertIn("trusted-signing-action", source)
        # Signing must be conditional: the repository has to build without the
        # Azure secrets, otherwise nobody can fork or reproduce a build.
        self.assertIn("env.HAS_SIGNING", source)
        # The artifact must reach the tag's release.
        self.assertIn("gh release upload", source)
        self.assertRegex(source, r"tags:\s*\n\s*- 'v\*'")

    def test_runtime_manifest_is_what_the_bootstrapper_reads(self):
        source = BOOTSTRAP_PATH.read_text(encoding="utf-8")
        self.assertRegex(source, r"targets\.'win32-x64'")
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        self.assertIn("win32-x64", manifest["targets"])


if __name__ == "__main__":
    unittest.main()
