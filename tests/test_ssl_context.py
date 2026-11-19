"""Guards the shared SSL context that prevents the libcrypto shutdown segfault.

Hook processes exit while daemon threads (live-relay flush, team-cache put)
may still be opening HTTPS. On Python 3.13 + OpenSSL 3.x a lazily created
per-thread SSLContext can race OPENSSL_cleanup and SIGSEGV
(pthread_rwlock_wrlock via EVP_PKEY_asn1_find_str). The fix: one context
created at import time (main thread) and shared everywhere.
"""

import ssl
import threading
import unittest

from src.ssl_context import shared_ssl_context


class TestSharedSSLContext(unittest.TestCase):
    def test_returns_ssl_context_with_default_verification(self):
        ctx = shared_ssl_context()
        self.assertIsInstance(ctx, ssl.SSLContext)
        self.assertIn(ctx.verify_mode, (ssl.CERT_REQUIRED, ssl.CERT_OPTIONAL))

    def test_single_instance_across_threads(self):
        seen = []

        def grab():
            seen.append(shared_ssl_context())

        threads = [threading.Thread(target=grab) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(set(map(id, seen))), 1)
        self.assertIs(seen[0], shared_ssl_context())

    def test_relay_and_team_cache_share_the_context(self):
        # The daemon-thread HTTPS call sites must never lazily build their own
        # context: that lazy init is exactly what races interpreter shutdown.
        import inspect

        from src import live_relay, team_cache

        relay_src = inspect.getsource(live_relay)
        cache_src = inspect.getsource(team_cache)
        self.assertIn("shared_ssl_context()", relay_src)
        self.assertIn("shared_ssl_context()", cache_src)
        self.assertNotIn(
            "urllib.request.urlopen(req, timeout=4.0) as resp",
            relay_src,
            "live relay flush must pass the shared SSL context",
        )
        self.assertNotIn(
            "urllib.request.urlopen(req, timeout=1.5):",
            cache_src,
            "team cache put worker must pass the shared SSL context",
        )


if __name__ == "__main__":
    unittest.main()
