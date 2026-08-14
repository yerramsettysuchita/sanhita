"""A remediation task has to point at the artifact that answers it.

"Corrected evidence was filed" is an assertion. A task that names the event ids,
and through them the document, page and row those events came from, is a record
an inspector can follow back to a piece of paper. The audit called this out as
the missing link between the remediation loop and the evidence store.

Attaching still closes nothing. The re-check has to agree.
"""

from __future__ import annotations

import re

import pytest

from tests.conftest import complete_setup, requires_corpus, sign_in


@pytest.fixture()
def client(corpus_pdf, tmp_path, monkeypatch):
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
    # And a firm that has finished onboarding. The routes now hold the same
    # order the screens walk a visitor through, so a POST to /assess before
    # setup is complete is refused rather than silently accepted.
    complete_setup(client)
    return client


def _plain(html: str) -> str:
    body = re.sub(r"<script.*?</script>", " ", html, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)).strip()


def _recurring_rule(tmp_path):
    from sanhita.cli_compile import _load_registry
    from sanhita.ir.enums import DeadlineKind, RuleStatus

    for rule in _load_registry(tmp_path / "rules.json").all_current():
        if (
            rule.status is RuleStatus.CERTIFIED
            and rule.deadline is not None
            and rule.deadline.kind is DeadlineKind.END_OF_PERIOD
            and rule.evidence
        ):
            return rule
    raise AssertionError("the store carries no certified recurring rule")


def _task_against_a_real_gap(client, tmp_path) -> tuple[str, str]:
    """Import one unfiled occasion, raise a task on the gap it produces."""
    from sanhita.remediate import RemediationStore

    rule = _recurring_rule(tmp_path)
    client.post(
        "/w/demo/evidence",
        headers={"x-sanhita-filename": "register.csv"},
        content=(
            "obligation_id,entity,occurred_on,filed_on,artifact_type,reference\n"
            f"{rule.id},ABC Securities,2026-01-31,,{rule.evidence[0].artifact_type},\n"
        ).encode(),
    )
    # The gap raised has to be the one on the rule we filed against, not simply
    # the worst gap on the page. The worst is normally a rule with no records at
    # all, and a task on that has nothing it could point at.
    client.post("/w/demo/assess", follow_redirects=True)
    gaps = client.get("/w/demo/gaps").text
    form = re.search(
        rf'name="obligation_id" value="{re.escape(rule.id)}">\s*'
        r'<input type="hidden" name="gap_id" value="([^"]+)">\s*'
        r'<input type="hidden" name="clause_id" value="([^"]+)"',
        gaps,
    )
    assert form, f"the gaps screen raised no task against {rule.id}"

    client.post(
        "/w/demo/remediation/open",
        data={
            "obligation_id": rule.id,
            "gap_id": form.group(1),
            "clause_id": form.group(2),
            "title": "Outstanding filings",
            "priority": "HIGH",
            "by": "A Named Officer",
        },
        follow_redirects=True,
    )
    store = RemediationStore.load(tmp_path / "remediation.json")
    task_id = next(iter(store.tasks))
    return task_id, store.get(task_id).obligation_id


@requires_corpus
def test_the_screen_offers_the_records_filed_against_that_rule(client, tmp_path):
    task_id, _ = _task_against_a_real_gap(client, tmp_path)
    body = client.get("/w/demo/remediation").text

    assert f"/remediation/{task_id}/attach" in body
    assert "Attach the records that answer this" in _plain(body)
    assert 'name="evidence_id"' in body


@requires_corpus
def test_attaching_names_the_exact_event_and_moves_the_task(client, tmp_path):
    from sanhita.execute import EvidenceStore
    from sanhita.remediate import RemediationStore, TaskStatus

    task_id, obligation_id = _task_against_a_real_gap(client, tmp_path)
    event = EvidenceStore.load(tmp_path / "evidence.json").for_obligation(obligation_id)[0]

    client.post(
        f"/w/demo/remediation/{task_id}/attach",
        data={"evidence_id": event.id, "by": "A Named Officer"},
        follow_redirects=True,
    )

    task = RemediationStore.load(tmp_path / "remediation.json").get(task_id)
    assert task.evidence_ids == [event.id]
    assert task.status is TaskStatus.READY_FOR_RECHECK
    assert event.id in _plain(client.get("/w/demo/remediation").text)


@requires_corpus
def test_attaching_is_on_the_chain_with_the_ids_it_attached(client, tmp_path):
    """An inspector reads the chain, not the task's current field values."""
    from sanhita.execute import EvidenceStore
    from sanhita.remediate import RemediationStore

    task_id, obligation_id = _task_against_a_real_gap(client, tmp_path)
    event = EvidenceStore.load(tmp_path / "evidence.json").for_obligation(obligation_id)[0]
    client.post(
        f"/w/demo/remediation/{task_id}/attach",
        data={"evidence_id": event.id, "by": "A Named Officer"},
        follow_redirects=True,
    )

    store = RemediationStore.load(tmp_path / "remediation.json")
    intact, problem = store.log.verify()
    assert intact, problem

    attached = [e for e in store.log.entries if e.transition.value == "EVIDENCE_ATTACHED"]
    assert attached, "attaching evidence left no trace"
    assert event.id in attached[-1].detail["evidence_ids"]
    assert attached[-1].actor == "A Named Officer"


@requires_corpus
def test_an_invented_evidence_id_is_refused(client, tmp_path):
    """The value of an attachment is that it can be followed back to a document."""
    task_id, _ = _task_against_a_real_gap(client, tmp_path)

    response = client.post(
        f"/w/demo/remediation/{task_id}/attach",
        data={"evidence_id": "EV-DOES-NOT-EXIST", "by": "A Named Officer"},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "EV-DOES-NOT-EXIST" in response.text


@requires_corpus
def test_attaching_nothing_is_refused(client, tmp_path):
    task_id, _ = _task_against_a_real_gap(client, tmp_path)

    response = client.post(
        f"/w/demo/remediation/{task_id}/attach",
        data={"by": "A Named Officer"},
        follow_redirects=False,
    )

    assert response.status_code == 400


@requires_corpus
def test_attaching_does_not_close_anything(client, tmp_path):
    """The property the whole design turns on, asserted on the new route.

    If attaching could close a task, a person would be able to close their own
    remediation by uploading a file and pointing at it. Closure stays with the
    engine.
    """
    from sanhita.execute import EvidenceStore
    from sanhita.remediate import RemediationStore, TaskStatus

    task_id, obligation_id = _task_against_a_real_gap(client, tmp_path)
    event = EvidenceStore.load(tmp_path / "evidence.json").for_obligation(obligation_id)[0]
    client.post(
        f"/w/demo/remediation/{task_id}/attach",
        data={"evidence_id": event.id, "by": "A Named Officer"},
        follow_redirects=True,
    )

    task = RemediationStore.load(tmp_path / "remediation.json").get(task_id)
    assert task.status is not TaskStatus.CLOSED
    assert task.status is not TaskStatus.VERIFIED
    assert task.verified_at is None
