"""If there are two of us and eighty things, what do we do on Monday?

The problem statement singles out "smaller intermediaries with limited
compliance resources", and the honest reading is not a discount or a simplified
mode. The same regulation lands on a team of two as on a team of forty, and the
team of two cannot triage eighty items by reading them.

Two properties are load-bearing and are what these tests hold. The ranking is
deterministic, because a list that reshuffles between two page loads teaches
people to ignore it. And it never presents itself as a risk opinion: a score
here means several stated facts line up, not that this is the firm's largest
legal exposure.
"""

from __future__ import annotations

import datetime as _dt
import re

import pytest

from tests.conftest import requires_corpus, sign_in

TODAY = _dt.date(2026, 8, 13)


# ------------------------------------------------------------ the fixtures


class _Value:
    def __init__(self, value):
        self.value = value


class _Status:
    def __init__(self, value="OPEN"):
        self.value = value
        self.is_open = value not in ("VERIFIED", "CLOSED")


class _Task:
    def __init__(self, task_id="REM-1", clause="15.1", due=None, priority="MEDIUM",
                 owner="", team="", status="OPEN", change_kind="", title=""):
        self.task_id, self.clause_id = task_id, clause
        self.obligation_id = f"SB-{clause}-a"
        self.title = title or f"Gap on clause {clause}"
        self.due_date = due
        self.priority = _Value(priority)
        self.status = _Status(status)
        self.owner, self.assigned_team = owner, team
        self.change_kind = change_kind

    def is_overdue(self, today=None):
        today = today or TODAY
        return self.due_date is not None and today > self.due_date

    def days_remaining(self, today=None):
        if self.due_date is None:
            return None
        return (self.due_date - (today or TODAY)).days


def _rank(tasks=(), health=None):
    from sanhita.priority import rank_open_work

    return rank_open_work(tasks=list(tasks), health=health, base="/w/demo", as_of=TODAY)


# ------------------------------------------------------- what scores what


def test_an_overdue_task_outranks_one_due_next_month():
    work = _rank([
        _Task("REM-1", "1.1", due=_dt.date(2026, 9, 5)),
        _Task("REM-2", "2.1", due=_dt.date(2026, 8, 1)),
    ])

    assert work.items[0].clause_id == "2.1"
    assert "it is overdue" in work.items[0].reasons


def test_a_deadline_this_week_is_ranked_above_one_this_month():
    work = _rank([
        _Task("REM-1", "1.1", due=_dt.date(2026, 9, 5)),
        _Task("REM-2", "2.1", due=_dt.date(2026, 8, 17)),
    ])

    assert work.items[0].clause_id == "2.1"


def test_a_broken_signature_is_ranked_as_the_serious_thing_it_is():
    """Until somebody signs it again, the rule cannot produce a finding."""
    from sanhita.priority import Band

    work = _rank([_Task("REM-1", "1.1", change_kind="RECERTIFY", priority="HIGH")])

    assert work.items[0].band is Band.NOW
    assert "a signature no longer covers the clause it names" in work.items[0].reasons
    assert work.items[0].kind == "AMENDMENT"


def test_an_item_nobody_owns_is_pushed_up_rather_than_forgotten():
    work = _rank([
        _Task("REM-1", "1.1", owner="R. Nair"),
        _Task("REM-2", "2.1"),
    ])

    assert work.items[0].clause_id == "2.1"
    assert "nobody owns it" in work.items[0].reasons
    assert not work.items[0].is_owned


def test_a_reopened_task_is_ranked_up():
    """The fix was attempted and the engine still finds a breach."""
    work = _rank([
        _Task("REM-1", "1.1", owner="R. Nair"),
        _Task("REM-2", "2.1", owner="R. Nair", status="REOPENED"),
    ])

    assert work.items[0].clause_id == "2.1"


def test_a_closed_task_is_not_work():
    work = _rank([_Task("REM-1", "1.1", status="CLOSED")])

    assert work.total == 0
    assert "no open compliance work" in work.headline()


# ------------------------------------------------- evidence joins the list


def test_records_that_stopped_arriving_appear_beside_the_tasks():
    """A team should see one list, not two screens that each hold half of it."""
    from sanhita.health import EvidenceHealth, RuleHealth, Signal

    health = EvidenceHealth(as_of=TODAY, since=_dt.date(2025, 7, 9))
    health.rules = [
        RuleHealth(
            obligation_id="SB-9.1-a",
            clause_id="9.1",
            requirement="file the monthly return",
            signal=Signal.GONE_QUIET,
            period="MONTH",
        )
    ]
    work = _rank(health=health)

    assert work.total == 1
    assert work.items[0].kind == "EVIDENCE"
    assert "its records have stopped arriving" in work.items[0].reasons
    assert "it recurs monthly or more often" in work.items[0].reasons


def test_a_healthy_duty_is_not_put_on_the_list():
    from sanhita.health import EvidenceHealth, RuleHealth, Signal

    health = EvidenceHealth(as_of=TODAY, since=_dt.date(2025, 7, 9))
    health.rules = [
        RuleHealth(obligation_id="SB-9.1-a", clause_id="9.1",
                   requirement="file it", signal=Signal.CURRENT)
    ]

    assert _rank(health=health).total == 0


# ---------------------------------------------------------- the properties


def test_the_same_inputs_produce_the_same_order_every_time():
    """A list that reshuffles between two page loads is one nobody can be
    held to."""
    tasks = [
        _Task("REM-1", "1.1", due=_dt.date(2026, 8, 1)),
        _Task("REM-2", "2.1", due=_dt.date(2026, 8, 1)),
        _Task("REM-3", "3.1", due=_dt.date(2026, 8, 1)),
    ]
    first = [i.clause_id for i in _rank(tasks).items]
    for _ in range(5):
        assert [i.clause_id for i in _rank(tasks).items] == first


def test_ties_are_broken_by_something_stable_rather_than_by_chance():
    work = _rank([_Task("REM-2", "2.1"), _Task("REM-1", "1.1")])

    assert [i.clause_id for i in work.items] == ["1.1", "2.1"]


def test_every_item_says_which_factors_fired():
    """A score shown without its reasons invites being trusted."""
    work = _rank([_Task("REM-1", "1.1", due=_dt.date(2026, 8, 1), priority="HIGH")])
    item = work.items[0]

    assert item.reasons
    assert all(r for r in item.reasons)
    assert "overdue" in item.describe().lower()


def test_the_formula_is_written_down_where_a_person_can_argue_with_it():
    from sanhita.priority import FACTORS

    assert len(FACTORS) >= 8
    for name, weight, why in FACTORS:
        assert name and why, "a factor with no explanation is a magic number"
        assert 0 < weight <= 50


def test_only_five_are_offered_because_that_is_what_a_small_team_holds():
    work = _rank([_Task(f"REM-{n}", f"{n}.1") for n in range(1, 12)])

    assert work.total == 11
    assert len(work.top) == 5


# ------------------------------------------------------------ through the UI


@pytest.fixture()
def client(corpus_pdf, tmp_path, monkeypatch):
    import shutil

    from fastapi.testclient import TestClient

    from sanhita.web.app import create_app

    monkeypatch.setenv("SANHITA_SIGNING_KEY", "a" * 64)
    store = tmp_path / "rules.json"
    shutil.copy(corpus_pdf.parent.parent / ".sanhita" / "rules.json", store)
    client = TestClient(create_app(corpus_pdf, store=store))
    sign_in(client, name="R. Nair")
    return client


def _plain(html: str) -> str:
    body = re.sub(r"<script.*?</script>", " ", html, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))


def _recurring(tmp_path):
    from sanhita.cli_compile import _load_registry
    from sanhita.ir.enums import DeadlineKind, RuleStatus

    return next(
        r
        for r in _load_registry(tmp_path / "rules.json").all_current()
        if r.status is RuleStatus.CERTIFIED
        and r.deadline is not None
        and r.deadline.kind is DeadlineKind.END_OF_PERIOD
        and r.evidence
    )


@requires_corpus
def test_the_overview_says_what_to_fix_first(client, tmp_path):
    rule = _recurring(tmp_path)
    client.post(
        "/w/demo/company/save",
        data={"name": "ABC Securities", "intermediary": "STOCK_BROKER"},
        follow_redirects=True,
    )
    client.post(
        "/w/demo/company/frameworks", data={"framework": "demo"}, follow_redirects=True
    )
    client.post(
        "/w/demo/evidence",
        headers={"x-sanhita-filename": "register.csv"},
        content=(
            "obligation_id,entity,occurred_on,filed_on,artifact_type,reference\n"
            f"{rule.id},ABC Securities,2025-01-31,,{rule.evidence[0].artifact_type},R1\n"
        ).encode(),
    )
    client.post("/w/demo/setup/complete", follow_redirects=True)

    page = _plain(client.get("/w/demo/company").text)

    assert "What should we fix first?" in page
    assert "This is not a legal risk opinion" in page
    assert "Do this week" in page or "Do this month" in page or "When you can" in page


@requires_corpus
def test_a_firm_with_nothing_open_is_not_shown_an_empty_list(client):
    client.post(
        "/w/demo/company/save", data={"name": "ABC Securities"}, follow_redirects=True
    )
    client.post(
        "/w/demo/company/frameworks", data={"framework": "demo"}, follow_redirects=True
    )
    client.post("/w/demo/setup/complete", follow_redirects=True)

    page = _plain(client.get("/w/demo/company").text)

    assert "What should we fix first?" not in page
