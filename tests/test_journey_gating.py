"""One question at a time, and assessment as an act rather than a page view.

Four properties, each of which was a real defect found by reading the product
the way a first-time compliance officer reads it.

**Setting up is gated.** The overview used to answer the whole product at once,
so somebody met "Upload compliance evidence" before there was a firm to hold
the evidence against, and the form that names the firm was below it.

**Assessment is an act.** The engine ran wherever anybody looked, and the run
was written down only if that person later opened the overview. So a firm could
read its own breaches with no record that an assessment ever happened.

**Regulatory change is the other journey.** The problem statement names two
distinct challenges. Numbering the amendment screen as stage five of the firm's
loop merged them.

**A gap closes pointing at something.** A task that closes without naming the
record that closed it leaves an inspector nothing to follow.
"""

from __future__ import annotations

import re

import pytest

from tests.conftest import requires_corpus, sign_in


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
    return client


def _plain(html: str) -> str:
    body = re.sub(r"<script.*?</script>", " ", html, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))


def _certified(tmp_path):
    from sanhita.cli_compile import _load_registry
    from sanhita.ir.enums import RuleStatus

    for rule in _load_registry(tmp_path / "rules.json").all_current():
        if rule.status is RuleStatus.CERTIFIED:
            return rule
    raise AssertionError("no certified rule in the store")


def _evidence(client, tmp_path, filed="2026-04-02"):
    rule = _certified(tmp_path)
    client.post(
        "/w/demo/evidence",
        headers={"x-sanhita-filename": "register.csv"},
        content=(
            "obligation_id,entity,occurred_on,filed_on,artifact_type,reference\n"
            f"{rule.id},ABC Securities,2026-03-31,{filed},report,RET-001\n"
        ).encode(),
    )


# ------------------------------------------------------------ setting up


@requires_corpus
def test_step_one_offers_nothing_but_naming_the_firm(client):
    page = _plain(client.get("/w/demo/company").text)

    assert "Step 1 of 3, setting up" in page
    assert "Firm name" in page
    # None of the later product, because none of it means anything yet.
    for premature in (
        "Upload compliance evidence",
        "Compliance assessment not yet run",
        "Falling due in 30 days",
        "Applicable duties",
    ):
        assert premature not in page, f"step one already offers {premature!r}"


@requires_corpus
def test_step_two_offers_nothing_but_the_framework_question(client):
    client.post(
        "/w/demo/company/save", data={"name": "ABC Securities"}, follow_redirects=True
    )
    page = _plain(client.get("/w/demo/company").text)

    assert "Step 2 of 3, setting up" in page
    assert "Which SEBI rulebooks apply to ABC Securities" in page
    assert "Upload compliance evidence" not in page


@requires_corpus
def test_step_three_offers_nothing_but_the_evidence_upload(client):
    """Saving a framework used to drop somebody onto the dashboard mid-setup."""
    client.post(
        "/w/demo/company/save", data={"name": "ABC Securities"}, follow_redirects=True
    )
    client.post(
        "/w/demo/company/frameworks", data={"framework": "demo"}, follow_redirects=True
    )
    page = _plain(client.get("/w/demo/company").text)

    assert "Step 3 of 3, setting up" in page
    assert "Bring your compliance evidence" in page
    assert "Stage 1 of 5" not in page


@requires_corpus
def test_the_overview_appears_only_once_setup_is_finished(client):
    client.post(
        "/w/demo/company/save", data={"name": "ABC Securities"}, follow_redirects=True
    )
    client.post(
        "/w/demo/company/frameworks", data={"framework": "demo"}, follow_redirects=True
    )
    client.post("/w/demo/setup/complete", follow_redirects=True)
    page = _plain(client.get("/w/demo/company").text)

    assert "Stage 1 of 5" in page
    assert "setting up" not in page
    # No records yet, so there is nothing an assessment could run against.
    assert "Assessment not available" in page
    assert "Upload compliance evidence" in page


# --------------------------------------------------- assessment is an act


@requires_corpus
def test_looking_at_the_result_does_not_record_an_assessment(client, tmp_path):
    """Reading a preview is not the same act as taking an assessment."""
    from sanhita.assess import AssessmentLog

    _evidence(client, tmp_path)
    for _ in range(3):
        client.get("/w/demo/gaps")

    assert not (tmp_path / "assessments.json").is_file(), (
        "looking at the screen wrote an assessment nobody took"
    )
    assert len(AssessmentLog.load(tmp_path / "assessments.json")) == 0


@requires_corpus
def test_an_unassessed_result_says_it_is_a_preview(client, tmp_path):
    _evidence(client, tmp_path)
    page = _plain(client.get("/w/demo/gaps").text)

    assert "Not yet assessed on these records" in page
    assert "preview of what an assessment would say" in page
    assert "Run compliance assessment" in page


@requires_corpus
def test_running_it_records_it_and_the_screen_then_says_so(client, tmp_path):
    from sanhita.assess import AssessmentLog

    _evidence(client, tmp_path)
    client.post("/w/demo/assess", follow_redirects=True)

    log = AssessmentLog.load(tmp_path / "assessments.json")
    assert len(log) == 1
    assert log.latest.has_findings

    page = _plain(client.get("/w/demo/gaps").text)
    assert "Assessment of record" in page
    assert log.latest.short_id in page
    assert "Not yet assessed" not in page


@requires_corpus
def test_changing_the_records_makes_the_result_a_preview_again(client, tmp_path):
    """An assessment is of the records it ran against, not of the firm forever."""
    _evidence(client, tmp_path, filed="2026-04-02")
    client.post("/w/demo/assess", follow_redirects=True)
    assert "Assessment of record" in _plain(client.get("/w/demo/gaps").text)

    _evidence(client, tmp_path, filed="")
    page = _plain(client.get("/w/demo/gaps").text)

    assert "Not yet assessed on these records" in page
    assert "have changed since run" in page


@requires_corpus
def test_assessing_with_no_records_is_refused(client):
    response = client.post("/w/demo/assess", follow_redirects=False)

    assert response.status_code == 400
    assert "nothing to assess" in response.text


# ------------------------------------------------ the two journeys, apart


@requires_corpus
def test_regulatory_change_is_not_a_stage_of_the_firms_loop(client):
    """The problem statement calls these two distinct challenges.

    Nobody closes a gap and then, as the next step, diffs two editions of a
    circular. That is a different question, usually asked because SEBI
    published something.
    """
    # The lifecycle row only exists once a firm has finished setting up.
    client.post(
        "/w/demo/company/save", data={"name": "ABC Securities"}, follow_redirects=True
    )
    client.post(
        "/w/demo/company/frameworks", data={"framework": "demo"}, follow_redirects=True
    )
    client.post("/w/demo/setup/complete", follow_redirects=True)

    body = client.get("/w/demo/company").text
    strip = body[body.index('class="stagebar"') :]
    strip = strip[: strip.index("</nav>")]

    numbered = strip[: strip.index("stagebar-apart")]
    assert "Regulatory changes" not in numbered, "it is still numbered as a stage"
    assert "Audit" in numbered, "the five stages should end at Audit"

    # Still reachable, and visibly set apart.
    assert "Separately" in strip
    assert "/w/demo/diff" in strip

    page = _plain(client.get("/w/demo/diff").text)
    assert "the other journey" in page
    assert "Stage" not in page.split("Regulatory change")[0][-200:]


# ----------------------------------------- closure has to point at something


@requires_corpus
def test_a_task_cannot_close_without_naming_what_closed_it(client, tmp_path):
    from sanhita.cli_compile import _load_registry
    from sanhita.ir.enums import DeadlineKind, RuleStatus
    from sanhita.remediate import RemediationStore

    rule = next(
        r
        for r in _load_registry(tmp_path / "rules.json").all_current()
        if r.status is RuleStatus.CERTIFIED
        and r.deadline is not None
        and r.deadline.kind is DeadlineKind.END_OF_PERIOD
        and r.evidence
    )
    client.post(
        "/w/demo/evidence",
        headers={"x-sanhita-filename": "register.csv"},
        content=(
            "obligation_id,entity,occurred_on,filed_on,artifact_type,reference\n"
            f"{rule.id},ABC Securities,2026-01-31,,{rule.evidence[0].artifact_type},\n"
        ).encode(),
    )
    client.post("/w/demo/assess", follow_redirects=True)
    gaps = client.get("/w/demo/gaps").text
    form = re.search(
        rf'name="obligation_id" value="{re.escape(rule.id)}">\s*'
        r'<input type="hidden" name="gap_id" value="([^"]+)">\s*'
        r'<input type="hidden" name="clause_id" value="([^"]+)"',
        gaps,
    )
    client.post(
        "/w/demo/remediation/open",
        data={
            "obligation_id": rule.id,
            "gap_id": form.group(1),
            "clause_id": form.group(2),
            "priority": "HIGH",
            "by": "S. Officer",
        },
        follow_redirects=True,
    )
    task_id = next(iter(RemediationStore.load(tmp_path / "remediation.json").tasks))

    refused = client.post(
        f"/w/demo/remediation/{task_id}/recheck",
        data={"by": "R. Sharma"},
        follow_redirects=False,
    )

    assert refused.status_code == 400
    assert "Attach the corrective evidence" in refused.text
    assert RemediationStore.load(tmp_path / "remediation.json").get(
        task_id
    ).recheck_count == 0, "the rule was run despite the refusal"

    # And the button is not offered either, so the refusal is not a surprise.
    page = client.get("/w/demo/remediation").text
    assert "disabled" in page[page.index("Run the rule again") - 200 :]


# ------------------------------------------------------------- the chain


@requires_corpus
def test_one_gap_can_be_read_end_to_end_on_one_page(client, tmp_path):
    """An inspector following a closure should not have to visit four screens."""
    from sanhita.cli_compile import _load_registry
    from sanhita.ir.enums import RuleStatus
    from sanhita.remediate import RemediationStore

    _evidence(client, tmp_path, filed="")
    client.post("/w/demo/assess", follow_redirects=True)
    gaps = client.get("/w/demo/gaps").text
    obligation = re.search(r'name="obligation_id" value="([^"]+)"', gaps).group(1)
    gap_id = re.search(r'name="gap_id" value="([^"]+)"', gaps).group(1)
    clause = re.search(r'name="clause_id" value="([^"]+)"', gaps).group(1)
    client.post(
        "/w/demo/remediation/open",
        data={
            "obligation_id": obligation,
            "gap_id": gap_id,
            "clause_id": clause,
            "title": "Outstanding filing",
            "owner": "Operations",
            "priority": "HIGH",
            "by": "S. Officer",
        },
        follow_redirects=True,
    )
    task_id = next(iter(RemediationStore.load(tmp_path / "remediation.json").tasks))

    response = client.get(f"/w/demo/chain/{task_id}")
    assert response.status_code == 200
    page = _plain(response.text)

    # Every link of the story, on one page.
    for section in (
        "The regulation",
        "The certified rule",
        "What the firm's records said",
        "The assessment that found it",
        "The work",
        "The corrective evidence",
        "The verdict",
    ):
        assert section in page, f"the chain is missing {section!r}"

    assert clause in page
    assert "Operations" in page
    assert "hash chained" in page
    # And it is reachable from the remediation screen rather than by guessing.
    assert f"/chain/{task_id}" in client.get("/w/demo/remediation").text


@requires_corpus
def test_the_chain_of_a_task_that_does_not_exist_is_a_clean_404(client):
    assert client.get("/w/demo/chain/REM-nope-001").status_code == 404
