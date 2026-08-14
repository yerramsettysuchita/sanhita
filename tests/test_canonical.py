"""Canonical JSON, hashing and signing.

If any of these fail, every certification signature in the product is void.
"""

from __future__ import annotations

import datetime as _dt

import pytest

from sanhita.ir.canonical import (
    CanonicalJSONError,
    canonical_bytes,
    canonical_json,
    sha256_hex,
    sign_hmac,
    verify_hmac,
)
from sanhita.ir.enums import Modality


def test_key_order_does_not_change_the_bytes():
    """The whole point: two equal objects built in different orders must agree."""
    first = {"b": 1, "a": 2, "c": {"z": 1, "y": 2}}
    second = {"c": {"y": 2, "z": 1}, "a": 2, "b": 1}
    assert canonical_json(first) == canonical_json(second)
    assert canonical_bytes(first) == canonical_bytes(second)
    assert sha256_hex(first) == sha256_hex(second)


def test_output_has_no_insignificant_whitespace():
    assert canonical_json({"a": 1, "b": [1, 2]}) == '{"a":1,"b":[1,2]}'


def test_non_ascii_is_emitted_literally():
    """Escaping would make the bytes differ from the regulation's own characters."""
    text = canonical_json({"clause": "clients’ funds"})
    assert "’" in text
    assert "\\u" not in text


def test_enums_serialise_as_their_value():
    assert canonical_json({"m": Modality.MUST}) == '{"m":"MUST"}'


def test_datetimes_normalise_to_utc_z():
    naive = _dt.datetime(2025, 6, 17, 10, 30)
    aware = _dt.datetime(
        2025, 6, 17, 16, 0, tzinfo=_dt.timezone(_dt.timedelta(hours=5, minutes=30))
    )
    assert canonical_json({"t": naive}) == '{"t":"2025-06-17T10:30:00Z"}'
    # The same instant expressed in IST must produce identical bytes.
    assert canonical_json({"t": aware}) == '{"t":"2025-06-17T10:30:00Z"}'


def test_dates_are_iso():
    assert canonical_json({"d": _dt.date(2011, 8, 22)}) == '{"d":"2011-08-22"}'


def test_sets_are_rejected():
    """A set has no deterministic order, so it cannot appear in signed bytes."""
    with pytest.raises(CanonicalJSONError):
        canonical_json({"a": {1, 2, 3}})


def test_non_finite_floats_are_rejected():
    with pytest.raises(CanonicalJSONError):
        canonical_json({"x": float("nan")})
    with pytest.raises(CanonicalJSONError):
        canonical_json({"x": float("inf")})


def test_unserialisable_types_raise_rather_than_guess():
    with pytest.raises(CanonicalJSONError):
        canonical_json({"x": object()})


def test_sha256_of_str_covers_the_characters_not_a_json_string():
    import hashlib

    text = "Stock brokers shall issue a daily margin statement."
    assert sha256_hex(text) == hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_hmac_round_trip_and_tamper_detection():
    payload = {"id": "SB-40.1.8-a", "modality": "MUST"}
    signature = sign_hmac(payload, "secret-key")

    assert verify_hmac(payload, "secret-key", signature)
    assert not verify_hmac({**payload, "modality": "MAY"}, "secret-key", signature)
    assert not verify_hmac(payload, "different-key", signature)


def test_signing_is_order_independent():
    a = sign_hmac({"x": 1, "y": 2}, "k")
    b = sign_hmac({"y": 2, "x": 1}, "k")
    assert a == b


def test_empty_signing_key_is_refused():
    with pytest.raises(ValueError):
        sign_hmac({"a": 1}, "")
