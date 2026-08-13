"""Accounts, stored on this machine.

Deliberately small and deliberately offline. Sanhita's whole claim is that it
runs in a room with no network, and an authentication step that phones a hosted
identity provider would quietly retract that.

Passwords are hashed with scrypt from the standard library, per user random
salt, and compared in constant time. There is no password reset by email,
because there is no email. An administrator with access to the machine resets a
password; anyone with that access could read the store anyway, so pretending
otherwise would be theatre.

What this is not: a multi-tenant identity system. It is the boundary that lets
one firm's compliance team keep their documents apart from each other on a
shared machine, and it is the seam a hosted provider would slot into later.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import json
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

__all__ = ["User", "UserStore", "AuthError", "check_password_strength"]

#: scrypt parameters. n=2**14 costs roughly 100ms per hash on a laptop, which is
#: slow enough to make offline guessing expensive and fast enough that nobody
#: notices signing in.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_KEY_LEN = 64

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

MIN_PASSWORD_LENGTH = 10


class AuthError(ValueError):
    """Something a person did wrong, phrased for that person."""


def check_password_strength(password: str) -> None:
    """Raise ``AuthError`` with a sentence, not a list of regex complaints."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AuthError(
            f"That password is {len(password)} characters. Use at least "
            f"{MIN_PASSWORD_LENGTH}. A short passphrase of a few words beats a "
            "short jumble of symbols."
        )
    if password.lower() in {"password12", "1234567890", "qwertyuiop", "sanhita123"}:
        raise AuthError("That is one of the first passwords anyone would try.")


def _hash(password: str, salt: bytes) -> str:
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_KEY_LEN,
        maxmem=64 * 1024 * 1024,
    )
    return derived.hex()


@dataclass
class User:
    id: str
    email: str
    name: str
    salt: str
    password_hash: str
    created_at: _dt.datetime

    def verify(self, password: str) -> bool:
        candidate = _hash(password, bytes.fromhex(self.salt))
        return hmac.compare_digest(candidate, self.password_hash)

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "email": self.email,
            "name": self.name,
            "salt": self.salt,
            "password_hash": self.password_hash,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_json(cls, raw: dict) -> User:
        return cls(
            id=raw["id"],
            email=raw["email"],
            name=raw["name"],
            salt=raw["salt"],
            password_hash=raw["password_hash"],
            created_at=_dt.datetime.fromisoformat(raw["created_at"]),
        )


class UserStore:
    """Accounts on disk, as JSON."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._users: dict[str, User] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        for raw in payload.get("users", []):
            user = User.from_json(raw)
            self._users[user.id] = user

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"users": [u.to_json() for u in self._users.values()]}
        self.path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        # The file holds password hashes. Narrow it where the platform allows.
        try:
            os.chmod(self.path, 0o600)
        except OSError:  # pragma: no cover - Windows and some filesystems
            pass

    # ------------------------------------------------------------------ read

    def __len__(self) -> int:
        return len(self._users)

    @property
    def any_users(self) -> bool:
        return bool(self._users)

    def get(self, user_id: str) -> User | None:
        return self._users.get(user_id)

    def by_email(self, email: str) -> User | None:
        target = email.strip().lower()
        for user in self._users.values():
            if user.email == target:
                return user
        return None

    # ----------------------------------------------------------------- write

    def create(self, *, email: str, name: str, password: str) -> User:
        address = email.strip().lower()
        if not _EMAIL.match(address):
            raise AuthError("That does not look like an email address.")
        if self.by_email(address):
            raise AuthError(
                "There is already an account with that email on this machine. "
                "Sign in instead."
            )
        if not name.strip():
            raise AuthError(
                "A name is required. It is what appears against every rule you "
                "certify, so it has to be a real one."
            )
        check_password_strength(password)

        salt = secrets.token_bytes(16)
        user = User(
            id=secrets.token_hex(8),
            email=address,
            name=name.strip()[:120],
            salt=salt.hex(),
            password_hash=_hash(password, salt),
            created_at=_dt.datetime.now(_dt.timezone.utc),
        )
        self._users[user.id] = user
        self._save()
        return user

    def authenticate(self, email: str, password: str) -> User:
        user = self.by_email(email)
        if user is None or not user.verify(password):
            # One message for both cases, so this cannot be used to find out
            # which addresses have accounts.
            raise AuthError("That email and password do not match an account.")
        return user
