"""On a public URL, everybody opens on the demonstration and nobody edits it.

A shared deployment keeps each visitor's firm data under their own scope, so
one reviewer's filing register is never served to the next. That is right, and
it had a consequence nobody had looked at: the demonstration state seeded by
`sanhita demo-seed` is unscoped, no visitor's scope matches it, and the public
site therefore opened on an empty onboarding form for everyone. A judge would
have had to type a firm in before seeing anything the product does.

So a seeded file is readable by anyone as a starting point, and the first write
puts that visitor on their own copy. Safe in a way that sharing generally is
not: the seeded firm is synthetic and marked as such, so there is nothing in it
belonging to a person.

The property that must hold, and what these tests are for: **a visitor's own
records never go back into the seed, and no visitor ever sees another's.**
"""

from __future__ import annotations

import datetime as _dt
import re
import shutil

import pytest

from tests.conftest import complete_setup, requires_corpus, sign_in


@pytest.fixture()
def shared(corpus_pdf, tmp_path, monkeypatch):
    """A public deployment with the demonstration already seeded."""
    from sanhita.demo_seed import seed_demo_state

    monkeypatch.setenv("SANHITA_SIGNING_KEY", "a" * 64)
    monkeypatch.setenv("SANHITA_SHARED", "1")
    shutil.copy(corpus_pdf.parent.parent / ".sanhita" / "rules.json", tmp_path / "rules.json")
    seed_demo_state(
        tmp_path,
        at=_dt.datetime(2026, 8, 14, tzinfo=_dt.timezone.utc),
        include_account=False,
    )
    return tmp_path


def _visitor(corpus_pdf, store):
    """A browser nobody has used before."""
    from fastapi.testclient import TestClient

    from sanhita.web.app import create_app

    return TestClient(create_app(corpus_pdf, store=store / "rules.json"))


def _plain(html: str) -> str:
    body = re.sub(r"<script.*?</script>", " ", html, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))


# ------------------------------------------------- everybody sees the demo


@requires_corpus
def test_a_first_time_visitor_opens_on_the_demonstration(corpus_pdf, shared):
    """The defect: the public site opened on an empty form for everyone."""
    page = _plain(_visitor(corpus_pdf, shared).get("/w/demo/company").text)

    assert "ABC Securities Pvt Ltd" in page
    assert "Whose compliance is this?" not in page, "the visitor met onboarding"


@requires_corpus
def test_the_demonstration_carries_its_position(corpus_pdf, shared):
    """Not just the firm's name: the assessment it already ran."""
    page = _plain(_visitor(corpus_pdf, shared).get("/w/demo/company").text)

    assert "Of what could be determined" in page
    assert "Demo Compliance Officer" in page


@requires_corpus
def test_two_strangers_see_the_same_starting_point(corpus_pdf, shared):
    first = _plain(_visitor(corpus_pdf, shared).get("/w/demo/company").text)
    second = _plain(_visitor(corpus_pdf, shared).get("/w/demo/company").text)

    for page in (first, second):
        assert "ABC Securities Pvt Ltd" in page


# ------------------------------------------- and the first write forks it


@requires_corpus
def test_changing_the_firm_writes_a_copy_rather_than_the_seed(corpus_pdf, shared):
    """The property everything else depends on."""
    from sanhita.company import Company

    before = (shared / "company.json").read_text(encoding="utf-8")
    client = _visitor(corpus_pdf, shared)
    client.post(
        "/w/demo/company/save",
        data={"name": "Zeta Broking Services", "intermediary": "STOCK_BROKER"},
        follow_redirects=True,
    )

    assert (shared / "company.json").read_text(encoding="utf-8") == before, (
        "a visitor edited the demonstration everybody else opens on"
    )
    forked = [p for p in shared.glob("company.*.json")]
    assert forked, "the visitor's own copy was not written"
    assert Company.load(forked[0]).name == "Zeta Broking Services"


@requires_corpus
def test_one_visitors_change_is_invisible_to_another(corpus_pdf, shared):
    """The whole reason scoping exists, and it has to survive the fallback."""
    mine = _visitor(corpus_pdf, shared)
    mine.post(
        "/w/demo/company/save",
        data={"name": "Zeta Broking Services"},
        follow_redirects=True,
    )

    stranger = _plain(_visitor(corpus_pdf, shared).get("/w/demo/company").text)

    assert "Zeta Broking Services" not in stranger
    assert "ABC Securities Pvt Ltd" in stranger


@requires_corpus
def test_uploaded_evidence_does_not_join_the_demonstration(corpus_pdf, shared):
    from sanhita.cli_compile import _load_registry
    from sanhita.ir.enums import DeadlineKind, RuleStatus

    rule = next(
        r
        for r in _load_registry(shared / "rules.json").all_current()
        if r.status is RuleStatus.CERTIFIED
        and r.deadline is not None
        and r.deadline.kind is DeadlineKind.END_OF_PERIOD
        and r.evidence
    )
    before = (shared / "evidence.json").read_text(encoding="utf-8")

    client = _visitor(corpus_pdf, shared)
    client.post(
        "/w/demo/evidence",
        headers={"x-sanhita-filename": "mine.csv"},
        content=(
            "obligation_id,entity,occurred_on,filed_on,artifact_type,reference\n"
            f"{rule.id},Zeta Broking,2026-02-28,,{rule.evidence[0].artifact_type},Z1\n"
        ).encode(),
    )

    assert (shared / "evidence.json").read_text(encoding="utf-8") == before, (
        "a visitor's filing register was written into the shared demonstration"
    )
    assert list(shared.glob("evidence.*.json")), "their own copy was not written"


@requires_corpus
def test_reassessing_the_seed_unchanged_records_nothing_new(corpus_pdf, shared):
    """The seeded run already covers those exact inputs, so pressing assess on
    an untouched demonstration is correctly a no-op rather than a second
    identical entry in everybody's history."""
    before = (shared / "assessments.json").read_text(encoding="utf-8")

    client = _visitor(corpus_pdf, shared)
    sign_in(client, name="R. Nair", email="nair@example.invalid")
    client.post("/w/demo/assess", follow_redirects=True)

    assert (shared / "assessments.json").read_text(encoding="utf-8") == before


@requires_corpus
def test_an_assessment_of_their_own_records_is_theirs_alone(corpus_pdf, shared):
    """A run written into the seed would appear in everybody's history."""
    from sanhita.cli_compile import _load_registry
    from sanhita.ir.enums import DeadlineKind, RuleStatus

    rule = next(
        r
        for r in _load_registry(shared / "rules.json").all_current()
        if r.status is RuleStatus.CERTIFIED
        and r.deadline is not None
        and r.deadline.kind is DeadlineKind.END_OF_PERIOD
        and r.evidence
    )
    before = (shared / "assessments.json").read_text(encoding="utf-8")

    client = _visitor(corpus_pdf, shared)
    sign_in(client, name="R. Nair", email="nair@example.invalid")
    # Their own firm, because signing in stops the fallback: an account means
    # somebody came to do their own work, so they are no longer handed the
    # seeded demonstration to assess. This is what a real visitor now does.
    complete_setup(client, name="Zeta Broking")
    # Their own records, so the run is genuinely a different one.
    client.post(
        "/w/demo/evidence",
        headers={"x-sanhita-filename": "mine.csv"},
        content=(
            "obligation_id,entity,occurred_on,filed_on,artifact_type,reference\n"
            f"{rule.id},Zeta Broking,2026-03-31,2026-03-31,"
            f"{rule.evidence[0].artifact_type},Z9\n"
        ).encode(),
    )
    client.post("/w/demo/assess", follow_redirects=True)

    assert (shared / "assessments.json").read_text(encoding="utf-8") == before
    assert list(shared.glob("assessments.u*.json"))


@requires_corpus
def test_a_task_raised_by_one_visitor_is_theirs_alone(corpus_pdf, shared):
    from sanhita.cli_compile import _load_registry
    from sanhita.ir.enums import DeadlineKind, RuleStatus
    from sanhita.remediate import RemediationStore

    client = _visitor(corpus_pdf, shared)
    sign_in(client, name="R. Nair", email="nair2@example.invalid")
    complete_setup(client, name="Zeta Broking")

    # A gap of their own to remediate. This used to lean on the seeded
    # demonstration's records, which a signed-in visitor no longer inherits, so
    # the firm has to have something to be assessed against.
    rule = next(
        r
        for r in _load_registry(shared / "rules.json").all_current()
        if r.status is RuleStatus.CERTIFIED
        and r.deadline is not None
        and r.deadline.kind is DeadlineKind.END_OF_PERIOD
        and r.evidence
    )
    client.post(
        "/w/demo/evidence",
        headers={"x-sanhita-filename": "mine.csv"},
        content=(
            "obligation_id,entity,occurred_on,filed_on,artifact_type,reference\n"
            f"{rule.id},Zeta Broking,2026-01-31,,{rule.evidence[0].artifact_type},\n"
        ).encode(),
    )
    client.post("/w/demo/assess", follow_redirects=True)

    gaps = client.get("/w/demo/gaps").text
    form = re.search(
        r'name="obligation_id" value="([^"]+)">\s*'
        r'<input type="hidden" name="gap_id" value="([^"]+)">\s*'
        r'<input type="hidden" name="clause_id" value="([^"]+)"',
        gaps,
    )
    assert form, "the demonstration offers no finding to remediate"
    client.post(
        "/w/demo/remediation/open",
        data={
            "obligation_id": form.group(1),
            "gap_id": form.group(2),
            "clause_id": form.group(3),
            "priority": "HIGH",
        },
        follow_redirects=True,
    )

    assert not (shared / "remediation.json").is_file(), (
        "a visitor's task was written into the shared demonstration"
    )
    theirs = list(shared.glob("remediation.u*.json"))
    assert theirs and RemediationStore.load(theirs[0]).tasks


# --------------------------------------------------- and off a shared box


@requires_corpus
def test_a_laptop_still_reads_and_writes_one_set_of_files(corpus_pdf, tmp_path, monkeypatch):
    """Nothing about this may change the single-user case."""
    from sanhita.demo_seed import seed_demo_state

    monkeypatch.setenv("SANHITA_SIGNING_KEY", "a" * 64)
    monkeypatch.delenv("SANHITA_SHARED", raising=False)
    shutil.copy(corpus_pdf.parent.parent / ".sanhita" / "rules.json", tmp_path / "rules.json")
    seed_demo_state(tmp_path, include_account=False)

    client = _visitor(corpus_pdf, tmp_path)
    client.post(
        "/w/demo/company/save", data={"name": "Zeta Broking"}, follow_redirects=True
    )

    from sanhita.company import Company

    assert Company.load(tmp_path / "company.json").name == "Zeta Broking"
    assert not list(tmp_path.glob("company.*.json")), "a laptop grew a scoped copy"
