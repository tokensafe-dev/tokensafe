"""The paid matrix must not die on one flaky provider read.

Matrix v5's first attempt spent ~$0.14 and lost every row: `urllib` raised
TimeoutError while reading a long generation, and the artifact was only written
once at the very end. Two regressions are pinned here — the client retries
transient failures, and every finished session is persisted immediately.
"""

import json
import os
import tempfile
import unittest
from unittest import mock

from src.bench import driver, runner


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _ok_payload(content="ok"):
    return {
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1},
    }


class TestProviderRetry(unittest.TestCase):
    def _client(self, retries=3):
        with mock.patch.object(driver, "resolve_api_key", return_value="test-key"):
            client = driver.DeepSeekClient(model="deepseek-chat", retries=retries)
        client._backoff = lambda attempt: 0  # no real sleeping in tests
        return client

    def test_timeout_is_retried_then_succeeds(self):
        client = self._client()
        attempts = {"n": 0}

        def fake_urlopen(req, timeout=None):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise TimeoutError("The read operation timed out")
            return _Resp(_ok_payload())

        with mock.patch.object(driver.urllib.request, "urlopen", fake_urlopen):
            message, usage = client.chat([{"role": "user", "content": "hi"}])

        self.assertEqual(message["content"], "ok")
        self.assertEqual(usage["prompt_tokens"], 3)
        self.assertEqual(attempts["n"], 3)

    def test_truncated_body_is_retried(self):
        client = self._client()
        attempts = {"n": 0}

        class _Truncated(_Resp):
            def read(self):
                return b'{"choices": ['

        def fake(req, timeout=None):
            attempts["n"] += 1
            return _Truncated({}) if attempts["n"] == 1 else _Resp(_ok_payload())

        with mock.patch.object(driver.urllib.request, "urlopen", fake):
            message, _usage = client.chat([])
        self.assertEqual(message["content"], "ok")
        self.assertEqual(attempts["n"], 2)

    def test_gives_up_after_the_retry_budget(self):
        client = self._client(retries=2)
        with mock.patch.object(driver.urllib.request, "urlopen", side_effect=TimeoutError("nope")):
            with self.assertRaises(RuntimeError) as ctx:
                client.chat([])
        self.assertIn("2 attempts", str(ctx.exception))

    def test_http_429_is_retried_but_401_is_not(self):
        client = self._client()

        def make_exc(code):
            return driver.urllib.error.HTTPError("u", code, "err", {}, mock.Mock(read=lambda: b"detail"))

        calls = {"n": 0}

        def fake(req, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise make_exc(429)
            return _Resp(_ok_payload())

        with mock.patch.object(driver.urllib.request, "urlopen", fake):
            message, _ = client.chat([])
        self.assertEqual(message["content"], "ok")
        self.assertEqual(calls["n"], 2)

        with mock.patch.object(driver.urllib.request, "urlopen", side_effect=make_exc(401)):
            with self.assertRaises(RuntimeError):
                client.chat([])


class TestMatrixCheckpoint(unittest.TestCase):
    def test_every_finished_row_is_persisted(self):
        tmp = tempfile.mkdtemp(prefix="ts-bench-ckpt-")
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        old = os.environ.get("TOKENSAFE_DATA_DIR")
        os.environ["TOKENSAFE_DATA_DIR"] = tmp
        self.addCleanup(lambda: os.environ.__setitem__("TOKENSAFE_DATA_DIR", old) if old is not None
                        else os.environ.pop("TOKENSAFE_DATA_DIR", None))

        row = {"arm": "baseline", "task": "t", "solved": True,
               "usage": {"input_tokens": 10, "output_tokens": 2, "usd": 0.001}, "steps": []}
        with mock.patch.object(runner, "run_one", return_value=dict(row)), \
             mock.patch.object(runner, "_provenance", return_value={}), \
             mock.patch.object(runner, "checkpoint_path",
                               lambda files=0: __import__("pathlib").Path(tmp) / "go-checkpoint.json"):
            report = runner.run_matrix(["t"], ["baseline"], runs=1, budget_usd=1.0)
            path = runner.checkpoint_path(0)

        self.assertTrue(path.exists(), "checkpoint must exist after the run")
        saved = json.loads(path.read_text())
        self.assertTrue(saved.get("partial"))
        self.assertEqual(len(saved["results"]), 1)
        self.assertEqual(len(report["results"]), 1)


if __name__ == "__main__":
    unittest.main()
