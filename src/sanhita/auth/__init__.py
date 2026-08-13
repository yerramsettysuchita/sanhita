"""Accounts and sessions, on this machine.

Off by default. With no accounts created, the workbench behaves exactly as it
always has, which keeps a first run frictionless. The moment somebody signs up,
their documents carry their id, and setting ``SANHITA_REQUIRE_AUTH=1`` closes
the app to anyone not signed in.

The identity backend is one module. Swapping to a hosted provider means
implementing ``UserStore.authenticate`` and ``UserStore.create`` against it;
nothing else in the app knows where a user came from.
"""

from sanhita.auth.session import COOKIE_NAME, MAX_AGE_SECONDS, issue, read, session_key
from sanhita.auth.users import AuthError, User, UserStore, check_password_strength

__all__ = [
    "COOKIE_NAME",
    "MAX_AGE_SECONDS",
    "AuthError",
    "User",
    "UserStore",
    "check_password_strength",
    "issue",
    "read",
    "session_key",
]
