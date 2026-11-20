"""Unit tests for the File-State Tracker (stale-edit guard state store).

Tracked project files live in a SEPARATE directory from TOKENSAFE_DATA_DIR:
the state store deliberately skips everything under the TokenSafe data root
(self-edits would poison the map) — tests mirror the production layout.
"""
import os
import sys
import shutil
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src.file_state import (
    STATE_FILENAME,
    _MAX_ENTRIES,
    _load_state,
    check_stale,
    guard_enabled,
    record_seen,
)


class TestFileState(unittest.TestCase):
    def setUp(self):
        self._data = tempfile.mkdtemp(prefix="tokensafe_filestate_data_")
        self._proj = tempfile.mkdtemp(prefix="tokensafe_filestate_proj_")
        os.environ["TOKENSAFE_DATA_DIR"] = self._data
        os.environ["TOKENSAFE_TELEMETRY_DISABLED"] = "1"
        self.addCleanup(shutil.rmtree, self._data, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self._proj, ignore_errors=True)
        self.addCleanup(os.environ.pop, "TOKENSAFE_DATA_DIR", None)
        self.addCleanup(os.environ.pop, "TOKENSAFE_TELEMETRY_DISABLED", None)

    def _write(self, rel: str, content: str) -> str:
        p = os.path.join(self._proj, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        return p

    def test_record_and_fresh_check(self):
        p = self._write("app.py", "print(1)")
        self.assertTrue(record_seen(p))
        self.assertIsNone(check_stale(p))

    def test_stale_detected_after_external_change(self):
        p = self._write("app.py", "print(1)")
        record_seen(p)
        self._write("app.py", "print(2)  # changed externally")
        stale = check_stale(p)
        self.assertIsNotNone(stale)
        self.assertIn("size_now", stale)
        self.assertIn("age_s", stale)

    def test_never_seen_returns_none(self):
        p = self._write("app.py", "print(1)")
        self.assertIsNone(check_stale(p))

    def test_missing_file_returns_none_and_drops_entry(self):
        p = self._write("app.py", "print(1)")
        record_seen(p)
        os.unlink(p)
        self.assertFalse(record_seen(p))
        self.assertNotIn(os.path.abspath(p), _load_state())
        self.assertIsNone(check_stale(p))

    def test_binary_suffix_skipped(self):
        p = self._write("assets/logo.png", "fake png bytes")
        self.assertFalse(record_seen(p))
        self.assertIsNone(check_stale(p))

    def test_node_modules_skipped(self):
        p = self._write("node_modules/pkg/index.js", "module.exports = 1;")
        self.assertFalse(record_seen(p))

    def test_lockfile_suffix_skipped(self):
        p = self._write("bun.lock", "{}")
        self.assertFalse(record_seen(p))

    def test_data_dir_files_skipped(self):
        state_file = os.path.join(self._data, STATE_FILENAME)
        with open(state_file, "w", encoding="utf-8") as f:
            f.write("{}")
        self.assertFalse(record_seen(state_file))

    def test_lru_cap(self):
        for i in range(_MAX_ENTRIES + 10):
            p = self._write(f"mod_{i}.py", f"value = {i}")
            record_seen(p)
        self.assertLessEqual(len(_load_state()), _MAX_ENTRIES)

    def test_guard_kill_switch(self):
        os.environ["TOKENSAFE_STALE_GUARD"] = "0"
        try:
            self.assertFalse(guard_enabled())
        finally:
            os.environ.pop("TOKENSAFE_STALE_GUARD", None)
        self.assertTrue(guard_enabled())

    def test_guard_default_on(self):
        os.environ.pop("TOKENSAFE_STALE_GUARD", None)
        self.assertTrue(guard_enabled())


if __name__ == "__main__":
    unittest.main()
