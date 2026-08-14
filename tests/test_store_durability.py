"""The store must never be left half written.

The rules file holds the audit ledger. Every certification rewrites the whole
file, so an in-place write that truncates first is a way to lose the one
artifact the product exists to protect. A crash between the truncate and the
write leaves nothing.

These tests assert the write is all-or-nothing.
"""

from __future__ import annotations

import json

import pytest

from sanhita.cli_compile import _write_atomically


def test_a_completed_write_lands(tmp_path):
    target = tmp_path / "rules.json"
    _write_atomically(target, '{"ok": true}')
    assert json.loads(target.read_text(encoding="utf-8")) == {"ok": True}


def test_a_failed_write_leaves_the_previous_file_intact(tmp_path, monkeypatch):
    """The case that matters: the process dies mid-save."""
    target = tmp_path / "rules.json"
    _write_atomically(target, '{"generation": 1}')

    import os as _os

    def die(*args, **kwargs):
        raise KeyboardInterrupt("power cut")

    monkeypatch.setattr(_os, "replace", die)

    with pytest.raises(KeyboardInterrupt):
        _write_atomically(target, '{"generation": 2}')

    # The old file is still whole and readable, not truncated to nothing.
    assert json.loads(target.read_text(encoding="utf-8")) == {"generation": 1}


def test_no_temp_file_is_left_behind(tmp_path, monkeypatch):
    target = tmp_path / "rules.json"
    _write_atomically(target, '{"generation": 1}')

    import os as _os

    monkeypatch.setattr(_os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
    with pytest.raises(OSError):
        _write_atomically(target, '{"generation": 2}')

    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "rules.json"]
    assert leftovers == [], f"left rubbish behind: {leftovers}"


def test_it_creates_the_directory_if_it_is_missing(tmp_path):
    target = tmp_path / "nested" / "deeper" / "rules.json"
    _write_atomically(target, '{"ok": true}')
    assert target.is_file()


def test_a_large_payload_round_trips(tmp_path):
    """6 MB is the real store size, and it is written on every certification."""
    target = tmp_path / "rules.json"
    payload = json.dumps({"rules": [{"id": f"SB-{i}", "text": "x" * 200} for i in range(5000)]})
    _write_atomically(target, payload)
    assert json.loads(target.read_text(encoding="utf-8")) == json.loads(payload)


def test_the_certification_store_survives_a_save(tmp_path):
    """End to end: a registry written through the real save path reloads."""
    from sanhita.certify.lifecycle import RuleRegistry
    from sanhita.cli_compile import _load_registry, _save_registry

    store = tmp_path / "rules.json"
    _save_registry(RuleRegistry(), circular_id="TEST", fingerprint="f" * 64, path=store)
    assert _load_registry(store) is not None
    assert json.loads(store.read_text(encoding="utf-8"))["circular_id"] == "TEST"
