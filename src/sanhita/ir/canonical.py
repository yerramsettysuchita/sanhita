"""Byte-stable serialisation, hashing and signing.

A certification signature is worthless if the bytes it covers can shift between
machines, Python builds or dict insertion orders. Everything a signature or a
hash ever covers goes through `canonical_bytes` here, and nothing else.

Canonical form, fixed by this module:
  - UTF-8, no BOM
  - object keys sorted by Unicode code point
  - no insignificant whitespace: separators are ``(",", ":")``
  - non-ASCII characters emitted literally, never \\u-escaped, so the bytes
    match the source regulation's own characters
  - floats rejected unless finite, and serialised via `repr` round-tripping
  - datetimes serialised as RFC 3339 in UTC with a trailing ``Z``
  - enums serialised as their ``.value``
  - sets rejected outright: they have no deterministic order

Deliberately absent: any dependency on Pydantic's own JSON encoder. Pydantic's
output ordering follows field declaration order, which is a schema-authoring
detail and not a stable wire contract.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import json
import math
from decimal import Decimal
from enum import Enum
from typing import Any


class CanonicalJSONError(TypeError):
    """Raised when a value has no deterministic canonical representation."""


def _plain(value: Any) -> Any:
    """Reduce an arbitrary value to JSON primitives, deterministically.

    Raises rather than guessing. A silent fallback such as ``str(value)`` would
    let an unserialisable field slip into a signed payload and change meaning
    between releases.
    """
    if value is None or isinstance(value, (str, bool)):
        return value

    if isinstance(value, Enum):
        return _plain(value.value)

    if isinstance(value, int):
        # bool is handled above; int is exact and needs no normalisation.
        return value

    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise CanonicalJSONError(f"non-finite float is not canonicalisable: {value!r}")
        # Python's repr is the shortest round-tripping decimal form and is
        # stable across platforms for IEEE-754 doubles.
        return value

    if isinstance(value, Decimal):
        if not value.is_finite():
            raise CanonicalJSONError(f"non-finite Decimal is not canonicalisable: {value!r}")
        return str(value)

    if isinstance(value, _dt.datetime):
        return _iso_datetime(value)

    if isinstance(value, _dt.date):
        return value.isoformat()

    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalJSONError(f"object keys must be str, got {type(key).__name__}")
            out[key] = _plain(item)
        return out

    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]

    if isinstance(value, (set, frozenset)):
        raise CanonicalJSONError(
            "sets have no deterministic order; use a sorted list at the model boundary"
        )

    # Pydantic models and anything else exposing a mapping dump.
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return _plain(dump(mode="python"))

    raise CanonicalJSONError(f"no canonical form for {type(value).__name__}")


def _iso_datetime(value: _dt.datetime) -> str:
    """RFC 3339 in UTC with a ``Z`` suffix, microseconds preserved.

    Naive datetimes are treated as UTC. Accepting them silently would be worse:
    it would let two machines in different zones sign different bytes for what
    the author intended as the same instant.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=_dt.timezone.utc)
    value = value.astimezone(_dt.timezone.utc)
    text = value.isoformat()
    return text.replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    """Return the canonical JSON text for `value`."""
    return json.dumps(
        _plain(value),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
        indent=None,
    )


def canonical_bytes(value: Any) -> bytes:
    """Return the canonical UTF-8 bytes for `value`. Hash and sign only these."""
    return canonical_json(value).encode("utf-8")


def sha256_hex(value: Any) -> str:
    """SHA-256 of the canonical bytes of `value`.

    `str` and `bytes` are hashed as-is rather than as JSON documents, because
    clause provenance hashes must cover the regulation's own characters and not
    a quoted JSON string of them.
    """
    if isinstance(value, bytes):
        payload = value
    elif isinstance(value, str):
        payload = value.encode("utf-8")
    else:
        payload = canonical_bytes(value)
    return hashlib.sha256(payload).hexdigest()


def sign_hmac(value: Any, key: bytes | str) -> str:
    """HMAC-SHA256 over the canonical bytes of `value`, hex encoded."""
    if isinstance(key, str):
        key = key.encode("utf-8")
    if not key:
        raise ValueError("signing key must not be empty")
    return hmac.new(key, canonical_bytes(value), hashlib.sha256).hexdigest()


def verify_hmac(value: Any, key: bytes | str, signature: str) -> bool:
    """Constant-time verification of an HMAC produced by `sign_hmac`."""
    expected = sign_hmac(value, key)
    return hmac.compare_digest(expected, signature)
