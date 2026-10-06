"""Constant-time comparison for API keys presented over HTTP."""

from __future__ import annotations

import hmac


def api_keys_match(provided: str, expected: str) -> bool:
    """Whether ``provided`` equals ``expected``, without leaking how much matched through timing.

    Compares UTF-8 bytes because ``hmac.compare_digest`` raises ``TypeError`` for a non-ASCII ``str``
    (header values arrive latin-1 decoded, so a client can send one). The key length can still leak,
    which is the documented behavior of ``compare_digest`` and acceptable for fixed-length random keys.
    """
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))
