"""The whole product, driven the way a person drives it.

Every step below goes through the actual HTTP routes. Nothing constructs a
`ComplianceEvent` by hand, nothing writes an evidence file directly, nothing
mocks a compliance result, and nothing sets a remediation status that the
engine did not produce. If a step could be skipped by reaching into the domain
model, this test would not be proof of anything.

    ABC Securities is configured
      -> the SEBI stock broker rulebook is already certified
      -> a company PDF is uploaded
      -> candidates appear for review
      -> a person maps one to a certified requirement
      -> the deterministic engine runs
      -> a gap is found
      -> a remediation task is raised, owned, with a deadline
      -> corrected evidence is uploaded and mapped
      -> the same certified rule is run again
      -> the task closes
      -> the whole life of it is on an intact chain

Run it on its own with

    pytest tests/test_company_document_to_closed_compliance.py -v
"""

from __future__ import annotations

import datetime as _dt
import re

import pytest

from tests.conftest import requires_corpus, sign_in


@pytest.fixture()
def client(corpus_pdf, tmp_path, monkeypatch):
    """A workbench on a copy of the real store, so the test never touches it."""
    import shutil

    from fastapi.testclient import TestClient

    from sanhita.web.app import create_app

    monkeypatch.setenv("SANHITA_SIGNING_KEY", "a-test-only-key")
    shutil.copy(corpus_pdf.parent.parent / ".sanhita" / "rules.json", tmp_path / "rules.json")
    client = TestClient(create_app(corpus_pdf, store=tmp_path / "rules.json"))
    # Compliance actions record who did them, so the journey these
    # tests walk needs an authenticated officer behind it.
    sign_in(client, name="R. Sharma")
    return client


def _plain(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def _month_ends_of(year: int) -> list[str]:
    """Every month end in a year, as ISO dates.

    An occasion is identified by the date it fell due, so a correction has to
    name that date. Filing the 28th of a 31-day month files a different
    occasion and leaves the original one outstanding.
    """
    import calendar as _cal

    return [
        f"{year}-{month:02d}-{_cal.monthrange(year, month)[1]:02d}"
        for month in range(1, 13)
    ]


def _monthly_certified_rule(tmp_path):
    """A signed rule that recurs, so silence about it is a gap rather than noise."""
    from sanhita.cli_compile import _load_registry
    from sanhita.ir.enums import DeadlineKind, RuleStatus

    for rule in _load_registry(tmp_path / "rules.json").all_current():
        if (
            rule.status is RuleStatus.CERTIFIED
            and rule.deadline is not None
            and rule.deadline.kind is DeadlineKind.END_OF_PERIOD
            and (rule.deadline.period or "").upper() in ("MONTH", "DAY")
            and rule.evidence
        ):
            return rule
    pytest.skip("the store carries no certified recurring rule to test against")


def _company_report(lines: list[str], title: str = "ABC SECURITIES") -> bytes:
    """A company document of the kind that names no Sanhita rule anywhere."""
    import fitz

    document = fitz.open()
    page = document.new_page()
    page.insert_text((60, 80), title + "\n" + "\n".join(lines), fontsize=11)
    data = document.tobytes()
    document.close()
    return data


@requires_corpus
def test_company_document_to_closed_compliance(client, tmp_path):
    from sanhita.company import Company, ReviewQueue
    from sanhita.execute.evidence import EvidenceStore
    from sanhita.remediate import RemediationStore, TaskStatus

    rule = _monthly_certified_rule(tmp_path)
    artifact = rule.evidence[0].artifact_type

    # ── 1. Company X is configured through the UI ──────────────────────
    response = client.post(
        "/w/demo/company/save",
        data={
            "name": "ABC Securities Pvt Ltd",
            "intermediary": "STOCK_BROKER",
            "registration": "INZ000000000",
            "processes": "Daily margin reporting\nClient onboarding",
            "systems": "Margin engine\nCRM",
            "facts": "Has retail clients\nOffers derivatives",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    firm = Company.load(tmp_path / "company.json")
    assert firm is not None and firm.name == "ABC Securities Pvt Ltd"

    # ── 1b. And says which SEBI rulebook governs it ────────────────────
    # Setting up is two answers, not one. Until the second is given there is
    # nothing to assess the firm against, so the product does not pretend.
    client.post(
        "/w/demo/company/frameworks", data={"framework": "demo"}, follow_redirects=True
    )
    assert Company.load(tmp_path / "company.json").frameworks == ["demo"]

    # Setting up is three answers. The third is being asked for records, which
    # this firm answers by finishing without any and uploading below.
    client.post("/w/demo/setup/complete", follow_redirects=True)

    # ── 2. Before any evidence, there is no score ──────────────────────
    page = _plain(client.get("/w/demo/company").text)
    assert "Assessment not available" in page, (
        "a percentage before evidence exists would be a fact about a random seed"
    )
    assert "Upload compliance evidence" in page, "the page must say what to do next"

    # ── 3. The firm uploads a real company document ────────────────────
    upload = client.post(
        "/w/demo/evidence",
        headers={"x-sanhita-filename": "ABC_margin_register_H1.pdf"},
        content=_company_report(
            [
                "Margin Statement Dispatch Register",
                "2026-01-31   RET-001   dispatched",
                "2026-02-28   RET-002   dispatched",
            ]
        ),
    )
    assert upload.status_code == 200, upload.text
    assert upload.json()["awaiting"] >= 2
    assert upload.json()["url"].endswith("/review")

    # ── 4. Nothing counts until a person rules on it ───────────────────
    assert not (tmp_path / "evidence.json").is_file() or len(
        EvidenceStore.load(tmp_path / "evidence.json")
    ) == 0

    # ── 5. A person maps one candidate to a certified requirement ──────
    queue = ReviewQueue.load(tmp_path / "review.json")
    first = queue.awaiting()[0]
    mapped = client.post(
        f"/w/demo/review/{first.item_id}/map",
        # `by` is accepted and ignored. Who mapped this is the account.
        data={"obligation_id": rule.id, "by": "Somebody Else"},
        follow_redirects=True,
    )
    assert mapped.status_code == 200

    store = EvidenceStore.load(tmp_path / "evidence.json")
    assert len(store) == 1, "the mapped candidate should now be evidence"
    assert store.events[0].source_document == "ABC_margin_register_H1.pdf"
    assert store.events[0].mapped_by == "R. Sharma", (
        "the mapping recorded a typed name rather than the signed-in account"
    )

    # ── 6. The engine runs, and the firm records that assessment ───────
    # A preview is not a finding. Acting on one would start the audit chain
    # from something nobody recorded, so the route refuses.
    client.post("/w/demo/assess", follow_redirects=True)
    gaps_page = client.get("/w/demo/gaps")
    assert gaps_page.status_code == 200
    gaps = gaps_page.text
    assert rule.source.clause_id in gaps or "GAP" in gaps.upper()

    obligation_ids = re.findall(r'name="obligation_id" value="([^"]+)"', gaps)
    gap_ids = re.findall(r'name="gap_id" value="([^"]+)"', gaps)
    clause_ids = re.findall(r'name="clause_id" value="([^"]+)"', gaps)
    assert obligation_ids, "the run produced no finding to remediate"

    # ── 7. A remediation task is raised, owned, with a deadline ────────
    due = (_dt.date.today() + _dt.timedelta(days=7)).isoformat()
    client.post(
        "/w/demo/remediation/open",
        data={
            "obligation_id": obligation_ids[0],
            "gap_id": gap_ids[0],
            "clause_id": clause_ids[0],
            "title": "Outstanding filings for ABC Securities",
            "owner": "Compliance Operations",
            "team": "Operations",
            "priority": "HIGH",
            "due": due,
            "action_text": "File the outstanding returns and retain dispatch proof",
            "by": "A Named Officer",
        },
        follow_redirects=True,
    )
    tasks = RemediationStore.load(tmp_path / "remediation.json")
    assert tasks.tasks, "no remediation task was persisted"
    task_id = next(iter(tasks.tasks))
    task = tasks.get(task_id)
    assert task.owner == "Compliance Operations"
    assert task.due_date == _dt.date.fromisoformat(due)

    # ── 8. Nobody can simply declare it fixed ──────────────────────────
    remediation_page = _plain(client.get("/w/demo/remediation").text).lower()
    for phrase in ("mark as fixed", "mark fixed", "mark resolved"):
        assert phrase not in remediation_page

    # ── 9. Corrected evidence arrives, as another real upload ──────────
    target = tasks.get(task_id).obligation_id

    # Corrected against the occasion that is actually outstanding, which means
    # matching its entity as well as its date. An occasion is identified by
    # (rule, entity, date), so a correction filed under a different spelling of
    # the firm's name is a new occasion rather than an answer to the old one,
    # and the original stays unfiled however many rows arrive.
    outstanding = [
        e
        for e in EvidenceStore.load(tmp_path / "evidence.json").for_obligation(target)
        if e.filed_on is None
    ]
    assert outstanding, "nothing is outstanding, so there is nothing to correct"
    entity = outstanding[0].entity
    corrected = client.post(
        "/w/demo/evidence",
        headers={"x-sanhita-filename": "ABC_corrected_returns.csv"},
        content=(
            "obligation_id,entity,occurred_on,filed_on,artifact_type,reference\n"
            # The occasions the firm actually owed, filed. The month ends
            # matter: an occasion is identified by its own date, so filing the
            # 28th of January does not answer a duty that fell due on the 31st.
            # This used to file the 28th of every month and cleared the gap by
            # accident, because the gap it happened to pick was a rule with no
            # records at all rather than one specific unfiled occasion.
            + "".join(
                f"{target},{entity},{day},{day},{artifact},FIX-{n:03d}\n"
                for n, day in enumerate(_month_ends_of(2026), start=1)
            )
        ).encode(),
    )
    assert corrected.status_code == 200, corrected.text
    # A CSV naming its rule needs no review, so it lands as evidence directly.
    assert corrected.json()["accepted"] >= 12

    # ── 10. The corrective record is named on the task ─────────────────
    # Not optional. A task that closes without pointing at the document that
    # closed it leaves an inspector nothing to follow, so the route refuses.
    too_soon = client.post(
        f"/w/demo/remediation/{task_id}/recheck",
        data={"by": "A Named Officer"},
        follow_redirects=False,
    )
    assert too_soon.status_code == 400
    assert "Attach the corrective evidence" in too_soon.text

    fix = EvidenceStore.load(tmp_path / "evidence.json").for_obligation(target)
    assert fix, "the corrected upload produced nothing to attach"
    client.post(
        f"/w/demo/remediation/{task_id}/attach",
        data={"evidence_id": [e.id for e in fix[:3]], "by": "A Named Officer"},
        follow_redirects=True,
    )
    assert RemediationStore.load(tmp_path / "remediation.json").get(task_id).evidence_ids

    # ── 11. The same certified rule is run again. This is what closes it ─
    client.post(
        f"/w/demo/remediation/{task_id}/recheck",
        data={"by": "A Named Officer"},
        follow_redirects=True,
    )

    final = RemediationStore.load(tmp_path / "remediation.json").get(task_id)
    assert final.recheck_count >= 1, "the rule was never re-run"
    assert final.status is TaskStatus.CLOSED, (
        f"expected CLOSED, got {final.status.value}. "
        f"Last re-check said: {final.last_recheck_result}"
    )
    assert final.verified_at is not None
    assert final.closed_at is not None

    # ── 12. The whole life of it is on an intact chain ─────────────────
    log = RemediationStore.load(tmp_path / "remediation.json").log
    intact, problem = log.verify()
    assert intact, problem

    transitions = [e.transition.value for e in log.for_task(task_id)]
    for expected in ("CREATED", "RECHECKED", "VERIFIED", "CLOSED"):
        assert expected in transitions, f"{expected} missing from {transitions}"

    # ── 13. And the dashboard now reports a real assessment ────────────
    client.post("/w/demo/assess", follow_redirects=True)
    dashboard = _plain(client.get("/w/demo/company").text)
    assert "Compliance assessment not yet run" not in dashboard
    assert "ABC Securities Pvt Ltd" in dashboard
    assert "reviewed evidence" in dashboard, (
        "the dashboard must name what it computed from"
    )


@requires_corpus
def test_the_journey_has_no_dead_ends(client):
    """Every state offers the next action.

    A page that reports a situation and offers nothing to do about it is where
    a first-time user stops, and this product has a lot of states.

    The next action has to be a control on the page, not a word in the
    masthead. This test used to pass on "Gaps" appearing in the navigation,
    which proved nothing about the screen the user was looking at.
    """
    # A firm that has finished setting up. Before that the overview is the
    # setup path, whose next action is the setup form itself.
    client.post(
        "/w/demo/company/save", data={"name": "ABC Securities"}, follow_redirects=True
    )
    client.post(
        "/w/demo/company/frameworks", data={"framework": "demo"}, follow_redirects=True
    )
    client.post("/w/demo/setup/complete", follow_redirects=True)

    pairs = [
        ("/w/demo/company", "Upload compliance evidence"),
        # Nothing uploaded, so the next action is the uploader itself.
        ("/w/demo/review", "Drop your compliance records here"),
        ("/w/demo/remediation", "gaps screen"),
    ]
    for path, expected in pairs:
        page = _plain(client.get(path).text)
        assert expected in page, f"{path} does not tell the user what to do next"


@requires_corpus
def test_evidence_hands_off_to_the_compliance_result(client, tmp_path):
    """Stage two ends by pointing at stage three, on the page rather than the bar.

    A masthead link is not a next action. Somebody who has just mapped their
    records should be told, where they are looking, that the assessment can now
    run and how to see it.
    """
    from sanhita.cli_compile import _load_registry
    from sanhita.ir.enums import RuleStatus

    rule = next(
        r
        for r in _load_registry(tmp_path / "rules.json").all_current()
        if r.status is RuleStatus.CERTIFIED
    )
    client.post(
        "/w/demo/evidence",
        headers={"x-sanhita-filename": "register.csv"},
        content=(
            "obligation_id,entity,occurred_on,filed_on,artifact_type,reference\n"
            f"{rule.id},ABC Securities,2026-03-31,2026-04-02,report,RET-001\n"
        ).encode(),
    )

    body = client.get("/w/demo/review").text
    page = _plain(body)

    assert "See your compliance result" in page
    assert f'href="/w/demo/gaps"' in body
    assert "Next" in page
