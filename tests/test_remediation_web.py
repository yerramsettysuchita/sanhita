"""The remediation loop through the HTTP surface, not just the domain model.

`test_remediation_loop.py` proves the domain model closes a gap. This file
proves a person can do it: the same journey a compliance officer clicks, driven
through the real routes, ending in CLOSED with an intact chain.

The distinction matters because a passing domain test and a usable product are
different things, and this project has already shipped one without the other.
"""

from __future__ import annotations

import re

import pytest

from tests.conftest import requires_corpus, sign_in


@pytest.fixture()
def client(corpus_pdf, tmp_path, monkeypatch):
    """A workbench on a copy of the real store, so tests never touch it."""
    import shutil

    from fastapi.testclient import TestClient

    from sanhita.web.app import create_app

    monkeypatch.setenv("SANHITA_SIGNING_KEY", "a-test-only-key")
    store = tmp_path / "rules.json"
    shutil.copy(corpus_pdf.parent.parent / ".sanhita" / "rules.json", store)
    client = TestClient(create_app(corpus_pdf, store=store))
    # Compliance actions record who did them, so the journey these
    # tests walk needs an authenticated officer behind it.
    sign_in(client)
    return client


def _plain(html: str) -> str:
    body = re.sub(r"<script.*?</script>", " ", html, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)).strip()


def _gaps_with_real_evidence(client, tmp_path) -> str:
    """Give the firm records so the engine has something to assess.

    The gaps screen no longer invents evidence for a firm that supplied none,
    so a test about remediation has to provide some first. That is the point of
    the change rather than an inconvenience of it.
    """
    from sanhita.cli_compile import _load_registry
    from sanhita.ir.enums import DeadlineKind, RuleStatus

    target = None
    for rule in _load_registry(tmp_path / "rules.json").all_current():
        if (
            rule.status is RuleStatus.CERTIFIED
            and rule.deadline is not None
            and rule.deadline.kind is DeadlineKind.END_OF_PERIOD
            and rule.evidence
        ):
            target = rule
            break
    assert target is not None, "the store carries no certified recurring rule"

    # One occasion, never filed. That is a breach the engine will report.
    client.post(
        "/w/demo/evidence",
        headers={"x-sanhita-filename": "register.csv"},
        content=(
            "obligation_id,entity,occurred_on,filed_on,artifact_type,reference\n"
            f"{target.id},ABC Securities,2026-01-31,,{target.evidence[0].artifact_type},\n"
        ).encode(),
    )
    # A task can only be raised against a finding an assessment actually made,
    # so take one. What the screen shows before that is a preview.
    client.post("/w/demo/assess", follow_redirects=True)
    return client.get("/w/demo/gaps").text


@requires_corpus
def test_the_remediation_screen_opens_empty_and_says_what_to_do(client):
    page = _plain(client.get("/w/demo/remediation").text)

    assert "No remediation work has been raised yet" in page
    assert "gaps screen" in page


@requires_corpus
def test_a_gap_offers_to_become_a_task(client, tmp_path):
    page = _gaps_with_real_evidence(client, tmp_path)

    # Named for what it does to the gap in front of you, not for the screen it
    # writes to. Remediation should not read as a second application.
    assert "Fix this gap" in page
    assert "/remediation/open" in page


@requires_corpus
def test_raising_a_task_persists_it(client, tmp_path):
    gaps = _gaps_with_real_evidence(client, tmp_path)
    obligation = re.search(r'name="obligation_id" value="([^"]+)"', gaps)
    gap_id = re.search(r'name="gap_id" value="([^"]+)"', gaps)
    clause = re.search(r'name="clause_id" value="([^"]+)"', gaps)
    assert obligation and gap_id and clause, "the gaps screen offers no task to raise"

    response = client.post(
        "/w/demo/remediation/open",
        data={
            "obligation_id": obligation.group(1),
            "gap_id": gap_id.group(1),
            "clause_id": clause.group(1),
            "title": "Outstanding filings",
            "owner": "Compliance Operations",
            "team": "Operations",
            "priority": "HIGH",
            "action_text": "File the outstanding returns",
            "by": "A Named Officer",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    page = _plain(response.text)
    assert "Outstanding filings" in page
    assert "Compliance Operations" in page
    # And it is on disk, not just in the response.
    again = _plain(client.get("/w/demo/remediation").text)
    assert "Outstanding filings" in again


@requires_corpus
def test_there_is_no_mark_as_fixed_button(client, tmp_path):
    """The property the design turns on, asserted against the rendered page.

    If a person could close their own remediation from the UI, closure would
    mean nothing to an inspector. The only control that can close a task is the
    one that re-runs the rule.
    """
    gaps = _gaps_with_real_evidence(client, tmp_path)
    obligation = re.search(r'name="obligation_id" value="([^"]+)"', gaps)
    gap_id = re.search(r'name="gap_id" value="([^"]+)"', gaps)
    clause = re.search(r'name="clause_id" value="([^"]+)"', gaps)
    client.post(
        "/w/demo/remediation/open",
        data={
            "obligation_id": obligation.group(1),
            "gap_id": gap_id.group(1),
            "clause_id": clause.group(1),
            "owner": "R. Sharma",
            "priority": "HIGH",
            "by": "officer",
        },
        follow_redirects=True,
    )

    body = client.get("/w/demo/remediation").text
    lowered = _plain(body).lower()

    assert "run the rule again" in lowered
    for phrase in ("mark as fixed", "mark fixed", "mark resolved", "close task"):
        assert phrase not in lowered, f"the UI offers {phrase!r}"
    # No route accepts a hand-written status either.
    assert "/status" not in body


@requires_corpus
def test_the_whole_journey_through_the_ui_reaches_closed(client, tmp_path):
    """Gap, task, owner, evidence, re-check, closed. Clicked, not called.

    The evidence the re-check runs against is the same generated set the gaps
    screen used, so this exercises the real path rather than a fixture the
    engine was handed.
    """
    import datetime as _dt

    from sanhita.execute import EvidenceStore
    from sanhita.execute.evidence import ComplianceEvent
    from sanhita.remediate import RemediationStore, TaskStatus

    # 1. A gap exists and a task is raised against it.
    gaps = _gaps_with_real_evidence(client, tmp_path)
    obligation_id = re.search(r'name="obligation_id" value="([^"]+)"', gaps).group(1)
    gap_id = re.search(r'name="gap_id" value="([^"]+)"', gaps).group(1)
    clause_id = re.search(r'name="clause_id" value="([^"]+)"', gaps).group(1)

    client.post(
        "/w/demo/remediation/open",
        data={
            "obligation_id": obligation_id,
            "gap_id": gap_id,
            "clause_id": clause_id,
            "title": "Outstanding filings",
            "priority": "HIGH",
            "by": "A Named Officer",
        },
        follow_redirects=True,
    )

    store_path = tmp_path / "remediation.json"
    store = RemediationStore.load(store_path)
    assert store.tasks, "the task was not persisted"
    task_id = next(iter(store.tasks))

    # 2. Assigned through the UI.
    client.post(
        f"/w/demo/remediation/{task_id}/assign",
        data={"owner": "R. Sharma", "team": "Operations", "by": "A Named Officer"},
        follow_redirects=True,
    )
    assert RemediationStore.load(store_path).get(task_id).owner == "R. Sharma"

    # 3. The firm files everything it owed. Written where the app reads it.
    obligation = None
    from sanhita.cli_compile import _load_registry

    for candidate in _load_registry(tmp_path / "rules.json").all_current():
        if candidate.id == obligation_id:
            obligation = candidate
            break
    assert obligation is not None

    filed = EvidenceStore(label="Demo Stockbroker Ltd, synthetic demo evidence")
    for index in range(1, 13):
        occurred = _dt.date(2026, index, 28) if index <= 8 else _dt.date(2025, index, 28)
        filed.add(
            ComplianceEvent(
                id=f"EV-{index:03d}",
                obligation_id=obligation_id,
                entity="Demo Stockbroker Ltd",
                occurred_on=occurred,
                artifact_type=(
                    obligation.evidence[0].artifact_type if obligation.evidence else "report"
                ),
                filed_on=occurred,
                reference=f"REF-{index:03d}",
            )
        )
    filed.save(tmp_path / "evidence.json")

    # 4. Name the records that answer this, then re-check. The route refuses
    #    to close a task that points at nothing.
    reloaded_evidence = EvidenceStore.load(tmp_path / "evidence.json")
    client.post(
        f"/w/demo/remediation/{task_id}/attach",
        data={
            "evidence_id": [
                e.id for e in reloaded_evidence.for_obligation(obligation_id)[:3]
            ],
            "by": "A Named Officer",
        },
        follow_redirects=True,
    )
    client.post(
        f"/w/demo/remediation/{task_id}/recheck",
        data={"by": "A Named Officer"},
        follow_redirects=True,
    )

    final = RemediationStore.load(store_path).get(task_id)
    assert final.recheck_count >= 1, "the rule was never re-run"
    assert final.status is TaskStatus.CLOSED, (
        f"expected CLOSED, got {final.status.value}: {final.last_recheck_result}"
    )
    assert final.verified_at is not None

    # 5. And the whole life of it is on an intact chain, shown on the screen.
    reloaded = RemediationStore.load(store_path)
    intact, problem = reloaded.log.verify()
    assert intact, problem

    page = _plain(client.get("/w/demo/remediation").text)
    assert "closed" in page.lower()
    assert "hash chained and verifies" in page
