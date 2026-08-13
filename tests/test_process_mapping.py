"""Clause to process to team to system to control to evidence to check.

Problem Statement 2 asks for a requirement to be mapped to the affected
intermediary's operational processes. The product knew a rule bound a stock
broker, which is a category of firm rather than a part of one, and separately
knew a team and a system through the control binding. Nothing joined the two,
so a supervisor could see a rule fail without seeing what had failed to do it.

The binding was extended rather than duplicated, so the 183 signatures are
untouched. These tests hold that property as much as they hold the feature.
"""

from __future__ import annotations

import re

import pytest

from tests.conftest import requires_corpus

from sanhita.controls import ControlStore


def _store(tmp_path) -> ControlStore:
    return ControlStore.load(tmp_path / "controls.json")


def test_the_chain_reads_in_order_and_skips_what_is_unset(tmp_path):
    store = _store(tmp_path)
    binding = store.bind(
        "SB-114.2-a",
        process="Daily margin reporting",
        function="Operations",
        system="Margin engine",
        control_ref="SOP-12",
    )

    assert binding.chain() == [
        ("process", "Daily margin reporting"),
        ("function", "Operations"),
        ("system", "Margin engine"),
        ("control", "SOP-12"),
    ]
    assert binding.is_complete


def test_a_partial_chain_is_not_complete(tmp_path):
    """Bound to a team and nothing else does not tell anybody what to fix."""
    store = _store(tmp_path)
    binding = store.bind("SB-1-a", function="Operations")

    assert not binding.is_complete
    assert binding.chain() == [("function", "Operations")]


def test_coverage_separates_bound_from_actually_mapped(tmp_path):
    store = _store(tmp_path)
    store.bind("SB-1-a", function="Operations")  # partial
    store.bind(
        "SB-2-a",
        process="Client onboarding",
        function="KYC",
        system="CRM",
        control_ref="SOP-4",
    )

    coverage = store.coverage(["SB-1-a", "SB-2-a", "SB-3-a"])

    assert coverage["bound"] == 2
    assert coverage["mapped"] == 1, "only one chain reaches the control"
    assert coverage["unbound"] == 1


def test_unmapped_bindings_are_grouped_visibly_not_dropped(tmp_path):
    """A rule bound to a team but no process must still appear somewhere."""
    store = _store(tmp_path)
    store.bind("SB-1-a", function="Operations")
    store.bind("SB-2-a", process="Margin reporting", function="Operations")

    grouped = store.by_process()

    assert "Margin reporting" in grouped
    assert "" in grouped, "the unnamed process bucket must exist"
    assert len(grouped[""]) == 1


def test_a_binding_written_before_processes_existed_still_loads(tmp_path):
    """Old sidecars have no process key. Reading one must not raise."""
    import json

    path = tmp_path / "controls.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "bindings": [
                    {
                        "obligation_id": "SB-1-a",
                        "function": "Operations",
                        "system": "Margin engine",
                        "control_ref": "SOP-12",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    store = ControlStore.load(path)
    binding = store.get("SB-1-a")

    assert binding is not None
    assert binding.process == ""
    assert not binding.is_complete


def test_systems_are_counted_so_a_single_point_of_failure_shows(tmp_path):
    store = _store(tmp_path)
    for index in range(3):
        store.bind(f"SB-{index}-a", function="Operations", system="Margin engine")
    store.bind("SB-9-a", function="KYC", system="CRM")

    assert store.systems() == {"Margin engine": 3, "CRM": 1}


def test_adding_a_process_does_not_touch_the_signed_payload():
    """The reason this is a sidecar and not a field on Obligation."""
    import datetime as _dt
    import hashlib

    from sanhita.ir.enums import Actor, DeadlineKind, Modality, RuleStatus, TriggerKind
    from sanhita.ir.schema import (
        Action,
        Certification,
        Deadline,
        EvidenceReq,
        Obligation,
        SourceAnchor,
        Trigger,
    )

    text = "Clause 114.2: the broker shall issue the statement."
    rule = Obligation(
        id="SB-114.2-a",
        actor=Actor.STOCK_BROKER,
        modality=Modality.MUST,
        action=Action(verb="issue", object="the margin statement"),
        trigger=Trigger(
            kind=TriggerKind.SCHEDULE, expression="daily", recurrence="FREQ=DAILY"
        ),
        deadline=Deadline(kind=DeadlineKind.END_OF_PERIOD, period="DAY"),
        evidence=[EvidenceReq(artifact_type="dispatch log")],
        source=SourceAnchor(
            circular_id="SB-2025-06-17",
            section="114",
            clause_id="114.2",
            page=95,
            char_span=(0, len(text)),
            verbatim_text=text,
            sha256=hashlib.sha256(text.encode()).hexdigest(),
        ),
        confidence=0.9,
        status=RuleStatus.CERTIFIED,
        certification=Certification(
            certified_by="A Named Officer",
            certified_at=_dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc),
            signature="f" * 64,
        ),
    )
    before = rule.model_dump_json()

    store = ControlStore(path=__import__("pathlib").Path("unused.json"))
    store.bind(
        rule.id,
        process="Daily margin reporting",
        function="Operations",
        system="Margin engine",
        control_ref="SOP-12",
    )

    assert rule.model_dump_json() == before
    assert "Daily margin reporting" not in before


# ═════════════════════════════════════════════════════════ the screen ══


@requires_corpus
def test_the_mapping_screen_renders(corpus_pdf):
    from fastapi.testclient import TestClient

    from sanhita.web.app import create_app

    response = TestClient(create_app(corpus_pdf)).get("/w/demo/processes")

    assert response.status_code == 200
    assert "clause" in response.text.lower()


@requires_corpus
def test_the_screen_says_when_nothing_is_mapped_rather_than_looking_finished(
    corpus_pdf, tmp_path
):
    import shutil

    from fastapi.testclient import TestClient

    from sanhita.web.app import create_app

    store = tmp_path / "rules.json"
    shutil.copy(corpus_pdf.parent.parent / ".sanhita" / "rules.json", store)
    body = TestClient(create_app(corpus_pdf, store=store)).get("/w/demo/processes").text
    plain = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))

    assert "No rule has been mapped yet" in plain
    assert "Owned inside the firm by" in plain, "it must say where to do the mapping"
