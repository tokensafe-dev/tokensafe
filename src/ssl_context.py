"""Shared SSL context, created once on the importing (main) thread.

Short-lived hook processes exit while daemon threads (live-relay flush,
team-cache put) are still opening HTTPS. With urllib's default handler
each thread lazily builds its own SSLContext, and on Python 3.13 +
OpenSSL 3.x that lazy set_default_verify_paths can collide with
OPENSSL_cleanup running on the main thread during interpreter shutdown
(observed as SIGSEGV in pthread_rwlock_wrlock from EVP_PKEY_asn1_find_str
while the main thread is already in exit()). Sharing one pre-created
context removes the lazy per-thread store mutation entirely, and importing
this module also warms the OpenSSL store on the main thread before any
daemon thread starts.
"""

import ssl

_CONTEXT = ssl.create_default_context()


def shared_ssl_context() -> ssl.SSLContext:
    """Returns the process-wide SSLContext (safe to share across threads)."""
    return _CONTEXT
