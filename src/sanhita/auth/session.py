"""Signed session cookies, no dependency and no server-side session table.

The cookie carries the user id, an issue time and an HMAC over both. Nothing
secret is in it and nothing in it is trusted until the HMAC verifies.

**Key separation.** The session key is derived from ``SANHITA_SIGNING_KEY``
rather than being it. That key signs certifications, which are the artifacts the
whole product exists to protect; a session cookie must never be signed with the
same bytes, so that a weakness in one cannot be turned into forged signatures on
the other.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time

__all__ = ["COOKIE_NAME", "MAX_AGE_SECONDS", "issue", "read", "session_key"]

COOKIE_NAME = "sanhita_session"

#: Eight hours. A compliance officer's working day, after which they sign in
#: again. Long enough not to interrupt a review, short enough that an unattended
#: machine does not stay signed in overnight.
MAX_AGE_SECONDS = 8 * 60 * 60

_KEY_ENV = "SANHITA_SIGNING_KEY"


def session_key() -> bytes | None:
    """A key for cookies, derived from but never equal to the signing key."""
    root = os.environ.get(_KEY_ENV)
    if not root:
        return None
    return hmac.new(root.encode("utf-8"), b"sanhita.session.v1", hashlib.sha256).digest()


def _sign(payload: str, key: bytes) -> str:
    mac = hmac.new(key, payload.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode("ascii").rstrip("=")


def issue(user_id: str, *, now: float | None = None) -> str | None:
    """Build a cookie value, or None when no signing key is configured."""
    key = session_key()
    if key is None:
        return None
    issued = int(now if now is not None else time.time())
    payload = f"{user_id}.{issued}"
    return f"{payload}.{_sign(payload, key)}"


def read(cookie: str | None, *, now: float | None = None) -> str | None:
    """The user id this cookie proves, or None.

    Returns None for anything that does not verify: a bad signature, a
    malformed value, or one past its age. There is no partial trust.
    """
    key = session_key()
    if key is None or not cookie:
        return None

    parts = cookie.split(".")
    if len(parts) != 3:
        return None
    user_id, issued_raw, signature = parts

    payload = f"{user_id}.{issued_raw}"
    if not hmac.compare_digest(_sign(payload, key), signature):
        return None

    try:
        issued = int(issued_raw)
    except ValueError:
        return None

    moment = now if now is not None else time.time()
    if moment - issued > MAX_AGE_SECONDS or issued > moment + 60:
        return None
    return user_id
